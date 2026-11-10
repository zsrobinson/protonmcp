# protonmcp

A small Streamable HTTP MCP server for Proton Mail through a local Proton Bridge.
It never sends mail and never permanently deletes mail. All existing tools are
enabled: folder listing, header-only listing/search, unsubscribe-header inspection,
explicit body reads, draft creation, archive, unarchive, move, label, mark-read, and action history.
Archive/move use IMAP MOVE; labels use COPY; mark-read sets only Seen. There is no
SMTP client, trash/delete tool, EXPUNGE or CLOSE command. Unsubscribe links are returned
for manual handling, never opened or sent. Drafts are saved with APPEND, never sent.

## Setup

Install Python 3.12+ and uv. Install and sign in to Proton Bridge separately, export
its local TLS certificate, and provision a private IMAP credential file containing
exactly two UTF-8 lines: Bridge username, then Bridge-generated password. Never use
your Proton account password. Linux ARM Bridge availability/support depends on Proton;
this repository does not install Bridge, accounts, services, tunnels, or credentials.

```sh
uv sync --locked
# Export your reviewed configuration, or load it through a process manager.
uv run --locked python mail_mcp.py
```

The server binds only `127.0.0.1:8766`, endpoint `/mcp`; Bridge remains at
`127.0.0.1:1143`. STARTTLS and the explicitly trusted Bridge certificate are required.
No connection is made by importing the module. `.env.example` documents configuration;
`.env` files are not automatically loaded.

## Configuration

- `CREDENTIALS_DIRECTORY`: directory containing `proton-bridge-imap`. Compatible with
  systemd `LoadCredential=proton-bridge-imap:...`; secrets are never accepted from env.
- `PROTON_BRIDGE_CERT`: required path to the exported certificate.
- `PROTON_ACTION_LOG`: private JSONL audit path. Legacy default:
  `/var/lib/proton-mail/actions/actions.jsonl`. Parent must be owned by the process
  user, mode 0700; log must be an owned, unlinked regular file, mode 0600.
- `ACCESS_TEAM_DOMAIN` and `ACCESS_AUD`: both or neither. When set, verify every
  request's Cloudflare Access RS256 assertion, issuer, audience and expiry before
  tool dispatch. JWKS retrieval fails closed. Omit both only for trusted localhost.
- `PROTON_PUBLIC_HOST`: exact public hostname (optional port), without scheme/path.
  No public hostname is accepted by default. Its HTTPS Origin is allowed only
  after successful Access verification. Exact localhost Host/Origin checks remain.

Existing ports, credential format, tool names/schemas, enabled mailbox writes,
limits and durable audit guarantees are preserved; cleanup audit records now group UIDs by chunk. Deployments migrating from a hard-coded
public host must set `PROTON_PUBLIC_HOST` to that same host before switching code.

## Privacy and safety

Headers and audit history reveal senders and subjects. Body reads and mailbox writes
require deliberate client permission choices; the server authenticates the caller
but does not enforce per-operation consent. Keep credentials, certificates, audit
logs, mail exports and Bridge state outside the checkout. Never commit captured mail.
Tests use generated synthetic messages and local cryptographic fixtures only.

Reads use read-only selection and BODY.PEEK. All Mail searches require `since` and
`before`. Search network work has a 45-second deadline, including untagged IMAP
progress; date bounds and limits do not guarantee fast All Mail scans. Search and body results include a `web_url` only when Bridge supplies a
valid opaque Proton message ID; open with the matching signed-in account and grouping
disabled. UIDs require matching UIDVALIDITY. Cleanup validates explicit identities,
existing safe destinations and MOVE capability before changes, supports dry runs,
and journals intent/results. One mail operation runs at a time; concurrent calls
receive a busy result while ping/HTTP remain responsive. Never blindly retry a draft
or uncertain mutation; inspect the mailbox and action history first.

## Batching, recovery and shutdown

- `PROTON_IMAP_CHUNK_SIZE`: positive integer, default `50`. MOVE, COPY and STORE
  use UID sets in chunks. Each cleanup call uses one connection, one mailbox
  selection, and one header FETCH for all explicit UIDs. Search/list fetch only
  the requested result slice in one header FETCH. Body reads group header
  preflight by mailbox and still fetch each explicit body separately.
- `PROTON_WRITE_TIMEOUT_SECONDS`: overall write deadline, default `120` seconds,
  including connection, login and preflight. A watchdog interrupts the transport.
  Timeout results identify completed chunks and uncertain UIDs. A timeout before
  mutation completes no chunks; an interrupted mutation marks its whole chunk
  uncertain and stops. Draft timeouts require inspecting Drafts before retrying.
- `PROTON_DRAIN_TIMEOUT_SECONDS`: default `60` seconds. SIGTERM refuses new tool
  calls and lets the active call finish within the drain budget, then exits.
  Configure systemd `TimeoutStopSec` above this value, for example `75s` for a
  `60s` drain. If the budget expires, durable intents identify potentially
  unfinished work; inspect the mailbox and journal before retrying.

Cleanup records share one `action_id` per call and have zero-based `chunk` indexes,
per-UID `outcomes`, and COPYUID values/mappings when supplied by Bridge. Each chunk
has one durable intent before mutation and one durable result after it. Audit
failure or a command error stops further chunks; a missing durable result leaves
that intent unresolved. Dry runs remain previews and use one audit record per
chunk. Existing tools retain their arguments and `dry_run=false` default.

`unarchive_messages` moves only from **Archive** to **INBOX**, using either
`uids` plus Archive `uidvalidity`, or `action_id` from a prior archive call.
Optional `folder` and `destination` must be `Archive` and `INBOX` respectively.
Action-based recovery requires completed chunk records with complete COPYUID
mappings and one Archive UIDVALIDITY; partial/uncertain actions are refused.
If Bridge supplies no COPYUID, search Archive and provide explicit identities.
The current Archive UIDVALIDITY is checked before moving anything. Unarchive
uses the same batching, deadline, dry-run default and durable journal rules as
archive. Moves from All Mail are refused. Moves require MOVE capability and never
fall back to COPY, Deleted flags or EXPUNGE. The server never sends mail and
never permanently deletes mail.

## Performance

Listing and search first run UID SEARCH with metadata keys only (dates and
UNSEEN, otherwise ALL). Header filters scan UID ENVELOPE and INTERNALDATE in
pages of 200, newest UID first, matching decoded subjects and display names or
addresses locally with case-insensitive substrings. Recipient matches To or Cc.
No body or TEXT search is supported. Only the final matches load header fields.
Without header filters, the existing last-`limit` UID slice and ordering remain.

Scanning stops after enough matches, 5,000 candidates, or 30 seconds checked
between pages. `complete: true` means the requested limit was filled or all
candidates were checked; it does not promise all matching mail was returned.
`complete: false` returns partial results: narrow `since`/`before` or the folder.
`candidates`, `scanned`, `pages`, and `oldest_scanned_date` (an internal-date
calendar date, or null) describe coverage. UID order is not guaranteed to follow
message dates. A slow individual IMAP command remains subject to the existing
45-second search watchdog. All Mail still requires both dates; limits remain 1–50.

Each call emits one `mail_call` JSON line, separate from private action history:

- `v: 1`, `ts` (UTC ISO), allowlisted `tool`, `total_ms` (monotonic elapsed time,
  including connection, audit and cleanup), `outcome` (`ok`, `tool_error`, `error`,
  `timeout`, or `refused`), and `error_class` (allowlisted name/code or null).
- `req_bytes`/`resp_bytes`: UTF-8 JSON payload sizes. HTTP calls count the complete
  request/response JSON; direct Python calls count arguments/result (zero response
  bytes when an exception escapes).
- `phase`: milliseconds for `connect`, `credentials`, `tls_context`, `tcp_greeting`
  (including initial capability discovery), `starttls`, `capability` (authenticated),
  `login`, `select`, `search`, `fetch`, `mutate`, and `audit`. Connect is an aggregate
  excluding login; its subphases overlap that aggregate and must not be summed.
- Counts: `requested`, `matched`, `chunks`, `candidates`, `scanned`, `pages`, and
  `complete` (null for calls without search results). Busy and draining refusals
  use the same schema, with zero phase times and counts.

Arguments, folder names, addresses, subjects, UIDs, message IDs, exception messages,
paths and bodies are excluded. Private action history still contains mailbox
identities and may contain subjects/senders; protect it as described above.

## Development

```sh
uv sync --locked
uv run --locked ruff check .
uv run --locked pytest -q
```

MIT licensed. Deployment-specific installers, systemd units and operational helpers
belong in the operator's deployment repository.
