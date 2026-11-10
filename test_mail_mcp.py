"""All mail below is generated synthetic content; no captured mail or live I/O."""
from email.message import EmailMessage
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
        'add_labels', 'mark_read', 'list_actions'}


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


def test_busy_call_does_not_block_ping(monkeypatch):
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
