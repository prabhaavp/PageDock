import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('APP_PASSWORD', 'test-password-long')
os.environ.setdefault('SESSION_SECRET', 'test-secret-long-enough-for-sessions')
os.environ.setdefault('PRINTER_HOST', '127.0.0.1')
os.environ.setdefault('DATA_DIR', tempfile.mkdtemp(prefix='printer-test-'))
import app


class SecurityTests(unittest.TestCase):
    def test_public_origin_rejects_cross_origin_and_sets_headers(self):
        client = app.app.test_client()
        with patch.object(app, 'PUBLIC_ORIGIN', 'https://print.example'), patch.dict(app.app.config, {'TRUSTED_HOSTS': ['print.example'], 'SESSION_COOKIE_SECURE': True}):
            result = client.get('/', base_url='https://other.example')
            self.assertEqual(result.status_code, 400)
            result = client.post('/api/login', base_url='https://print.example', headers={'Origin': 'https://attacker.example'}, json={'password': os.environ['APP_PASSWORD']})
            self.assertEqual(result.status_code, 403)
            result = client.post('/api/login', base_url='https://print.example', headers={'Origin': 'https://print.example'}, json={'password': os.environ['APP_PASSWORD']})
            self.assertEqual(result.status_code, 200)
            self.assertIn('Secure', result.headers['Set-Cookie'])
            self.assertIn('HttpOnly', result.headers['Set-Cookie'])
            self.assertIn('SameSite=Strict', result.headers['Set-Cookie'])
            self.assertEqual(result.headers['Cache-Control'], 'no-store')
            self.assertIn('Strict-Transport-Security', result.headers)

    def test_configured_names_are_escaped_in_html_and_match_manifest(self):
        client = app.app.test_client()
        with patch.object(app, 'APP_NAME', '<App & name>'), patch.object(app, 'PRINTER_NAME', '<Printer>'):
            page = client.get('/')
            self.assertIn(b'&lt;App &amp; name&gt;', page.data)
            self.assertIn(b'&lt;Printer&gt;', page.data)
            self.assertEqual(client.get('/manifest.webmanifest').json['name'], '<App & name>')


if __name__ == '__main__':
    unittest.main()
