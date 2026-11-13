import io
import os
import tempfile
import unittest
from pathlib import Path
from PIL import Image

os.environ.setdefault('APP_PASSWORD', 'test-password-long')
os.environ.setdefault('SESSION_SECRET', 'test-secret-long-enough-for-sessions')
os.environ.setdefault('PRINTER_HOST', '127.0.0.1')
os.environ.setdefault('DATA_DIR', tempfile.mkdtemp(prefix='printer-test-'))
import app


class ScanUndoTests(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()
        response = self.client.post('/api/login', json={'password': os.environ['APP_PASSWORD']})
        self.headers = {'X-CSRF-Token': response.json['csrf']}
        self.ident = 'f' * 32
        self.path = app.PAGES / (self.ident + '.jpg')
        for p in app.PAGES.glob(self.ident + '.*'):
            p.unlink()
        Image.new('RGB', (120, 80), 'white').save(self.path)
        (app.PAGES / (self.ident + '.dpi')).write_text('100')

    def edit(self, crop):
        return self.client.post('/api/page/' + self.ident + '/edit', json={'crop': crop}, headers=self.headers)

    def size(self):
        with Image.open(self.path) as image:
            return image.size

    def test_undo_restores_saved_edits_across_sessions(self):
        self.assertEqual(self.edit([0, 0, .5, 1]).status_code, 200)
        self.assertEqual(self.size(), (60, 80))
        self.assertEqual(self.edit([0, 0, .5, 1]).status_code, 200)
        self.assertEqual(self.size(), (30, 80))
        self.client.post('/api/logout', headers=self.headers)
        response = self.client.post('/api/login', json={'password': os.environ['APP_PASSWORD']})
        self.headers = {'X-CSRF-Token': response.json['csrf']}
        self.assertEqual(self.client.get('/api/pages').json['undo'][self.ident], 2)
        self.assertEqual(self.client.post('/api/page/' + self.ident + '/undo', headers=self.headers).status_code, 200)
        self.assertEqual(self.size(), (60, 80))
        self.assertEqual(self.client.post('/api/page/' + self.ident + '/undo', headers=self.headers).status_code, 200)
        self.assertEqual(self.size(), (120, 80))
        self.assertEqual(self.client.post('/api/page/' + self.ident + '/undo', headers=self.headers).status_code, 409)

    def test_thumbnail_refreshes_after_edit_and_undo(self):
        url = '/api/page/' + self.ident + '/thumbnail'
        first = self.client.get(url)
        self.assertEqual(Image.open(io.BytesIO(first.data)).size, (120, 80))
        first.close()
        self.assertEqual(self.edit([0, 0, .5, 1]).status_code, 200)
        edited = self.client.get(url)
        self.assertEqual(Image.open(io.BytesIO(edited.data)).size, (60, 80))
        edited.close()
        self.client.post('/api/page/' + self.ident + '/undo', headers=self.headers)
        restored = self.client.get(url)
        self.assertEqual(Image.open(io.BytesIO(restored.data)).size, (120, 80))
        restored.close()

    def test_history_is_bounded_and_deleted_with_page(self):
        for _ in range(app.UNDO_DEPTH + 2):
            self.assertEqual(self.edit([0, 0, .9, 1]).status_code, 200)
        self.assertEqual(app.undo_count(self.ident), app.UNDO_DEPTH)
        self.assertEqual(self.client.delete('/api/page/' + self.ident, headers=self.headers).status_code, 200)
        self.assertEqual(list(app.PAGES.glob(self.ident + '.*')), [])

    def test_cross_origin_mutation_is_rejected(self):
        result = self.client.post('/api/page/' + self.ident + '/undo', headers={**self.headers, 'Origin': 'https://attacker.example'})
        self.assertEqual(result.status_code, 403)


if __name__ == '__main__':
    unittest.main()
