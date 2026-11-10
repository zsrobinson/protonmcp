"""All mail below is generated synthetic content; no captured mail or live I/O."""
from contextlib import contextmanager
from email.message import EmailMessage
from imaplib import IMAP4 as WireIMAP
import http.client
import json
import socket
import threading
from unittest.mock import Mock

import pytest
import mail_mcp as mail
from action_log import ActionLog, PublicError


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    monkeypatch.setenv('PROTON_ACTION_LOG', str(tmp_path / 'actions' / 'audit.jsonl'))
    monkeypatch.setattr(mail.imaplib, 'IMAP4', Mock(side_effect=AssertionError('Live IMAP forbidden')))
    monkeypatch.setattr(socket, 'create_connection', Mock(side_effect=AssertionError('External network forbidden')))


def generated_message():
    message = EmailMessage()
    message['From'] = 'sender@example.invalid'
    message['To'] = 'recipient@example.invalid'
    message['Subject'] = 'Synthetic fixture'
    message['X-Pm-Internal-Id'] = 'synthetic-id=='
    message.set_content('Synthetic body created for tests.')
    return message


class FakeIMAP:
    capabilities = (b'MOVE',)
    bridge_username = 'sender@example.invalid'

    def __init__(self):
        self.commands = []
        self.raw = generated_message().as_bytes()
        self.logged_out = False

    def list(self):
        return 'OK', [b'(\\Inbox) "/" "INBOX"', b'(\\Archive) "/" "Archive"',
                      b'(\\Drafts) "/" "Drafts"', b'() "/" "Folders/Test"',
                      b'() "/" "Labels/Test"', b'(\\Trash) "/" "Trash"']

    def select(self, folder, readonly=True):
        self.commands.append(('SELECT', folder, readonly))
        return 'OK', [b'1']

    def response(self, name):
        return name, [b'1'] if name == 'UIDVALIDITY' else []

    def uid(self, command, *args):
        self.commands.append((command, *args))
        if command == 'FETCH':
            return 'OK', [(b'1 (UID 1 FLAGS () RFC822.SIZE 200)', self.raw)]
        if command == 'SEARCH':
            return 'OK', [b'1']
        if command in ('MOVE', 'COPY', 'STORE'):
            return 'OK', []
        raise AssertionError('Forbidden IMAP command')

    def append(self, folder, flags, timestamp, raw):
        self.commands.append(('APPEND', folder, flags, raw))
        return 'OK', []

    def logout(self):
        self.logged_out = True


def test_existing_tool_surface_preserved():
    assert {t['name'] for t in mail.TOOLS} == {
        'list_folders', 'list_messages', 'search_messages', 'list_unsubscribe',
        'read_messages', 'create_draft', 'archive_messages', 'move_messages',
        'add_labels', 'mark_read', 'list_actions', 'unarchive_messages'}


@pytest.mark.parametrize('name,target,command', [
    ('archive_messages', '', 'MOVE'), ('move_messages', 'Folders/Test', 'MOVE'),
    ('add_labels', 'Labels/Test', 'COPY'), ('mark_read', '', 'STORE')])
@pytest.mark.parametrize('dry_run', [False, True])
def test_cleanup_remains_enabled_and_bounded(name, target, command, dry_run, tmp_path):
    client = FakeIMAP()
    args = dict(folder='INBOX', uidvalidity='1', uids=['1'], dry_run=dry_run)
    if name in ('move_messages', 'add_labels'):
        args['destination'] = target
    result = mail.call(name, args, factory=lambda: client)
    mutations = [c for c in client.commands if c[0] in ('MOVE', 'COPY', 'STORE')]
    assert len(mutations) == (0 if dry_run else 1)
    if mutations:
        assert mutations[0][0] == command
        if command == 'STORE':
            assert mutations[0][2:] == ('+FLAGS.SILENT', '(\\Seen)')
    assert result['dry_run'] == dry_run
    assert client.logged_out
    records = ActionLog(tmp_path / 'actions' / 'audit.jsonl').recent()['entries']
    assert records[-1]['status'] == ('dry_run' if dry_run else 'completed')
    assert all('body' not in record for record in records)


@pytest.mark.parametrize('name,target', [('move_messages', 'Trash'), ('move_messages', 'Drafts'),
                                        ('add_labels', 'Folders/Test')])
def test_forbidden_destinations(name, target):
    with pytest.raises(PublicError):
        mail.Mail(FakeIMAP()).destination(name, target)


def test_move_requires_move_capability():
    client = FakeIMAP()
    client.capabilities = ()
    with pytest.raises(PublicError):
        mail.Mail(client).destination('archive_messages', 'Archive')
    assert not client.commands


def test_draft_appends_only_to_drafts():
    client = FakeIMAP()
    result = mail.call('create_draft', dict(to='recipient@example.invalid', subject='Synthetic fixture',
                                          body='Synthetic draft.'), factory=lambda: client)
    assert result['saved'] and result['folder'] == 'Drafts'
    assert [c[0] for c in client.commands] == ['APPEND']
    assert client.commands[0][1:3] == ('"Drafts"', '(\\Draft)')
    assert client.logged_out


@pytest.mark.parametrize('name', ['send_mail', 'delete_messages', 'expunge'])
def test_no_send_or_permanent_delete_tool(name):
    factory = Mock()
    with pytest.raises(PublicError):
        mail.call(name, {}, factory=factory)
    factory.assert_not_called()


def test_stale_identity_refuses_before_mutation():
    client = FakeIMAP()
    with pytest.raises(PublicError):
        mail.call('archive_messages', dict(folder='INBOX', uidvalidity='2', uids=['1']), factory=lambda: client)
    assert all(c[0] not in ('MOVE', 'COPY', 'STORE') for c in client.commands)


def test_all_mail_requires_bounded_dates():
    client = FakeIMAP()
    with pytest.raises(PublicError):
        mail.Mail(client).search(folder='All Mail')
    assert not client.commands
    result = mail.Mail(client).search(folder='All Mail', since='01-Jan-2026', before='02-Jan-2026')
    assert result['messages'][0]['web_url'] == 'https://mail.proton.me/all-mail/synthetic-id=='
    fetches = [c for c in client.commands if c[0] == 'FETCH']
    assert all('BODY.PEEK[HEADER.FIELDS' in c[2] for c in fetches)


def test_body_read_peeks_and_returns_web_url():
    client = FakeIMAP()
    result = mail.call('read_messages', {'messages': [{'folder': 'INBOX', 'uid': '1', 'uidvalidity': '1'}]},
                       factory=lambda: client)
    assert result['messages'][0]['body'].strip() == 'Synthetic body created for tests.'
    assert result['messages'][0]['web_url'].endswith('/synthetic-id==')
    assert all('BODY.PEEK[' in c[2] for c in client.commands if c[0] == 'FETCH')


@pytest.mark.parametrize('value', ['', '../bad', 'bad/id', 'synthetic-id=='])
def test_web_url_validates_opaque_id(value):
    message = EmailMessage()
    message['X-Pm-Internal-Id'] = value
    result = mail.message_web_url(message)
    assert (result is not None) == (value == 'synthetic-id==')


def test_busy_call_does_not_block_ping(monkeypatch, caplog):
    # Real loopback transport with mocked mail, no credential or external requests.
    monkeypatch.undo()
    started, release = threading.Event(), threading.Event()
    def operation(*args):
        started.set()
        assert release.wait(5)
        return {}
    monkeypatch.setattr(mail, 'call', operation)
    server = mail.BoundedHTTPServer(('127.0.0.1', 0), mail.Handler)
    server.access = None
    worker = threading.Thread(target=server.serve_forever)
    worker.start()
    def request(method):
        conn = http.client.HTTPConnection(*server.server_address, timeout=3)
        body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method,
                           'params': {'name': 'list_folders', 'arguments': {}}})
        conn.request('POST', '/mcp', body, headers={'Host': '127.0.0.1:8766',
                     'Accept': 'application/json, text/event-stream', 'Content-Type': 'application/json'})
        response = conn.getresponse()
        data = json.loads(response.read())
        conn.close()
        return data
    first = threading.Thread(target=request, args=('tools/call',))
    try:
        first.start()
        assert started.wait(3)
        assert request('ping')['result'] == {}
        second = request('tools/call')['result']
        assert second['isError'] and 'busy' in second['content'][0]['text']
        release.set()
        first.join()
        server.draining.set()
        refused = request('tools/call')['result']
        assert refused['isError'] and 'shutting down' in refused['content'][0]['text']
        records = [json.loads(r.message.removeprefix('mail_call ')) for r in caplog.records
                   if r.message.startswith('mail_call ')]
        refusals = [r for r in records if r['error_class'] in ('busy', 'draining')]
        assert len(refusals) == 2
        for record in refusals:
            assert_telemetry(record)
            assert record['outcome'] == 'refused'
            assert record['req_bytes'] > 0 and record['resp_bytes'] > 0
    finally:
        release.set()
        first.join()
        server.shutdown()
        worker.join()
        server.server_close()


def test_search_deadline_interrupts_transport(monkeypatch):
    client = FakeIMAP()
    client.sock = Mock()
    monkeypatch.setattr(mail, 'SEARCH_TIMEOUT_SECONDS', 0.01)
    def slow_search(*args):
        assert client.sock.shutdown.called is False
        threading.Event().wait(0.05)
        return 'OK', [b'1']
    client.uid = slow_search
    with pytest.raises(PublicError, match='timed out'):
        mail.Mail(client).search(folder='INBOX')
    client.sock.shutdown.assert_called_once_with(socket.SHUT_RDWR)


def test_search_read_timeout_is_controlled(monkeypatch):
    client = FakeIMAP()
    client.uid = Mock(side_effect=TimeoutError())
    with pytest.raises(PublicError, match='timed out waiting for Bridge'):
        mail.Mail(client).search(folder='INBOX')


class BatchIMAP(FakeIMAP):
    def __init__(self, count=120):
        super().__init__()
        self.uids = [str(i) for i in range(1, count + 1)]
        self.copied = []
        self.sock = Mock()
        self.fail_chunk = None

    def uid(self, command, *args):
        self.commands.append((command, *args))
        if command == 'SEARCH':
            return 'OK', [' '.join(self.uids).encode()]
        if command == 'FETCH':
            return 'OK', [(f'{i} (UID {uid} FLAGS () RFC822.SIZE 200)'.encode(), self.raw)
                          for i, uid in enumerate(args[0].split(','), 1) if uid in self.uids]
        if command in ('MOVE', 'COPY', 'STORE'):
            if sum(c[0] == command for c in self.commands) == self.fail_chunk:
                return 'NO', [b'Failed']
            self.copied = [f'1 {args[0]} '.encode() + ','.join(str(int(u) + 1000) for u in args[0].split(',')).encode()]
            return 'OK', []
        raise AssertionError(command)

    def response(self, name):
        if name == 'COPYUID':
            values, self.copied = self.copied, []
            return name, values
        return super().response(name)


def archive_args(count=120):
    return {'folder': 'INBOX', 'uidvalidity': '1', 'uids': [str(i) for i in range(1, count + 1)]}


def test_archive_command_counts_and_chunk_journal(tmp_path):
    client = BatchIMAP()
    journal = ActionLog(tmp_path / 'private' / 'actions.jsonl')
    result = mail.call('archive_messages', archive_args(), factory=lambda: client, journal=journal)
    assert result['completed'] == client.uids
    assert result['completed_chunks'] == [0, 1, 2]
    assert [c[0] for c in client.commands].count('SELECT') == 1
    assert [c[0] for c in client.commands].count('FETCH') == 1
    assert [c[0] for c in client.commands].count('MOVE') == 3
    assert [len(c[1].split(',')) for c in client.commands if c[0] == 'MOVE'] == [50, 50, 20]
    records = journal.recent()['entries']
    assert len(records) == 6
    assert {r['action_id'] for r in records} == {result['action_id']}
    assert records[-1]['mapping']['120'] == '1120'
    assert records[-1]['outcomes']['120'] == 'completed'


@pytest.mark.parametrize('name', ['list_messages', 'search_messages'])
def test_search_fetches_only_slice_once(name):
    client = BatchIMAP()
    result = mail.call(name, {'folder': 'INBOX', 'limit': 3}, factory=lambda: client)
    assert [m['uid'] for m in result['messages']] == ['118', '119', '120']
    assert [c[0] for c in client.commands] == ['SELECT', 'SEARCH', 'FETCH']
    assert client.commands[-1][1] == '118,119,120'


@pytest.mark.parametrize('name,target,command', [('move_messages', 'Folders/Test', 'MOVE'),
    ('add_labels', 'Labels/Test', 'COPY'), ('mark_read', '', 'STORE')])
def test_all_cleanup_tools_batch(name, target, command, monkeypatch):
    monkeypatch.setenv('PROTON_IMAP_CHUNK_SIZE', '40')
    client = BatchIMAP()
    result = mail.call(name, {**archive_args(), 'destination': target} if target else archive_args(), factory=lambda: client)
    assert not result['uncertain']
    assert [c[0] for c in client.commands].count(command) == 3
    assert [c[0] for c in client.commands].count('FETCH') == 1
    assert [c[0] for c in client.commands].count('SELECT') == 1


def test_chunk_error_stops_and_marks_whole_chunk_uncertain(tmp_path):
    client = BatchIMAP()
    client.fail_chunk = 2
    journal = ActionLog(tmp_path / 'private' / 'actions.jsonl')
    result = mail.call('archive_messages', archive_args(), factory=lambda: client, journal=journal)
    assert result['completed'] == client.uids[:50]
    assert result['uncertain_uids'] == client.uids[50:100]
    assert [c[0] for c in client.commands].count('MOVE') == 2
    assert set(journal.recent()['entries'][-1]['outcomes'].values()) == {'uncertain'}


def test_write_deadline_reports_completed_chunks(monkeypatch):
    client = BatchIMAP()
    original = client.uid
    def slow_second(command, *args):
        if command == 'MOVE' and any(c[0] == 'MOVE' for c in client.commands):
            mail._local.deadline.interrupt()
        return original(command, *args)
    client.uid = slow_second
    monkeypatch.setenv('PROTON_WRITE_TIMEOUT_SECONDS', '5')
    result = mail.call('archive_messages', archive_args(), factory=lambda: client)
    assert result['timed_out'] and result['uncertain']
    assert result['completed_chunks'] == [0]
    assert result['completed'] == client.uids[:50]
    assert result['uncertain_uids'] == client.uids[50:100]
    client.sock.shutdown.assert_called_once_with(socket.SHUT_RDWR)


def test_write_deadline_during_preflight(monkeypatch):
    client = BatchIMAP()
    original = client.uid
    def slow_fetch(*args):
        threading.Event().wait(.04)
        return original(*args)
    client.uid = slow_fetch
    monkeypatch.setenv('PROTON_WRITE_TIMEOUT_SECONDS', '.01')
    result = mail.call('archive_messages', archive_args(), factory=lambda: client)
    assert result['timed_out'] and result['completed_chunks'] == []
    assert not result['uncertain']
    assert not any(c[0] == 'MOVE' for c in client.commands)


def test_copyuid_based_unarchive(tmp_path):
    journal = ActionLog(tmp_path / 'private' / 'actions.jsonl')
    archived = mail.call('archive_messages', archive_args(), factory=BatchIMAP, journal=journal)
    client = BatchIMAP()
    client.uids = [str(i + 1000) for i in range(1, 121)]
    result = mail.call('unarchive_messages', {'action_id': archived['action_id']}, factory=lambda: client, journal=journal)
    assert result['completed'] == client.uids
    assert [c[1:] for c in client.commands if c[0] == 'SELECT'] == [('"Archive"', False)]
    assert all(c[2] == '"INBOX"' for c in client.commands if c[0] == 'MOVE')
    assert [c[0] for c in client.commands].count('FETCH') == 1
    assert [c[0] for c in client.commands].count('MOVE') == 3


@pytest.mark.parametrize('extra', [{'folder': 'All Mail'}, {'folder': 'INBOX'}, {'destination': 'Folders/Test'}, {'destination': 'Archive'}])
def test_unarchive_rejects_other_mailboxes(extra):
    client = BatchIMAP()
    with pytest.raises(PublicError):
        mail.call('unarchive_messages', {'uids': ['1'], 'uidvalidity': '1', **extra}, factory=lambda: client)
    assert not client.commands


def test_unarchive_explicit_and_dry_run():
    client = BatchIMAP()
    result = mail.call('unarchive_messages', {'uids': ['1', '2'], 'uidvalidity': '1', 'dry_run': True}, factory=lambda: client)
    assert result['dry_run'] and not result['completed']
    assert [c[0] for c in client.commands] == ['SELECT', 'FETCH']
    assert client.commands[0] == ('SELECT', '"Archive"', True)


@pytest.mark.parametrize('records', [[], [dict(status='attempting', chunk=0)],
    [dict(status='attempting', chunk=0), dict(status='uncertain', chunk=0)]])
def test_unarchive_refuses_missing_or_uncertain_mapping(records):
    journal = Mock()
    journal.action.return_value = records
    client = BatchIMAP()
    with pytest.raises(PublicError):
        mail.call('unarchive_messages', {'action_id': 'synthetic-action'}, factory=lambda: client, journal=journal)
    assert not client.commands


def test_timing_logs_are_metadata_free(caplog):
    client = BatchIMAP()
    mail.call('archive_messages', archive_args(), factory=lambda: client)
    lines = [r.message for r in caplog.records if r.message.startswith('mail_call ')]
    assert len(lines) == 1
    record = json.loads(lines[0].removeprefix('mail_call '))
    assert record['requested'] == record['matched'] == 120 and record['chunks'] == 3
    assert record['outcome'] == 'ok'
    assert_telemetry(record)
    assert all(k in record['phase'] for k in mail.PHASES)
    for secret in ('Synthetic fixture', 'sender@example.invalid', 'recipient@example.invalid', 'synthetic-id=='):
        assert secret not in lines[0]


@pytest.mark.parametrize('finish', [True, False])
def test_drain_waits_for_inflight_up_to_budget(monkeypatch, finish):
    monkeypatch.undo()
    monkeypatch.setenv('PROTON_DRAIN_TIMEOUT_SECONDS', '.15')
    server = mail.BoundedHTTPServer(('127.0.0.1', 0), mail.Handler)
    assert server.mail_slot.acquire(False)
    worker = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01))
    worker.start()
    done = threading.Event()
    def drain():
        server.drain()
        done.set()
    draining = threading.Thread(target=drain)
    draining.start()
    try:
        assert server.draining.wait(1)
        assert not done.wait(.02)
        if finish:
            server.mail_slot.release()
        assert done.wait(1)
    finally:
        if not finish:
            server.mail_slot.release()
        draining.join()
        worker.join()


def test_missing_preflight_uid_prevents_all_mutations():
    client = BatchIMAP(119)
    with pytest.raises(PublicError, match='missing'):
        mail.call('archive_messages', archive_args(), factory=lambda: client)
    assert not any(c[0] == 'MOVE' for c in client.commands)


@pytest.mark.parametrize('fail_status,uncertain,moves', [('attempting', False, 0), ('completed', True, 1)])
def test_audit_failure_stops_safely(fail_status, uncertain, moves):
    journal = Mock()
    def append(entry):
        if entry['status'] == fail_status:
            raise OSError('Synthetic unavailable log')
    journal.append.side_effect = append
    client = BatchIMAP()
    result = mail.call('archive_messages', archive_args(), factory=lambda: client, journal=journal)
    assert result['uncertain'] == uncertain
    assert sum(c[0] == 'MOVE' for c in client.commands) == moves
    assert not result['completed']


def test_header_batch_handles_imap_literal_suffix_attributes():
    client = BatchIMAP(2)
    client.uid = Mock(return_value=('OK', [(b'1 (BODY[HEADER.FIELDS (FROM)] {5}', client.raw),
        b' UID 2 FLAGS () RFC822.SIZE 200)', (b'2 (UID 1 FLAGS () RFC822.SIZE 200)', client.raw), b')']))
    result = mail.Mail(client).metadata_batch('INBOX', ['1', '2'], '1')
    assert [m['uid'] for m in result] == ['1', '2']


def test_list_folders_still_returns_list():
    assert isinstance(mail.call('list_folders', {}, factory=FakeIMAP), list)


def test_read_messages_batches_preflight():
    client = BatchIMAP(2)
    result = mail.call('read_messages', {'messages': [dict(folder='INBOX', uid=u, uidvalidity='1') for u in client.uids]}, factory=lambda: client)
    assert len(result['messages']) == 2
    assert sum(c[0] == 'SELECT' for c in client.commands) == 1
    assert sum(c[0] == 'FETCH' and 'HEADER.FIELDS' in c[2] for c in client.commands) == 1


def test_unsubscribe_batches_preflight():
    client = BatchIMAP(2)
    result = mail.call('list_unsubscribe', archive_args(2), factory=lambda: client)
    assert len(result['messages']) == 2
    assert [c[0] for c in client.commands] == ['SELECT', 'FETCH']


def test_copyuid_ranges_and_refuse_malformed():
    assert mail.copy_mapping(['9 1:3 100:102'], ['1', '2', '3']) == ('9', {'1': '100', '2': '101', '3': '102'})
    for value in ('9 1:3 100:101', '9 1:999999999 1', '9 1,1 2,3', 'bad 1 2'):
        with pytest.raises(PublicError):
            mail.copy_mapping([value], ['1', '2', '3'])


def test_unarchive_refuses_partial_action_even_if_earlier_chunks_completed(tmp_path):
    journal = ActionLog(tmp_path / 'private' / 'actions.jsonl')
    entry = {'action_id': 'partial', 'tool': 'archive_messages', 'destination': 'Archive',
             'chunk': 0, 'chunk_count': 2, 'uids': ['1'], 'copyuid': ['1 1 1001']}
    for status in ('attempting', 'completed'):
        journal.append({**entry, 'status': status})
    with pytest.raises(PublicError, match='incomplete'):
        mail.call('unarchive_messages', {'action_id': 'partial'}, factory=BatchIMAP, journal=journal)


def test_unarchive_legacy_single_uid_action(tmp_path):
    journal = ActionLog(tmp_path / 'private' / 'actions.jsonl')
    entry = {'action_id': 'legacy', 'tool': 'archive_messages', 'destination': 'Archive', 'uids': ['1']}
    journal.append({**entry, 'status': 'attempting'})
    journal.append({**entry, 'status': 'completed', 'copyuid': ['1 1 1001']})
    client = BatchIMAP(1)
    client.uids = ['1001']
    result = mail.call('unarchive_messages', {'action_id': 'legacy'}, factory=lambda: client, journal=journal)
    assert result['completed'] == ['1001']


class EnvelopeIMAP(BatchIMAP):
    def __init__(self, count=5000, matches=True):
        super().__init__(count)
        self.matches = matches
        self.literal_loads = 0

    def uid(self, command, *args):
        if command == 'SEARCH':
            # Model Gluon's literal-loading behavior for the old search.
            if any(key in str(args) for key in ('FROM', 'SUBJECT', 'TO ', 'CC ')):
                self.literal_loads += len(self.uids)
        if command == 'FETCH' and 'ENVELOPE' in args[1]:
            self.commands.append((command, *args))
            rows = []
            for uid in args[0].split(','):
                subject = '=?utf-8?b?Q2Fmw6k=?=' if self.matches else 'Other'
                rows.append((f'{uid} (UID {uid} ENVELOPE (NIL "{subject}" '
                             '(("=?utf-8?q?Ren=C3=A9?=" NIL "sender" "example.invalid")) '
                             'NIL NIL (("Reader" NIL "to" "example.invalid")) '
                             '(("Copy" NIL "cc" "example.invalid")) NIL NIL NIL) '
                             'INTERNALDATE "01-Jan-2026 12:00:00 +0000")').encode())
            return 'OK', rows
        if command == 'FETCH':
            self.literal_loads += len(args[0].split(','))
        return super().uid(command, *args)


@pytest.mark.parametrize('name', ['list_messages', 'search_messages'])
@pytest.mark.parametrize('filters', [dict(sender='RENÉ'), dict(sender='SENDER@EXAMPLE.INVALID'),
                                    dict(recipient='reader'), dict(recipient='CC@EXAMPLE.INVALID'),
                                    dict(subject='CAFÉ'), dict(sender='rené', recipient='copy', subject='café')])
def test_two_stage_filters_and_order(name, filters):
    client = EnvelopeIMAP()
    result = mail.call(name, dict(folder='INBOX', limit=3, since='01-Jan-2026', unread=True, **filters),
                       factory=lambda: client)
    assert [m['uid'] for m in result['messages']] == ['5000', '4999', '4998']
    assert result['complete'] and result['pages'] == 1 and result['scanned'] == 3
    assert result['candidates'] == 5000 and result['oldest_scanned_date'] == '2026-01-01'
    assert client.commands[0] == ('SELECT', '"INBOX"', True)
    assert client.commands[1] == ('SEARCH', None, '(SINCE "01-Jan-2026" UNSEEN)')
    assert client.literal_loads == 3
    assert len(client.commands) == 4
    assert all(not any(k in c[-1] for k in ('FROM', 'SUBJECT', 'TO ', 'CC '))
               for c in client.commands if c[0] == 'SEARCH')


@pytest.mark.parametrize('budget', ['count', 'time'])
def test_partial_search_budget(monkeypatch, budget):
    client = EnvelopeIMAP(count=401, matches=False)
    if budget == 'count':
        monkeypatch.setattr(mail, 'SEARCH_SCAN_MAX', 201)
    else:
        clock = Mock(return_value=0)
        monkeypatch.setattr(mail.time, 'monotonic', clock)
        original = client.uid
        def advance(*args):
            result = original(*args)
            if args[0] == 'FETCH':
                clock.return_value = 31
            return result
        client.uid = advance
    result = mail.Mail(client).search(folder='INBOX', subject='absent')
    assert not result['complete'] and not result['messages']
    assert result['scanned'] == (201 if budget == 'count' else 200)
    assert result['pages'] == (2 if budget == 'count' else 1)
    assert result['oldest_scanned_date'] == '2026-01-01'


def test_partial_search_preserves_matches(monkeypatch):
    monkeypatch.setattr(mail, 'SEARCH_SCAN_MAX', 2)
    result = mail.Mail(EnvelopeIMAP(count=3)).search(folder='INBOX', sender='sender', limit=3)
    assert not result['complete']
    assert [m['uid'] for m in result['messages']] == ['3', '2']


def test_exhausted_and_empty_searches():
    for count in (0, 401):
        result = mail.Mail(EnvelopeIMAP(count=count, matches=False)).search(folder='INBOX', subject='absent')
        assert result['complete'] and result['scanned'] == count and not result['messages']


def test_envelope_split_literals_and_escapes():
    values = mail.envelope_values([
        (b'1 (ENVELOPE (NIL {5}', b'a"b\\c'),
        b'NIL NIL NIL NIL NIL NIL NIL NIL) UID 7 INTERNALDATE "01-Jan-2026 12:00:00 +0000")'])
    assert values['7']['ENVELOPE'][1] == 'a"b\\c'


def assert_telemetry(record):
    assert set(record) == {'v', 'ts', 'tool', 'total_ms', 'outcome', 'error_class', 'req_bytes',
                           'resp_bytes', 'phase', 'requested', 'matched', 'chunks', 'candidates',
                           'scanned', 'pages', 'complete'}
    assert set(record['phase']) == {'connect', 'credentials', 'tls_context', 'tcp_greeting',
                                    'starttls', 'capability', 'login', 'select', 'search',
                                    'fetch', 'mutate', 'audit'}
    assert record['v'] == 1 and record['total_ms'] >= 0
    assert record['outcome'] in ('ok', 'tool_error', 'error', 'timeout', 'refused')
    assert record['ts'].endswith('+00:00')


@pytest.mark.parametrize('kind', ['ok', 'tool_error', 'error', 'timeout'])
def test_telemetry_schema_and_privacy(caplog, kind):
    client = EnvelopeIMAP()
    factory = lambda: client
    args = dict(folder='Folders/private-canary', sender='private-filter-canary', limit=2)
    if kind == 'error':
        factory = Mock(side_effect=RuntimeError('/private-path-canary'))
    if kind == 'timeout':
        factory = Mock(side_effect=TimeoutError('private-error-canary'))
    if kind == 'tool_error':
        args['limit'] = 51
    try:
        mail.call('search_messages', args, factory=factory)
    except (PublicError, RuntimeError, TimeoutError):
        pass
    line = next(r.message for r in caplog.records if r.message.startswith('mail_call '))
    record = json.loads(line.removeprefix('mail_call '))
    assert_telemetry(record)
    assert record['outcome'] == kind
    assert 'canary' not in line and 'example.invalid' not in line
    assert record['req_bytes'] == len(json.dumps(args).encode())


def test_5000_message_command_counts():
    before = EnvelopeIMAP()
    before.uid('SEARCH', None, '(FROM "sender" SUBJECT "café")')
    before.uid('FETCH', '4998,4999,5000', '(UID FLAGS RFC822.SIZE BODY.PEEK[HEADER.FIELDS (From)])')
    after = EnvelopeIMAP()
    mail.Mail(after).search(folder='INBOX', sender='sender', subject='café', limit=3)
    # SEARCH literal loads: 5000 -> 0; search/fetch round trips: 2 -> 3.
    assert before.literal_loads == 5003 and after.literal_loads == 3
    assert len(before.commands) == 2 and len(after.commands) == 4  # after also EXAMINEs


# Exercise imaplib's actual wire parser as well as the command-level fakes.
@contextmanager
def envelope_server(count=5000):
    transport, peer = socket.socketpair()
    model = EnvelopeIMAP(count=count)
    failures = []

    class Client(WireIMAP):
        def _create_socket(self, timeout):
            transport.settimeout(3)
            return transport

    def serve():
        try:
            peer.sendall(b'* OK synthetic server\r\n')
            reader = peer.makefile('rb')
            while line := reader.readline():
                tag, command, *rest = line.decode('ascii').strip().split(' ', 2)
                args = rest[0] if rest else ''
                if command == 'CAPABILITY':
                    peer.sendall(b'* CAPABILITY IMAP4rev1\r\n')
                elif command == 'EXAMINE':
                    model.select(args, readonly=True)
                    peer.sendall(f'* {count} EXISTS\r\n* OK [UIDVALIDITY 1] valid\r\n'.encode())
                elif command == 'UID':
                    operation, values = args.split(' ', 1)
                    if operation == 'SEARCH':
                        _, data = model.uid('SEARCH', None, values)
                        peer.sendall(b'* SEARCH ' + data[0] + b'\r\n')
                    else:
                        uids, fields = values.split(' ', 1)
                        _, data = model.uid('FETCH', uids, fields)
                        for row in data:
                            if isinstance(row, bytes):
                                sequence, attrs = row.split(b' ', 1)
                                peer.sendall(b'* ' + sequence + b' FETCH ' + attrs + b'\r\n')
                            else:
                                attrs, raw = row
                                sequence, attrs = attrs.split(b' ', 1)
                                peer.sendall(b'* ' + sequence + b' FETCH ' + attrs[:-1] +
                                             b' BODY[HEADER.FIELDS (From)] {' + str(len(raw)).encode() +
                                             b'}\r\n' + raw + b')\r\n')
                elif command == 'LOGOUT':
                    peer.sendall(b'* BYE done\r\n' + tag.encode() + b' OK logout\r\n')
                    break
                else:
                    raise AssertionError(command)
                peer.sendall(tag.encode() + b' OK done\r\n')
        except Exception as error:
            failures.append(error)
        finally:
            peer.close()

    worker = threading.Thread(target=serve, daemon=True)
    worker.start()
    client = Client('synthetic.invalid')
    client.state = 'AUTH'  # Authentication is outside this search-only fixture.
    try:
        yield client, model
    finally:
        client.logout()
        worker.join(3)
        assert not worker.is_alive() and not failures


def test_wire_5000_message_before_after():
    with envelope_server() as (client, before):
        client.select('"INBOX"', readonly=True)
        mail.ok(client.uid('SEARCH', None, '(FROM "sender" SUBJECT "fixture")'))
        mail.Mail(client).metadata_batch('INBOX', ['4998', '4999', '5000'], '1')
    with envelope_server() as (client, after):
        result = mail.Mail(client).search(folder='INBOX', sender='rené', subject='café', limit=3)
    assert [m['uid'] for m in result['messages']] == ['5000', '4999', '4998']
    assert result['complete'] and result['pages'] == 1
    assert before.literal_loads == 5003 and after.literal_loads == 3
    assert len(before.commands) == 3 and len(after.commands) == 4


def test_total_includes_connect_audit_and_cleanup(monkeypatch, caplog):
    clock = Mock(return_value=0)
    monkeypatch.setattr(mail.time, 'monotonic', clock)
    client = BatchIMAP(count=1)
    def factory():
        clock.return_value += .1
        return client
    def append(entry):
        clock.return_value += .2
    def logout():
        clock.return_value += .3
    client.logout = logout
    mail.call('list_messages', {'folder': 'INBOX'}, factory=factory, journal=Mock(append=append))
    record = json.loads(caplog.records[-1].message.removeprefix('mail_call '))
    assert record['total_ms'] == 600
    assert record['phase']['connect'] == 100 and record['phase']['audit'] == 200


def test_wire_no_filter_path():
    with envelope_server(count=10) as (client, model):
        result = mail.Mail(client).search(folder='INBOX', limit=2)
    assert [m['uid'] for m in result['messages']] == ['9', '10']
    assert [c[0] for c in model.commands] == ['SELECT', 'SEARCH', 'FETCH']
    assert model.literal_loads == 2


def test_wire_default_scan_cap():
    with envelope_server(count=5001) as (client, model):
        result = mail.Mail(client).search(folder='INBOX', subject='absent')
    assert not result['complete'] and result['scanned'] == 5000
    assert result['pages'] == 25 and result['candidates'] == 5001
    assert model.literal_loads == 0
