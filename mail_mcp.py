#!/usr/bin/env python3
"""Local MCP: separate header/body tools and logged cleanup, draft APPEND."""
import email.policy
from contextlib import contextmanager
from email.parser import BytesParser
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
import http.server
import imaplib
import json
import logging
import os
from pathlib import Path
import re
import ssl
import socket
import threading
import time
import signal
from contextlib import nullcontext
from action_log import ActionLog, PublicError
from uuid import uuid4
from access import configured_access

MAX_MESSAGE = 2 * 1024 * 1024
MAX_REQUEST = 128 * 1024
SEARCH_TIMEOUT_SECONDS = 45
PROTOCOLS = ('2025-03-26', '2025-06-18', '2025-11-25')


_local = threading.local()


def setting(name, default):
    value = float(os.environ.get(name, default))
    if not 0 < value < 86400:
        raise PublicError('Invalid positive service limit')
    return value


@contextmanager
def phase(name):
    start = time.monotonic()
    try:
        yield
    finally:
        metrics = getattr(_local, 'metrics', None)
        if metrics is not None:
            metrics[name] += time.monotonic() - start


class WriteDeadline:
    def __init__(self):
        self.expired = threading.Event()
        self.client = None
        self.ends = time.monotonic() + setting('PROTON_WRITE_TIMEOUT_SECONDS', 120)
        self.timer = threading.Timer(setting('PROTON_WRITE_TIMEOUT_SECONDS', 120), self.interrupt)
        self.timer.daemon = True

    def interrupt(self):
        self.expired.set()
        try:
            self.client.sock.shutdown(socket.SHUT_RDWR)
        except (OSError, AttributeError):
            pass

    def check(self):
        if self.expired.is_set():
            raise PublicError('Write timed out; inspect completed chunks and action history before retrying.')

    def __enter__(self):
        self.timer.start()
        _local.deadline = self
        return self

    def __exit__(self, *args):
        self.timer.cancel()
        self.timer.join()
        _local.deadline = None


def check_deadline():
    deadline = getattr(_local, 'deadline', None)
    if deadline:
        deadline.check()


class TimedClient:
    def __init__(self, client):
        self.client = client

    def __getattr__(self, name):
        value = getattr(self.client, name)
        if name not in ('select', 'uid', 'append'):
            return value
        def invoke(*args, **kwargs):
            check_deadline()
            label = 'mutate' if name == 'append' else 'select' if name == 'select' else {
                'SEARCH': 'search', 'FETCH': 'fetch'}.get(args[0], 'mutate')
            with phase(label):
                result = value(*args, **kwargs)
            check_deadline()
            return result
        return invoke


class TimedJournal:
    def __init__(self, journal):
        self.journal = journal

    def append(self, entry):
        with phase('audit'):
            return self.journal.append(entry)

    def __getattr__(self, name):
        return getattr(self.journal, name)


def expand_uid_set(value):
    result = []
    for part in value.split(','):
        bounds = part.split(':')
        if len(bounds) > 2 or any(not re.fullmatch(r'[1-9][0-9]{0,9}', b) for b in bounds):
            raise PublicError('Invalid COPYUID mapping')
        first, last = int(bounds[0]), int(bounds[-1])
        if abs(last - first) > 200 or len(result) + abs(last - first) + 1 > 200:
            raise PublicError('COPYUID mapping exceeds call limit')
        result.extend(str(n) for n in range(first, last + (1 if last >= first else -1), 1 if last >= first else -1))
    if len(set(result)) != len(result):
        raise PublicError('Duplicate COPYUID mapping')
    return result


def copy_mapping(values, expected):
    if len(values) != 1:
        raise PublicError('Archive COPYUID unavailable; use explicit Archive identities')
    parts = values[0].split()
    if len(parts) != 3 or not re.fullmatch(r'[1-9][0-9]{0,9}', parts[0]):
        raise PublicError('Invalid COPYUID mapping')
    source, target = expand_uid_set(parts[1]), expand_uid_set(parts[2])
    if set(source) != set(expected) or len(source) != len(target):
        raise PublicError('Incomplete COPYUID mapping')
    return parts[0], dict(zip(source, target))


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


@contextmanager
def search_deadline(client):
    """Interrupt the transport even if untagged IMAP progress keeps arriving."""
    expired = threading.Event()

    def interrupt():
        expired.set()
        try:
            # imaplib.shutdown() closes its buffered reader first and can wait
            # for the blocked read's lock. Shutdown the socket first instead.
            client.sock.shutdown(socket.SHUT_RDWR)
        except (OSError, AttributeError):
            pass

    timer = threading.Timer(SEARCH_TIMEOUT_SECONDS, interrupt)
    timer.daemon = True
    timer.start()
    try:
        try:
            yield
        except TimeoutError:
            if not expired.is_set():
                raise PublicError('Mail search timed out waiting for Bridge. Use INBOX or a narrower folder/date range.') from None
        except Exception:
            if not expired.is_set():
                raise
        if expired.is_set():
            raise PublicError('Mail search timed out after ' + str(SEARCH_TIMEOUT_SECONDS)
                              + 's. Use INBOX or a narrower folder/date range; All Mail may scan the whole mailbox.')
    finally:
        timer.cancel()
        timer.join()  # No watchdog survives the operation or races the next call.


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

    def select(self, folder, validity=None, readonly=True):
        ok(self.client.select(quote(folder), readonly=readonly))  # Reads always EXAMINE; cleanup has its own SELECT.
        _, data = self.client.response('UIDVALIDITY')
        current = data[0].decode('ascii') if data and data[0] else None
        if current is None or (validity is not None and current != validity):
            raise PublicError('Mailbox UIDVALIDITY changed; search again')
        return current

    HEADER_FIELDS = ('From To Cc Subject Date Message-ID References In-Reply-To '
                     'List-Unsubscribe List-Unsubscribe-Post X-Pm-Internal-Id')

    def metadata(self, folder, uid, uidvalidity):
        self.select(folder, uidvalidity)
        return self.metadata_batch(folder, [uid], uidvalidity)[0]

    def metadata_batch(self, folder, uids, uidvalidity):
        if not uids:
            return []
        data = ok(self.client.uid('FETCH', ','.join(uids),
                  '(UID FLAGS RFC822.SIZE BODY.PEEK[HEADER.FIELDS (' + self.HEADER_FIELDS + ')]<0.65537>)'))
        found = {}
        for index, row in enumerate(data):
            if not isinstance(row, tuple):
                continue
            attributes, raw = row
            if len(raw) > 65536:
                raise PublicError('Headers too large')
            # imaplib may return attributes following a literal as separate bytes.
            for tail in data[index + 1:]:
                if isinstance(tail, tuple):
                    break
                if isinstance(tail, bytes):
                    attributes += b' ' + tail
            identity = re.search(rb'UID (\d+)', attributes)
            if not identity or identity[1].decode() not in uids or identity[1].decode() in found:
                raise PublicError('Unexpected message identity')
            uid = identity[1].decode()
            found[uid] = self.parse_metadata(folder, uid, uidvalidity, attributes, raw)
        if set(found) != set(uids):
            raise PublicError('Message missing; search again')
        metrics = getattr(_local, 'metrics', None)
        if metrics is not None:
            metrics['matched'] += len(found)
            if not metrics['requested']:
                metrics['requested'] = len(uids)
        return [found[uid] for uid in uids]

    def parse_metadata(self, folder, uid, uidvalidity, attributes, raw):
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
        with search_deadline(self.client):
            return self._search(folder, sender, recipient, subject, since, before, unread, label, limit)

    def _search(self, folder, sender, recipient, subject, since, before, unread, label, limit):
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
                'messages': self.metadata_batch(folder, [u.decode() for u in uids[-limit:]], validity),
                'truncated': len(uids) > limit}

    def message(self, journal, folder, uid, uidvalidity, metadata=None):
        metadata = metadata or self.metadata(folder, uid, uidvalidity)
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
        groups = {}
        for ref in messages:
            groups.setdefault((ref['folder'], ref['uidvalidity']), []).append(ref)
        results = {}
        for (folder, validity), refs in groups.items():
            self.select(folder, validity)
            items = self.metadata_batch(folder, sorted({r['uid'] for r in refs}, key=int), validity)
            for item in items:
                results[(folder, validity, item['uid'])] = self.message(
                    journal, folder, item['uid'], validity, item)
        return {'messages': [results[(r['folder'], r['uidvalidity'], r['uid'])] for r in messages]}

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
                 (tool_name == 'unarchive_messages' and target == 'INBOX') or
                 (tool_name == 'move_messages' and target.startswith('Folders/')) or
                 (tool_name == 'add_labels' and target.startswith('Labels/')))
        if not valid or any(part.casefold() in ('trash', 'spam', 'junk', 'starred', 'drafts', 'sent', 'all mail')
                            for part in target.split('/')):
            raise PublicError('Forbidden destination')
        if tool_name in ('archive_messages', 'unarchive_messages', 'move_messages') and b'MOVE' not in self.client.capabilities:
            raise PublicError('Bridge lacks RFC 6851 MOVE; moves disabled, no fallback')
        return target

    def unsubscribe(self, folder, uidvalidity, uids):
        self.select(folder, uidvalidity)
        return {'messages': [{'uid': item['uid'], 'folder': folder, 'uidvalidity': uidvalidity,
                              'subject': item['headers']['Subject'], 'sender': item['headers']['From'],
                              'List-Unsubscribe': item['headers']['List-Unsubscribe'],
                              'List-Unsubscribe-Post': item['headers']['List-Unsubscribe-Post']}
                             for item in self.metadata_batch(folder, sorted(set(uids), key=int), uidvalidity)],
                'handling': 'Manual only; no links opened, HTTP requests or mail sent.'}

    def unarchive(self, journal, folder='Archive', uidvalidity=None, uids=None,
                  action_id=None, destination='INBOX', dry_run=False):
        if folder != 'Archive' or destination != 'INBOX':
            raise PublicError('Unarchive requires Archive source and INBOX destination')
        if action_id:
            if uids is not None or uidvalidity is not None:
                raise PublicError('Use either action_id or explicit Archive identities')
            records = journal.action(action_id)
            intents = {r.get('chunk', 0): r for r in records if r.get('status') == 'attempting'}
            results = {r.get('chunk', 0): r for r in records if r.get('status') == 'completed'}
            expected_chunks = set(range(next(iter(intents.values())).get('chunk_count', 1))) if intents else set()
            if not intents or set(intents) != expected_chunks or set(intents) != set(results) or any(
                    r.get('tool') != 'archive_messages' or r.get('destination') != 'Archive'
                    or r.get('status') == 'uncertain' for r in records):
                raise PublicError('Archive action incomplete or uncertain; use explicit Archive identities')
            uids = []
            validities = set()
            for record in results.values():
                validity, mapping = copy_mapping(record.get('copyuid', []), record['uids'])
                validities.add(validity)
                uids.extend(mapping.values())
            if len(validities) != 1 or len(uids) > 200 or len(set(uids)) != len(uids):
                raise PublicError('Inconsistent Archive identities')
            uidvalidity = validities.pop()
        if not uids or not uidvalidity:
            raise PublicError('Provide Archive UIDs and UIDVALIDITY or archive action_id')
        return self.cleanup(journal, 'unarchive_messages', folder, uidvalidity, uids, destination, dry_run)

    def cleanup(self, journal, tool_name, folder, uidvalidity, uids, destination='', dry_run=False):
        if tool_name == 'archive_messages':
            destination = 'Archive'
        if folder == 'All Mail' and tool_name in ('archive_messages', 'unarchive_messages', 'move_messages'):
            raise PublicError('Moves from All Mail are forbidden; use a concrete mailbox')
        target = self.destination(tool_name, destination)
        self.select(folder, uidvalidity, readonly=dry_run)
        uids = sorted(set(uids), key=int)
        messages = self.metadata_batch(folder, uids, uidvalidity)
        changes = [{'uids': [item['uid']], 'subject': item['headers']['Subject'],
                    'sender': item['headers']['From'], 'destination': target} for item in messages]
        action_id = str(uuid4())
        configured_chunk = setting('PROTON_IMAP_CHUNK_SIZE', 50)
        chunk_size = int(configured_chunk)
        if chunk_size < 1 or chunk_size != configured_chunk:
            raise PublicError('Invalid chunk size')
        completed = []
        completed_chunks = []
        for index, start in enumerate(range(0, len(uids), chunk_size)):
            chunk = uids[start:start + chunk_size]
            entry = {'action_id': action_id, 'chunk': index, 'chunk_count': (len(uids) + chunk_size - 1) // chunk_size, 'tool': tool_name, 'folder': folder,
                     'uidvalidity': uidvalidity, 'uids': chunk, 'destination': target}
            metrics = getattr(_local, 'metrics', None)
            if metrics is not None:
                metrics['chunks'] += 1
            def stopped(uncertain, message):
                return {'action_id': action_id, 'completed': completed, 'completed_chunks': completed_chunks,
                        'uncertain': uncertain, 'uncertain_uids': chunk if uncertain else [],
                        'uid': chunk[0], 'stopped': True, 'dry_run': dry_run,
                        'timed_out': bool(getattr(_local, 'deadline', None) and _local.deadline.expired.is_set()),
                        'message': message}
            try:
                check_deadline()
                journal.append({**entry, 'status': 'dry_run' if dry_run else 'attempting',
                                'outcomes': {uid: 'dry_run' if dry_run else 'attempting' for uid in chunk}})
            except Exception:
                return stopped(False, 'Deadline expired or action log unavailable; no command attempted for this chunk.')
            if dry_run:
                continue
            try:
                check_deadline()
                uid_set = ','.join(chunk)
                if tool_name in ('archive_messages', 'unarchive_messages', 'move_messages'):
                    ok(self.client.uid('MOVE', uid_set, quote(target)))
                elif tool_name == 'add_labels':
                    ok(self.client.uid('COPY', uid_set, quote(target)))
                elif tool_name == 'mark_read':
                    ok(self.client.uid('STORE', uid_set, '+FLAGS.SILENT', '(\\Seen)'))
                else:
                    raise PublicError('Unknown cleanup tool')
                _, copied = self.client.response('COPYUID')
                values = [v.decode('ascii') for v in copied if v] if copied else []
                mapping = copy_mapping(values, chunk)[1] if values and tool_name != 'mark_read' else {}
                journal.append({**entry, 'status': 'completed', 'copyuid': values, 'mapping': mapping,
                                'outcomes': {uid: 'completed' for uid in chunk}})
                completed.extend(chunk)
                completed_chunks.append(index)
            except Exception:
                try:
                    journal.append({**entry, 'status': 'uncertain',
                                    'outcomes': {uid: 'uncertain' for uid in chunk}})
                except Exception:
                    pass
                return stopped(True, 'Stopped after an uncertain result or timeout. Inspect mail and list_actions; never retry blindly.')
        return {'action_id': action_id, 'completed': completed, 'completed_chunks': completed_chunks,
                'dry_run': dry_run, 'uncertain': False,
                **({'tool': tool_name, 'folder': folder, 'uidvalidity': uidvalidity, 'changes': changes,
                    'note': 'Preview only; chat approval is the assistant responsibility, not enforced by this server.'} if dry_run else {})}

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
         tool('list_messages', 'HEADERS ONLY: list message headers, flags, size; no bodies, snippets or attachment names. web_url opens that message in Proton web (grouping disabled, matching signed-in account), or is null. Supply folder or label. All Mail requires since and before; use a short date range. Search network work has a 45s deadline; date bounds and limit do not guarantee fast All Mail scans.', SEARCH, []),
         tool('search_messages', 'HEADERS ONLY: sender/recipient/subject/date/unread search in a folder or label mailbox. Never searches/returns bodies. web_url opens that message in Proton web (grouping disabled, matching signed-in account), or is null. All Mail requires since and before; use a short date range. Search network work has a 45s deadline; date bounds and limit do not guarantee fast All Mail scans.', SEARCH, []),
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
         tool('unarchive_messages', 'MAILBOX WRITE: move only Archive to INBOX; never sends or permanently deletes. Use Archive UIDs/UIDVALIDITY or a previous archive action_id. Same dry_run default as archive.',
              {**CLEANUP, 'action_id': S, 'destination': S}, [], False),
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
    deadline = getattr(_local, 'deadline', None)
    class DeadlineIMAP(imaplib.IMAP4):
        def _create_socket(self, timeout):
            # Attach before the constructor reads Bridge's greeting/capabilities.
            self.sock = super()._create_socket(timeout)
            if deadline:
                deadline.client = self
                if deadline.expired.is_set():
                    self.sock.close()
                    deadline.check()
            return self.sock
    timeout = min(20, max(.001, deadline.ends - time.monotonic())) if deadline else 20
    client = DeadlineIMAP('127.0.0.1', 1143, timeout=timeout)
    try:
        deadline = getattr(_local, 'deadline', None)
        if deadline:
            deadline.client = client
            deadline.check()
        client.starttls(context)
        with phase('login'):
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


def _call(name, args, factory=connect, journal=None):
    validate(name, args)
    journal = TimedJournal(journal or ActionLog(os.environ.get('PROTON_ACTION_LOG', '/var/lib/proton-mail/actions/actions.jsonl')))
    if name == 'list_actions':
        result = journal.recent(**args)
        audit_headers(journal, name, result)
        return result
    login_before = _local.metrics['login']
    with phase('connect'):
        client = TimedClient(factory())
    _local.metrics['connect'] -= _local.metrics['login'] - login_before
    deadline = getattr(_local, 'deadline', None)
    if deadline:
        deadline.client = client
    try:
        check_deadline()
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
        elif name == 'unarchive_messages':
            return mail.unarchive(journal, **args)
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
            try:
                client.shutdown()  # LOGOUT can raise before imaplib closes an interrupted socket.
            except Exception:
                pass


def call(name, args, factory=connect, journal=None):
    metrics = dict.fromkeys(('connect', 'login', 'select', 'search', 'fetch', 'mutate', 'audit',
                             'requested', 'matched', 'chunks'), 0)
    _local.metrics = metrics
    outcome = 'error'
    try:
        validate(name, args)
        metrics['requested'] = len(args.get('uids', args.get('messages', [])))
        write = name in ('archive_messages', 'unarchive_messages', 'move_messages', 'add_labels', 'mark_read', 'create_draft')
        with WriteDeadline() if write else nullcontext():
            try:
                result = _call(name, args, factory, journal)
            except Exception:
                deadline = getattr(_local, 'deadline', None)
                if deadline and deadline.expired.is_set():
                    result = {'timed_out': True, 'completed': [], 'completed_chunks': [],
                              'uncertain': name == 'create_draft', 'stopped': True,
                              'message': ('Write timed out; inspect Drafts and action history before retrying.'
                                          if name == 'create_draft' else 'Write timed out before mutations; no chunks completed.')}
                else:
                    raise
            outcome = 'completed'
            if isinstance(result, dict):
                for key, label in (('stopped', 'stopped'), ('uncertain', 'uncertain'), ('timed_out', 'timeout')):
                    if result.get(key):
                        outcome = label
            return result
    except PublicError as error:
        if 'timed out' in str(error):
            outcome = 'timeout'
        raise
    finally:
        # Only allowlisted fields and tool names; never serialize arguments/exceptions.
        safe_name = name if isinstance(name, str) and name in {t['name'] for t in TOOLS} else 'unknown'
        logging.warning('mail_call %s', json.dumps({'tool': safe_name, 'outcome': outcome,
                        **{k: round(v, 6) for k, v in metrics.items()}}))
        _local.metrics = None


class BoundedHTTPServer(http.server.ThreadingHTTPServer):
    """Keep HTTP responsive without admitting parallel mail operations."""
    daemon_threads = True

    def __init__(self, *args, **kwargs):
        self.draining = threading.Event()
        self.drain_done = threading.Event()
        self.mail_slot = threading.BoundedSemaphore(1)
        self._http_slots = threading.BoundedSemaphore(8)
        self._requests = threading.Condition()
        self._active_requests = 0
        super().__init__(*args, **kwargs)

    def drain(self):
        self.draining.set()
        end = time.monotonic() + setting('PROTON_DRAIN_TIMEOUT_SECONDS', 60)
        try:
            self.shutdown()
            acquired = self.mail_slot.acquire(timeout=max(0, end - time.monotonic()))
            if acquired:
                self.mail_slot.release()
            with self._requests:
                finished = self._requests.wait_for(lambda: self._active_requests == 0,
                                                   timeout=max(0, end - time.monotonic()))
            self.server_close()
            return acquired and finished
        finally:
            self.drain_done.set()

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
        with self._requests:
            self._active_requests += 1
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._http_slots.release()
            with self._requests:
                self._active_requests -= 1
                self._requests.notify_all()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._http_slots.release()
            with self._requests:
                self._active_requests -= 1
                self._requests.notify_all()


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
                    if self.server.draining.is_set():
                        raise PublicError('Mail service shutting down; new calls refused.')
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
        server = create_server()
        def terminate(*_):
            if server.draining.is_set():
                return
            server.draining.set()
            threading.Thread(target=server.drain, daemon=True).start()
        signal.signal(signal.SIGTERM, terminate)
        server.serve_forever()
        server.drain_done.wait()
    except RuntimeError as error:
        raise SystemExit(str(error)) from None
