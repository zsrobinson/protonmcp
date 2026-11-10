# protonmcp

A small Streamable HTTP MCP server for Proton Mail through a local Proton Bridge.
It never sends mail and never permanently deletes mail. All existing tools are
enabled: folder listing, header-only listing/search, unsubscribe-header inspection,
explicit body reads, draft creation, archive, move, label, mark-read, and action history.
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
limits and audit behavior are preserved. Deployments migrating from a hard-coded
public host must set `PROTON_PUBLIC_HOST` to that same host before switching code.

## Privacy and safety

Headers and audit history reveal senders and subjects. Body reads and mailbox writes
require deliberate client permission choices; the server authenticates the caller
but does not enforce per-operation consent. Keep credentials, certificates, audit
logs, mail exports and Bridge state outside the checkout. Never commit captured mail.
Tests use generated synthetic messages and local cryptographic fixtures only.

Reads use read-only selection and BODY.PEEK. All Mail searches require `since` and
`before`. Search and body results include a `web_url` only when Bridge supplies a
valid opaque Proton message ID; open with the matching signed-in account and grouping
disabled. UIDs require matching UIDVALIDITY. Cleanup validates explicit identities,
existing safe destinations and MOVE capability before changes, supports dry runs,
and journals intent/results. One mail operation runs at a time; concurrent calls
receive a busy result while ping/HTTP remain responsive. Never blindly retry a draft
or uncertain mutation; inspect the mailbox and action history first.

## Development

```sh
uv sync --locked
uv run --locked ruff check .
uv run --locked pytest -q
```

MIT licensed. Deployment-specific installers, systemd units and operational helpers
belong in the operator's deployment repository.
