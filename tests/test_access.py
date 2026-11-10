"""Access assertions use local RSA/JWKS fixtures only, mirroring print's tests."""
import http.client
import importlib.util
import io
import json
from pathlib import Path
import sys
import threading
import time
import urllib.request
from urllib.error import URLError
from unittest.mock import Mock

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

sys.path.insert(0, str(Path(__file__).parents[1]))
import access
spec = importlib.util.spec_from_file_location('mail_mcp', Path(__file__).parents[1] / 'mail_mcp.py')
mail = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mail)
TEAM = 'test-team.cloudflareaccess.com'
AUD = 'test-mail-audience'
CALL = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
        'params': {'name': 'list_folders', 'arguments': {}}}


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    (tmp_path / 'proton-bridge-imap').write_text('fixture@example.test\nfixture-password\n')
    monkeypatch.setenv('CREDENTIALS_DIRECTORY', str(tmp_path))
    monkeypatch.delenv('ACCESS_TEAM_DOMAIN', raising=False)
    monkeypatch.delenv('ACCESS_AUD', raising=False)
    monkeypatch.setattr(urllib.request.OpenerDirector, 'open', Mock(side_effect=AssertionError('Unexpected network')))


@pytest.fixture(scope='module')
def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def protected(monkeypatch, key):
    monkeypatch.setenv('PROTON_PUBLIC_HOST', 'mail.example.com')
    monkeypatch.setenv('ACCESS_TEAM_DOMAIN', TEAM)
    monkeypatch.setenv('ACCESS_AUD', AUD)
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    jwk.update(kid='local-key', alg='RS256', use='sig')
    def fetch(request, timeout):
        assert request.full_url == f'https://{TEAM}/cdn-cgi/access/certs'
        assert timeout == 5
        return io.BytesIO(json.dumps({'keys': [jwk]}).encode())
    network = Mock(side_effect=fetch)
    monkeypatch.setattr(urllib.request.OpenerDirector, 'open', network)
    tools = Mock(return_value=[])
    monkeypatch.setattr(mail, 'call', tools)
    server = mail.BoundedHTTPServer(('127.0.0.1', 0), mail.Handler)
    server.access = access.configured_access()
    worker = threading.Thread(target=server.serve_forever)
    worker.start()
    yield server, tools, network
    server.shutdown()
    worker.join()
    server.server_close()


def token(key, **claims):
    data = {'iss': f'https://{TEAM}', 'aud': [AUD], 'exp': int(time.time()) + 300}
    data.update(claims)
    return jwt.encode(data, key, algorithm='RS256', headers={'kid': 'local-key'})


def request(server, assertion=None, method='POST', host='127.0.0.1:8766', origin=None, duplicate=False):
    conn = http.client.HTTPConnection(*server.server_address)
    body = json.dumps(CALL).encode()
    conn.putrequest(method, '/mcp', skip_host=True)
    conn.putheader('Host', host)
    conn.putheader('Accept', 'application/json, text/event-stream')
    conn.putheader('Content-Type', 'application/json')
    conn.putheader('Content-Length', str(len(body)))
    if origin:
        conn.putheader('Origin', origin)
    if assertion is not None:
        conn.putheader('Cf-Access-Jwt-Assertion', assertion)
        if duplicate:
            conn.putheader('Cf-Access-Jwt-Assertion', assertion)
    conn.endheaders(body)
    response = conn.getresponse()
    status, data = response.status, response.read()
    conn.close()
    return status, data


def test_valid_remote_and_cached(protected, key):
    server, tools, network = protected
    for _ in range(2):
        assert request(server, token(key), host='mail.example.com',
                       origin='https://mail.example.com')[0] == 200
    assert tools.call_count == 2
    assert network.call_count == 1


@pytest.mark.parametrize('kind', ['missing', 'wrong-aud', 'expired', 'bad-signature', 'wrong-issuer',
                                  'missing-exp', 'missing-aud', 'missing-iss', 'malformed', 'wrong-alg', 'oversized', 'duplicate'])
def test_rejected_before_tools(protected, key, kind):
    server, tools, network = protected
    assertion = token(key)
    if kind == 'missing': assertion = None
    elif kind == 'wrong-aud': assertion = token(key, aud='another-app')
    elif kind == 'expired': assertion = token(key, exp=int(time.time()) - 60)
    elif kind == 'wrong-issuer': assertion = token(key, iss='https://other.cloudflareaccess.com')
    elif kind == 'bad-signature': assertion = token(rsa.generate_private_key(public_exponent=65537, key_size=2048))
    elif kind.startswith('missing-'):
        claims = {'iss': f'https://{TEAM}', 'aud': AUD, 'exp': int(time.time()) + 300}
        del claims[kind.removeprefix('missing-')]
        assertion = jwt.encode(claims, key, algorithm='RS256', headers={'kid': 'local-key'})
    elif kind == 'malformed': assertion = 'not-a-jwt'
    elif kind == 'wrong-alg': assertion = jwt.encode({'aud': AUD}, 'x' * 32, algorithm='HS256')
    elif kind == 'oversized': assertion = 'x' * 16385
    for method in ('POST', 'GET', 'DELETE', 'PUT'):
        status, data = request(server, assertion, method=method, duplicate=kind == 'duplicate')
        assert status == 401
        assert b'Invalid or missing Access assertion' in data
    tools.assert_not_called()
    if kind in ('missing', 'malformed', 'wrong-alg', 'oversized', 'duplicate'):
        network.assert_not_called()


def test_jwks_outage_fails_closed(protected, key):
    server, tools, network = protected
    network.side_effect = URLError('private network detail')
    status, body = request(server, token(key))
    assert status == 401 and b'private network detail' not in body
    tools.assert_not_called()


@pytest.mark.parametrize('host,origin', [('evil.test', None), ('mail.example.com.evil.test', None),
                                         ('127.0.0.1:8765', None), ('mail.example.com', 'https://evil.test')])
def test_exact_host_origin_guard(protected, key, host, origin):
    server, tools, _ = protected
    assert request(server, token(key), host=host, origin=origin)[0] == 403
    tools.assert_not_called()


@pytest.mark.parametrize('settings', [{'ACCESS_TEAM_DOMAIN': TEAM}, {'ACCESS_AUD': AUD},
                                      {'ACCESS_TEAM_DOMAIN': 'https://bad.example/path', 'ACCESS_AUD': AUD}])
def test_bad_config_fails_before_bind(monkeypatch, settings):
    for name, value in settings.items(): monkeypatch.setenv(name, value)
    bind = Mock()
    monkeypatch.setattr(mail, 'BoundedHTTPServer', bind)
    with pytest.raises(ValueError): mail.create_server()
    bind.assert_not_called()


def test_unset_warns_but_stays_loopback(monkeypatch, caplog):
    bind = Mock()
    monkeypatch.setattr(mail, 'BoundedHTTPServer', bind)
    mail.create_server()
    assert 'Access JWT validation is DISABLED' in caplog.text
    assert bind.call_args.args[0] == ('127.0.0.1', 8766)
    assert bind.return_value.access is None


@pytest.mark.parametrize('host,origin,status', [
    ('mail.example.com', None, 403),
    ('mail.example.com', 'https://mail.example.com', 403),
    ('127.0.0.1:8766', 'https://mail.example.com', 403),
    ('127.0.0.1:8766', None, 200),
    ('localhost:8766', 'http://localhost:8766', 200),
    ('127.0.0.1:8766', 'http://127.0.0.1:8766', 200),
])
def test_unconfigured_accepts_only_local_host_origin(monkeypatch, host, origin, status):
    tools = Mock(return_value=[])
    monkeypatch.setattr(mail, 'call', tools)
    server = mail.BoundedHTTPServer(('127.0.0.1', 0), mail.Handler)
    server.access = None
    worker = threading.Thread(target=server.serve_forever)
    worker.start()
    try:
        # Even supplying an assertion cannot enable public routing without config.
        assert request(server, 'not-a-jwt', host=host, origin=origin)[0] == status
        assert tools.call_count == (1 if status == 200 else 0)
    finally:
        server.shutdown()
        worker.join()
        server.server_close()


@pytest.mark.parametrize('assertion', [None, 'not-a-jwt'])
def test_configured_public_requires_assertion(protected, assertion):
    server, tools, _ = protected
    assert request(server, assertion, host='mail.example.com',
                   origin='https://mail.example.com')[0] == 401
    tools.assert_not_called()
