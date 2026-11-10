#!/usr/bin/env python3
"""Local MCP: separate header/body tools and logged cleanup, draft APPEND."""
import email.policy
from email.parser import BytesParser
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
import http.server
import imaplib
import json
import os
from pathlib import Path
import re
import ssl
import threading
from action_log import ActionLog, PublicError
from uuid import uuid4
from access import configured_access

MAX_MESSAGE = 2 * 1024 * 1024
MAX_REQUEST = 128 * 1024
PROTOCOLS = ('2025-03-26', '2025-06-18', '2025-11-25')


def text(value, maximum=512):
    if not isinstance(value, str) or len(value) > maximum or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise PublicError('Invalid text')
    return value


def quote(value):
    value = text(value)
    value.encode('ascii')  # IMAP modified UTF-7 folder names; no raw UTF-8 commands.
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def ok(result):
    status, data = result
    if status != 'OK':
        raise PublicError('IMAP operation failed')
    return data


def message_web_url(headers):
    """Bridge's API message ID, for a Proton web account with grouping disabled."""
    values = headers.get_all('X-Pm-Internal-Id', [])
    if len(values) != 1:
        return None
    ident = str(values[0]).strip(' \t\r\n')
    # Preserve opaque IDs (including base64 padding); never guess from Message-ID.
    # Only accept path-safe characters that Proton's route parser preserves.
    if len(ident) > 512 or not re.fullmatch(r'[A-Za-z0-9_+~.-]+={0,2}', ident) or ident in ('.', '..'):
        return None
    # No /u/0: the browser selects its signed-in account; use the Bridge account.
    return 'https://mail.proton.me/all-mail/' + ident


class Mail:
    def __init__(self, client, sender=None):
        self.client = client
        self.sender = sender

    def folders(self):
        folders = []
        for line in ok(self.client.list()):
            # Fail closed on unsupported literal LIST responses, never guess a write target.
            if not isinstance(line, bytes):
                raise PublicError('Unsupported folder encoding')
            match = re.fullmatch(rb'\(([^)]*)\) (?:"(?:[^"\\]|\\.)*"|NIL) ("(?:[^"\\]|\\.)*"|[^\s]+)', line)
            if not match:
                raise PublicError('Unsupported folder encoding')
            name = match[2].decode('ascii')
            if name.startswith('"'):
                name = re.sub(r'\\(.)', r'\1', name[1:-1])
            folders.append({'name': name, 'flags': match[1].decode('ascii').split()})
        return folders

    def select(self, folder, validity=None):
        ok(self.client.select(quote(folder), readonly=True))  # Reads always EXAMINE; cleanup has its own SELECT.
        _, data = self.client.response('UIDVALIDITY')
        current = data[0].decode('ascii') if data and data[0] else None
        if current is None or (validity is not None and current != validity):
            raise PublicError('Mailbox UIDVALIDITY changed; search again')
        return current

    HEADER_FIELDS = ('From To Cc Subject Date Message-ID References In-Reply-To '
                     'List-Unsubscribe List-Unsubscribe-Post X-Pm-Internal-Id')

    def metadata(self, folder, uid, uidvalidity):
        self.select(folder, uidvalidity)
        data = ok(self.client.uid('FETCH', uid,
                  '(UID FLAGS RFC822.SIZE BODY.PEEK[HEADER.FIELDS (' + self.HEADER_FIELDS + ')]<0.65537>)'))
        tuples = [row for row in data if isinstance(row, tuple)]
        if len(tuples) != 1 or len(tuples[0][1]) > 65536:
            raise PublicError('Message missing or headers too large')
        attributes, raw = tuples[0]
        attributes += b' ' + b' '.join(row for row in data if isinstance(row, bytes))
        identity = re.search(rb'UID (\d+)', attributes)
        if not identity or identity[1].decode() != uid:
            raise PublicError('Unexpected message identity')
        flags = re.search(rb'FLAGS \(([^)]*)\)', attributes)
        size = re.search(rb'RFC822.SIZE (\d+)', attributes)
        if not flags or not size:
            raise PublicError('Missing message metadata')
        headers = BytesParser(policy=email.policy.default).parsebytes(raw, headersonly=True)
        return {'uid': uid, 'uidvalidity': uidvalidity, 'folder': folder,
                'web_url': message_web_url(headers),
                'headers': {key: str(headers.get(key, '')) for key in self.HEADER_FIELDS.split()},
                'flags': flags[1].decode('ascii').split(), 'size': int(size[1]),
                'labels': ([folder[7:]] if folder.startswith('Labels/') else []) +
                          (['Starred'] if b'\\Flagged' in flags[1].split() else []),
                'labelsComplete': False,
                'hasAttachments': None, 'attachmentCount': None,
                'attachmentMetadata': 'Unknown: header-only IMAP cannot reliably count attachments.'}

    def search(self, folder='', sender='', recipient='', subject='', since='', before='', unread=False, label='', limit=20):
        if label:
            label_folder = label if label.startswith('Labels/') else 'Labels/' + label
            if folder and folder != label_folder:
                raise PublicError('Search one mailbox at a time: use the Labels/ mailbox as folder for label search')
            folder = label_folder
        if not folder:
            raise PublicError('Provide folder or label')
        if folder == 'All Mail' and not (since and before):
            raise PublicError('All Mail searches require since and before; use a short date range or a narrower folder.')
        validity = self.select(folder)
        terms = []
        for key, value in [('FROM', sender), ('SUBJECT', subject), ('SINCE', since), ('BEFORE', before)]:
            if value:
                if key in ('SINCE', 'BEFORE'):
                    import datetime
                    datetime.datetime.strptime(value, '%d-%b-%Y')
                terms.extend([key, quote(value)])
        if recipient:
            terms.append('(OR TO ' + quote(recipient) + ' CC ' + quote(recipient) + ')')
        if unread:
            terms.append('UNSEEN')
        uids = ok(self.client.uid('SEARCH', None, '(' + ' '.join(terms) + ')' if terms else 'ALL'))[0].split()
        return {'uidvalidity': validity,
                'messages': [self.metadata(folder, u.decode(), validity) for u in uids[-limit:]],
                'truncated': len(uids) > limit}

    def message(self, journal, folder, uid, uidvalidity):
        metadata = self.metadata(folder, uid, uidvalidity)
        if metadata['size'] > MAX_MESSAGE:
            raise PublicError('Message exceeds 2 MiB read limit')
        entry = {'action_id': str(uuid4()), 'tool': 'read_messages', 'folder': folder,
                 'uids': [uid], 'uidvalidity': uidvalidity, 'subject': metadata['headers']['Subject'],
                 'sender': metadata['headers']['From'], 'destination': None}
        journal.append({**entry, 'status': 'attempting'})
        try:
            data = ok(self.client.uid('FETCH', uid, f'(UID BODY.PEEK[]<0.{MAX_MESSAGE + 1}>)'))
            chunks = [row for row in data if isinstance(row, tuple)]
            if len(chunks) != 1 or len(chunks[0][1]) > MAX_MESSAGE:
                raise PublicError('Message missing or too large')
            attributes = chunks[0][0] + b' ' + b' '.join(row for row in data if isinstance(row, bytes))
            identity = re.search(rb'UID (\d+)', attributes)
            if not identity or identity[1].decode() != uid:
                raise PublicError('Unexpected message identity')
            rendered = self.render(BytesParser(policy=email.policy.default).parsebytes(chunks[0][1]))
            journal.append({**entry, 'status': 'completed'})
            return {'folder': folder, 'uid': uid, 'uidvalidity': uidvalidity,
                    'web_url': metadata['web_url'], **rendered}
        except Exception:
            try:
                journal.append({**entry, 'status': 'uncertain'})
            except Exception:
                pass
            raise

    def read_messages(self, journal, messages):
        return {'messages': [self.message(journal, **ref) for ref in messages]}

    @staticmethod
    def render(message):
        body = message.get_body(preferencelist=('plain', 'html'))
        content = body.get_content() if body else ''
        attachments = []
        for index, part in enumerate(message.walk()):
            if part.get_filename() or part.get_content_disposition() == 'attachment':
                attachments.append({'part': index, 'filename': part.get_filename(),
                                    'contentType': part.get_content_type(),
                                    'bytes': len(part.get_payload(decode=True) or b'')})
        return {'headers': {key: str(message.get(key, '')) for key in
                ('From', 'To', 'Cc', 'Subject', 'Date', 'Message-ID', 'References', 'In-Reply-To')},
                'body': content[:65536], 'bodyTruncated': len(content) > 65536,
                'contentType': body.get_content_type() if body else None, 'attachments': attachments}

    def destination(self, tool_name, target):
        if tool_name == 'mark_read':
            return None
        existing = {f['name']: f for f in self.folders()}
        if target not in existing:
            raise PublicError('Existing destination required')
        flags = {f.casefold() for f in existing[target]['flags']}
        if flags & {'\\trash', '\\junk', '\\drafts', '\\sent', '\\flagged', '\\all', '\\noselect'}:
            raise PublicError('Forbidden destination')
        valid = ((tool_name == 'archive_messages' and target == 'Archive') or
                 (tool_name == 'move_messages' and target.startswith('Folders/')) or
                 (tool_name == 'add_labels' and target.startswith('Labels/')))
        if not valid or any(part.casefold() in ('trash', 'spam', 'junk', 'starred', 'drafts', 'sent', 'all mail')
                            for part in target.split('/')):
            raise PublicError('Forbidden destination')
        if tool_name in ('archive_messages', 'move_messages') and b'MOVE' not in self.client.capabilities:
            raise PublicError('Bridge lacks RFC 6851 MOVE; moves disabled, no fallback')
        return target

    def unsubscribe(self, folder, uidvalidity, uids):
        return {'messages': [{'uid': item['uid'], 'folder': folder, 'uidvalidity': uidvalidity,
                              'subject': item['headers']['Subject'], 'sender': item['headers']['From'],
                              'List-Unsubscribe': item['headers']['List-Unsubscribe'],
                              'List-Unsubscribe-Post': item['headers']['List-Unsubscribe-Post']}
                             for item in (self.metadata(folder, uid, uidvalidity) for uid in sorted(set(uids), key=int))],
                'handling': 'Manual only; no links opened, HTTP requests or mail sent.'}

    def cleanup(self, journal, tool_name, folder, uidvalidity, uids, destination='', dry_run=False):
        if tool_name == 'archive_messages':
            destination = 'Archive'
        target = self.destination(tool_name, destination)
        # Preflight EVERY explicit identity before any mutations. Missing UIDs fail closed.
        messages = [self.metadata(folder, uid, uidvalidity) for uid in sorted(set(uids), key=int)]
        changes = [{'uids': [item['uid']], 'subject': item['headers']['Subject'],
                    'sender': item['headers']['From'], 'destination': target} for item in messages]
        if dry_run:
            for change in changes:
                journal.append({'tool': tool_name, 'folder': folder, 'uidvalidity': uidvalidity,
                                **change, 'status': 'dry_run'})
            return {'dry_run': True, 'tool': tool_name, 'folder': folder, 'uidvalidity': uidvalidity,
                    'changes': changes, 'note': 'Preview only; chat approval is the assistant responsibility, not enforced by this server.'}
        ok(self.client.select(quote(folder), readonly=False))
        _, validity = self.client.response('UIDVALIDITY')
        if not validity or validity[0].decode() != uidvalidity:
            raise PublicError('Mailbox changed; search again')
        # Check existence again in the read-write selection, not just a prior EXAMINE.
        expected = {item['uid'] for item in messages}
        result = ok(self.client.uid('SEARCH', None, '(UID ' + ','.join(sorted(expected, key=int)) + ')'))
        if not result or set(result[0].decode().split()) != expected:
            raise PublicError('Messages disappeared; search again before cleanup')
        completed = []
        for change in changes:
            uid = change['uids'][0]
            entry = {'action_id': str(uuid4()), 'tool': tool_name, 'folder': folder,
                     'uidvalidity': uidvalidity, **change}
            # Durable intent BEFORE each command. If logging fails, do not perform it.
            try:
                journal.append({**entry, 'status': 'attempting'})
            except Exception:
                return {'completed': completed, 'uncertain': False, 'stopped': True, 'uid': uid,
                        'message': 'Action log unavailable; no command attempted for this UID. Inspect earlier results before retrying.'}
            try:
                if tool_name in ('archive_messages', 'move_messages'):
                    ok(self.client.uid('MOVE', uid, quote(target)))
                elif tool_name == 'add_labels':
                    # Proton maps COPY into Labels/ to adding the existing label. Never deletion.
                    ok(self.client.uid('COPY', uid, quote(target)))
                elif tool_name == 'mark_read':
                    ok(self.client.uid('STORE', uid, '+FLAGS.SILENT', '(\\Seen)'))
                else:
                    raise PublicError('Unknown cleanup tool')
                _, copied = self.client.response('COPYUID')
                journal.append({**entry, 'status': 'completed',
                                'copyuid': [value.decode('ascii') for value in copied if value] if copied else []})
                completed.append(uid)
            except Exception:
                try:
                    journal.append({**entry, 'status': 'uncertain'})
                except Exception:
                    pass  # The durable intent remains if the result cannot be journaled.
                return {'completed': completed, 'uncertain': True, 'uid': uid,
                        'message': 'Stopped after an uncertain result. Inspect mail and list_actions; never retry blindly.'}
        return {'completed': completed, 'dry_run': False, 'uncertain': False}

    def draft(self, to, subject, body, cc='', in_reply_to=''):
        # Draft creation writes only to the fixed special-use Drafts target.
        targets = [f['name'] for f in self.folders() if '\\Drafts' in f['flags']]
        if not targets:
            targets = [f['name'] for f in self.folders() if f['name'] == 'Drafts']
        if len(targets) != 1:
            raise PublicError('Unique Drafts folder required')
        message = EmailMessage(policy=email.policy.SMTP)
        if not self.sender:
            raise PublicError('Bridge sender required')
        message['From'] = text(self.sender, 2048)
        for key, value in [('To', to), ('Cc', cc), ('Subject', subject), ('In-Reply-To', in_reply_to)]:
            if value:
                message[key] = text(value, 2048)
        message['Date'] = formatdate(localtime=False)
        message['Message-ID'] = make_msgid()
        message.set_content(body)
        raw = message.as_bytes()
        if len(raw) > MAX_REQUEST:
            raise PublicError('Draft too large')
        ok(self.client.append(quote(targets[0]), '(\\Draft)', None, raw))
        return {'saved': True, 'folder': targets[0], 'messageId': str(message['Message-ID']),
                'note': 'Do not retry automatically after a connection error; check Drafts first.'}


S = {'type': 'string'}
LOCATION = {'folder': S, 'uid': {'type': 'string', 'pattern': '^[1-9][0-9]*$'},
            'uidvalidity': {'type': 'string', 'pattern': '^[1-9][0-9]*$'}}


def tool(name, description, properties, required, read=True):
    return {'name': name, 'description': description,
            'inputSchema': {'type': 'object', 'properties': properties, 'required': required, 'additionalProperties': False},
            'annotations': {'readOnlyHint': read, 'destructiveHint': not read, 'idempotentHint': read, 'openWorldHint': True}}


MESSAGES = {'folder': S, 'uidvalidity': LOCATION['uidvalidity'],
            'uids': {'type': 'array', 'items': LOCATION['uid'], 'minItems': 1, 'maxItems': 200}}
CLEANUP = {**MESSAGES, 'dry_run': {'type': 'boolean', 'default': False}}
SEARCH = {'folder': S, 'sender': S, 'recipient': S, 'subject': S, 'since': S, 'before': S,
          'unread': {'type': 'boolean'}, 'label': S,
          'limit': {'type': 'integer', 'minimum': 1, 'maximum': 50}}
TOOLS = [tool('list_folders', 'HEADERS/METADATA ONLY: list folders. Never returns message bodies.', {}, []),
         tool('list_messages', 'HEADERS ONLY: list message headers, flags, size; no bodies, snippets or attachment names. web_url opens that message in Proton web (grouping disabled, matching signed-in account), or is null. Supply folder or label. All Mail requires since and before; use a short date range.', SEARCH, []),
         tool('search_messages', 'HEADERS ONLY: sender/recipient/subject/date/unread search in a folder or label mailbox. Never searches/returns bodies. web_url opens that message in Proton web (grouping disabled, matching signed-in account), or is null. All Mail requires since and before; use a short date range.', SEARCH, []),
         tool('list_unsubscribe', 'HEADERS ONLY: list unsubscribe header values for explicit messages. Never opens URLs, makes requests or sends mail.',
              MESSAGES, list(MESSAGES)),
         tool('read_messages', 'BODY READ: returns message bodies and attachment names/sizes for up to 50 explicit refs. web_url opens that message in Proton web (grouping disabled, matching signed-in account), or is null. No attachment bytes. Set this tool to ask first in the client. Server does not enforce consent.',
              {'messages': {'type': 'array', 'minItems': 1, 'maxItems': 50,
                            'items': {'type': 'object', 'properties': LOCATION,
                                      'required': list(LOCATION), 'additionalProperties': False}}}, ['messages']),
         tool('create_draft', 'MAILBOX WRITE: creates a plain-text draft with Drafts APPEND. Never sends. Ask owner first in the client.',
              {k: S for k in ('to', 'cc', 'subject', 'body', 'in_reply_to')}, ['to', 'subject', 'body'], False),
         tool('archive_messages', 'MAILBOX WRITE: changes the mailbox by moving explicit UIDs to Archive. Ask owner first in chat/client; server does not enforce consent. Supports dry_run.',
              CLEANUP, list(MESSAGES), False),
         tool('move_messages', 'MAILBOX WRITE: changes the mailbox by moving explicit UIDs to an existing Folders/ destination. Ask owner first in chat/client. Supports dry_run.',
              {**CLEANUP, 'destination': S}, [*MESSAGES, 'destination'], False),
         tool('add_labels', 'MAILBOX WRITE: changes mailbox labels with COPY to an existing Labels/ destination. Retains source membership. Ask owner first in chat/client. Supports dry_run.',
              {**CLEANUP, 'destination': S}, [*MESSAGES, 'destination'], False),
         tool('mark_read', 'MAILBOX WRITE: changes the mailbox by setting Seen on explicit UIDs. Ask owner first in chat/client. Supports dry_run.',
              CLEANUP, list(MESSAGES), False),
         tool('list_actions', 'METADATA ONLY: read recent private read/write audit records, never bodies. The history read is itself logged.',
              {'limit': {'type': 'integer', 'minimum': 1, 'maximum': 200}}, [])]


def validate(name, args):
    schema = next((t['inputSchema'] for t in TOOLS if t['name'] == name), None)
    if schema is None or not isinstance(args, dict) or set(args) - schema['properties'].keys() or any(k not in args for k in schema['required']):
        raise PublicError('Unknown tool or invalid arguments')
    for key, value in args.items():
        if key == 'limit':
            if type(value) is not int or not 1 <= value <= (200 if name == 'list_actions' else 50):
                raise PublicError('Invalid limit')
        elif key in ('unread', 'dry_run'):
            if type(value) is not bool:
                raise PublicError('Invalid boolean')
        elif key == 'messages':
            if not isinstance(value, list) or not 1 <= len(value) <= 50:
                raise PublicError('Provide 1–50 explicit message references')
            for ref in value:
                if not isinstance(ref, dict) or set(ref) != set(LOCATION):
                    raise PublicError('Each message requires folder, uid and uidvalidity')
                for field, item in ref.items():
                    if not isinstance(item, str) or len(item) > 512:
                        raise PublicError('Invalid message reference')
                    if field != 'folder' and not re.fullmatch('[1-9][0-9]{0,9}', item):
                        raise PublicError('Invalid UID')
                quote(ref['folder'])
        elif key == 'uids':
            if not isinstance(value, list) or not 1 <= len(value) <= 200 or any(not isinstance(u, str) or not re.fullmatch('[1-9][0-9]{0,9}', u) for u in value):
                raise PublicError('Invalid UIDs')
        elif not isinstance(value, str) or len(value) > (65536 if key == 'body' else 2048):
            raise PublicError('Invalid string')
        elif key in ('uid', 'uidvalidity') and not re.fullmatch('[1-9][0-9]{0,9}', value):
            raise PublicError('Invalid UID')


def read_credentials():
    """Read systemd's private credential file; never accept secrets from env."""
    directory = os.environ.get('CREDENTIALS_DIRECTORY', '')
    try:
        if not os.path.isabs(directory):
            raise OSError()
        with (Path(directory) / 'proton-bridge-imap').open('rb') as source:
            raw = source.read(16385)
    except OSError:
        raise RuntimeError('Bridge IMAP credential unavailable; configure LoadCredential=proton-bridge-imap.') from None
    try:
        if len(raw) > 16384:
            raise ValueError()
        lines = raw.decode('utf-8').split('\n')
        if lines[-1] == '':
            lines.pop()
        if len(lines) != 2 or any(not line or any(ord(c) < 32 or ord(c) == 127 for c in line) for line in lines):
            raise ValueError()
    except (UnicodeError, ValueError):
        raise RuntimeError('Bridge IMAP credential must contain exactly two nonempty UTF-8 lines: username then password, with an optional final newline (16 KiB maximum).') from None
    return tuple(lines)


def connect():
    username, password = read_credentials()
    # Fixed IPv4 loopback, required STARTTLS before LOGIN, trust only exported Bridge cert.
    context = ssl.create_default_context(cafile=os.environ['PROTON_BRIDGE_CERT'])
    context.check_hostname = False  # Bridge's self-signed cert is the trust anchor.
    client = imaplib.IMAP4('127.0.0.1', 1143, timeout=20)
    try:
        client.starttls(context)
        client.login(username, password)
        client.bridge_username = username
        # Gluon advertises MOVE only AFTER authentication; imaplib's cached
        # pre-login capabilities are insufficient for the cleanup gate.
        capabilities = ok(client.capability())
        client.capabilities = tuple(capabilities[0].upper().split())
        return client
    except Exception:
        client.shutdown()
        raise


def audit_headers(journal, name, result, folder=None):
    items = result.get('messages', []) if isinstance(result, dict) else []
    for item in items or [{}]:
        headers = item.get('headers', {})
        journal.append({'tool': name, 'folder': item.get('folder', folder),
                        'uids': [item['uid']] if item.get('uid') else [],
                        'uidvalidity': item.get('uidvalidity'),
                        'subject': headers.get('Subject', item.get('subject')),
                        'sender': headers.get('From', item.get('sender')), 'destination': None, 'status': 'completed'})


def call(name, args, factory=connect, journal=None):
    validate(name, args)
    journal = journal or ActionLog(os.environ.get('PROTON_ACTION_LOG', '/var/lib/proton-mail/actions/actions.jsonl'))
    if name == 'list_actions':
        result = journal.recent(**args)
        audit_headers(journal, name, result)
        return result
    client = factory()
    try:
        mail = Mail(client, client.bridge_username)
        if name == 'list_folders':
            result = mail.folders()
        elif name in ('search_messages', 'list_messages'):
            result = mail.search(**args)
        elif name == 'list_unsubscribe':
            result = mail.unsubscribe(**args)
        elif name == 'read_messages':
            return mail.read_messages(journal, **args)
        elif name in ('archive_messages', 'move_messages', 'add_labels', 'mark_read'):
            return mail.cleanup(journal, name, **args)
        elif name == 'create_draft':
            entry = {'action_id': str(uuid4()), 'tool': name, 'folder': 'Drafts', 'uids': [],
                     'subject': args['subject'], 'sender': client.bridge_username,
                     'destination': 'Drafts'}
            journal.append({**entry, 'status': 'attempting'})
            try:
                result = mail.draft(**args)
                journal.append({**entry, 'folder': result['folder'], 'destination': result['folder'],
                                'status': 'completed', 'messageId': result['messageId']})
                return result
            except Exception:
                try:
                    journal.append({**entry, 'status': 'uncertain'})
                except Exception:
                    pass
                raise
        else:
            raise PublicError('Unknown tool')
        audit_headers(journal, name, result, args.get('folder'))
        return result
    finally:
        # LOGOUT, never CLOSE (which can expunge); no persistent client state.
        try:
            client.logout()
        except Exception:
            pass


class BoundedHTTPServer(http.server.ThreadingHTTPServer):
    """Keep HTTP responsive without admitting parallel mail operations."""
    daemon_threads = True

    def __init__(self, *args, **kwargs):
        self.mail_slot = threading.BoundedSemaphore(1)
        self._http_slots = threading.BoundedSemaphore(8)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self._http_slots.acquire(blocking=False):
            try:
                request.settimeout(1)
                request.sendall(b'HTTP/1.1 503 Service Unavailable\r\n'
                                b'Content-Length: 0\r\nRetry-After: 5\r\n'
                                b'Connection: close\r\n\r\n')
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._http_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._http_slots.release()


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = 'FleetMail/1'

    def log_message(self, *_):
        pass  # No request, mail, credential, or exception logging.

    def setup(self):
        super().setup()
        self.connection.settimeout(25)

    def reply(self, status, value=None):
        data = json.dumps(value).encode() if value is not None else b''
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def parse_request(self):
        if not super().parse_request():
            return False
        verifier = getattr(self.server, 'access', None)
        if verifier is not None and not verifier.authorized(self.headers.get_all('Cf-Access-Jwt-Assertion', [])):
            self.reply(401, {'error': 'Invalid or missing Access assertion'})
            return False
        hosts = self.headers.get_all('Host', [])
        origins = self.headers.get_all('Origin', [])
        allowed_hosts = ('127.0.0.1:8766', 'localhost:8766')
        allowed_origins = ('http://127.0.0.1:8766', 'http://localhost:8766')
        if verifier is not None:
            # Public routing is accepted only after a configured verifier passes.
            public_host = os.environ.get('PROTON_PUBLIC_HOST', '').strip()
            if public_host:
                allowed_hosts += (public_host,)
            if public_host:
                allowed_origins += ('https://' + public_host,)
        if (len(hosts) != 1 or hosts[0] not in allowed_hosts
                or len(origins) > 1 or (origins and origins[0] not in allowed_origins)):
            self.reply(403)
            return False
        return True

    def do_GET(self):
        self.reply(405)  # No unsolicited SSE stream.

    do_DELETE = do_GET

    def do_POST(self):
        if self.path != '/mcp':
            return self.reply(404)
        if self.headers.get('Transfer-Encoding') or self.headers.get_content_type() != 'application/json':
            return self.reply(415)
        accept = self.headers.get('Accept', '')
        if 'application/json' not in accept or 'text/event-stream' not in accept:
            return self.reply(406)
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= MAX_REQUEST:
                return self.reply(413)
            request = json.loads(self.rfile.read(length))
            if not isinstance(request, dict) or request.get('jsonrpc') != '2.0' or not isinstance(request.get('method'), str):
                raise PublicError()
            ident = request.get('id')
            if 'id' in request and (type(ident) not in (int, str)):
                raise PublicError()
            method = request['method']
            params = request.get('params', {})
            if not isinstance(params, dict):
                raise PublicError()
        except (ValueError, UnicodeError):
            return self.reply(400, {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'Invalid request'}})
        if 'id' not in request:
            return self.reply(202)  # Notifications cannot invoke tools.
        result = None
        error = None
        if method == 'initialize':
            proposed = params.get('protocolVersion')
            result = {'protocolVersion': proposed if proposed in PROTOCOLS else PROTOCOLS[-1],
                      'capabilities': {'tools': {}}, 'serverInfo': {'name': 'fleet-proton-mail', 'version': '1.0.0'}}
        elif self.headers.get('MCP-Protocol-Version', PROTOCOLS[0]) not in PROTOCOLS:
            return self.reply(400)
        elif method == 'ping':
            result = {}
        elif method == 'tools/list':
            result = {'tools': TOOLS}
        elif method == 'tools/call':
            try:
                if not self.server.mail_slot.acquire(blocking=False):
                    raise PublicError('Mail service busy; retry serially after the current call finishes.')
                try:
                    value = call(params.get('name'), params.get('arguments', {}))
                finally:
                    self.server.mail_slot.release()
                result = {'content': [{'type': 'text', 'text': json.dumps(value)}]}
            except PublicError as refusal:
                result = {'isError': True, 'content': [{'type': 'text', 'text': str(refusal)}]}
            except Exception:
                result = {'isError': True, 'content': [{'type': 'text', 'text': 'Mail operation failed. Check configuration or limits. For a draft, inspect Drafts before retrying.'}]}
        else:
            error = {'code': -32601, 'message': 'Method not found'}
        self.reply(200, {'jsonrpc': '2.0', 'id': ident, **({'error': error} if error else {'result': result})})


def create_server():
    # Validate configuration before binding; always loopback, including OAuth mode.
    read_credentials()
    verifier = configured_access()
    server = BoundedHTTPServer(('127.0.0.1', 8766), Handler)
    server.access = verifier
    return server


if __name__ == '__main__':
    try:
        create_server().serve_forever()
    except RuntimeError as error:
        raise SystemExit(str(error)) from None
