"""Synthetic systemd credentials only; no Bridge account or secret profiles."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import mail_mcp as mail


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='.credential-test-', dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.credential = self.directory / 'proton-bridge-imap'
        self.environment = patch.dict(os.environ, {
            'CREDENTIALS_DIRECTORY': str(self.directory),
            'PROTON_BRIDGE_USERNAME': 'ignored-env-user',
            'PROTON_BRIDGE_PASSWORD': 'ignored-env-password',
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_exact_format_with_optional_final_newline(self):
        for ending in (b'', b'\n'):
            with self.subTest(ending=ending):
                self.credential.write_bytes(b'fixture@example.test\nfixture-password' + ending)
                self.assertEqual(mail.read_credentials(), ('fixture@example.test', 'fixture-password'))

    def test_preserves_spaces_and_utf8(self):
        self.credential.write_text('fixture@example.test\n  fixture-pässword  \n', encoding='utf-8')
        self.assertEqual(mail.read_credentials()[1], '  fixture-pässword  ')

    def test_malformed_files_fail_closed_without_echoing_values(self):
        for raw in (b'', b'private-fixture', b'\nprivate-fixture', b'private-fixture\n',
                    b'private-fixture\n\n', b'private-fixture\npw\n\n', b'private-fixture\npw\nextra',
                    b'private-fixture\r\npw\r\n', b'private-fixture\npw\x00',
                    b'private-fixture\npw\t', b'private-fixture\npw\x7f',
                    b'private-fixture\n\xff', b'private-fixture\n' + b'x' * 16384):
            with self.subTest(length=len(raw)):
                self.credential.write_bytes(raw)
                with self.assertRaisesRegex(RuntimeError, 'exactly two nonempty UTF-8 lines') as caught:
                    mail.read_credentials()
                self.assertNotIn('private-fixture', str(caught.exception))
                self.assertTrue(caught.exception.__suppress_context__)

    def test_missing_directory_or_file_never_falls_back_to_env(self):
        for directory in (None, '', 'relative/path', str(self.directory)):
            with self.subTest(directory=directory):
                if directory is None:
                    os.environ.pop('CREDENTIALS_DIRECTORY', None)
                else:
                    os.environ['CREDENTIALS_DIRECTORY'] = directory
                with self.assertRaisesRegex(RuntimeError, 'credential unavailable'):
                    mail.read_credentials()

    def test_unreadable_file_has_redacted_error(self):
        with patch.object(Path, 'open', side_effect=PermissionError('private-fixture')):
            with self.assertRaisesRegex(RuntimeError, 'credential unavailable') as caught:
                mail.read_credentials()
        self.assertNotIn('private-fixture', str(caught.exception))

    def test_missing_credential_refuses_before_network_or_bind(self):
        with patch.object(mail.imaplib, 'IMAP4') as imap, patch.object(mail, 'BoundedHTTPServer') as bind:
            with self.assertRaisesRegex(RuntimeError, 'credential unavailable'):
                mail.connect()
            with self.assertRaisesRegex(RuntimeError, 'credential unavailable'):
                mail.create_server()
            imap.assert_not_called()
            bind.assert_not_called()

    def test_credential_is_reread_for_a_new_connection(self):
        self.credential.write_bytes(b'fixture@example.test\nfirst-password\n')
        self.assertEqual(mail.read_credentials()[1], 'first-password')
        self.credential.write_bytes(b'fixture@example.test\nsecond-password\n')
        self.assertEqual(mail.read_credentials()[1], 'second-password')

