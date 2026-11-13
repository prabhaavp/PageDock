import io
import os
import tempfile
import unittest
from PIL import Image
from pypdf import PdfReader

os.environ.setdefault('APP_PASSWORD', 'test-password-long')
os.environ.setdefault('SESSION_SECRET', 'test-secret-long-enough-for-sessions')
os.environ.setdefault('PRINTER_HOST', '127.0.0.1')
os.environ.setdefault('DATA_DIR', tempfile.mkdtemp(prefix='printer-test-'))
import app


class ExportTests(unittest.TestCase):
    def test_pdf_keeps_page_dimensions_with_different_resolutions(self):
        client = app.app.test_client()
        login = client.post('/api/login', json={'password': os.environ['APP_PASSWORD']})
        headers = {'X-CSRF-Token': login.json['csrf']}
        ids = ['a' * 32, 'b' * 32]
        for ident, dpi in zip(ids, (100, 200)):
            Image.new('RGB', (100, 200), 'white').save(app.PAGES / (ident + '.jpg'))
            (app.PAGES / (ident + '.dpi')).write_text(str(dpi))
        response = client.post('/api/export', json={'format': 'pdf', 'pages': ids}, headers=headers)
        self.assertEqual(response.status_code, 200)
        pdf = PdfReader(io.BytesIO(response.data))
        response.close()
        self.assertEqual([float(page.mediabox.width) for page in pdf.pages], [72, 36])
        documents = client.get('/api/documents').json['documents']
        self.assertTrue(any(document['pages'] == 2 for document in documents))


if __name__ == '__main__':
    unittest.main()
