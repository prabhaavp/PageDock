import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault('APP_PASSWORD', 'test-password-long')
os.environ.setdefault('SESSION_SECRET', 'test-secret-long-enough-for-sessions')
os.environ.setdefault('PRINTER_HOST', '127.0.0.1')
os.environ.setdefault('DATA_DIR', tempfile.mkdtemp(prefix='printer-test-'))
import app

JOB = b'<JobList><Job><JobUrl>/Jobs/JobList/31</JobUrl><JobCategory>Print</JobCategory><JobState>ProcessingError</JobState></Job></JobList>'
EMPTY = b'<JobList/>'
PAPER_OUT = b'<ProductStatus><ProductStatusAlertID>trayEmptyOrOpen</ProductStatusAlertID></ProductStatus>'


def response(content):
    result = Mock()
    result.content = content
    result.raise_for_status.return_value = None
    return result


class PrinterControls(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()
        result = self.client.post('/api/login', json={'password': os.environ['APP_PASSWORD']})
        self.headers = {'X-CSRF-Token': result.json['csrf']}

    def test_cancel_device_job_confirms_device_state(self):
        with patch.object(app.requests, 'get', side_effect=[response(JOB), response(EMPTY)]), patch.object(app.requests, 'put', return_value=response(b'')) as put:
            result = self.client.post('/api/printer/jobs/31/cancel', headers=self.headers)
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.json['confirmed'])
        self.assertIn('<JobState>Canceled</JobState>', put.call_args.kwargs['data'])
        self.assertTrue(put.call_args.args[0].endswith('/Jobs/JobList/31'))

    def test_cancel_does_not_report_success_until_job_clears(self):
        with patch.object(app.requests, 'get', side_effect=[response(JOB), response(JOB)]), patch.object(app.requests, 'put', return_value=response(b'')):
            result = self.client.post('/api/printer/jobs/31/cancel', headers=self.headers)
        self.assertFalse(result.json['confirmed'])

    def test_continue_does_not_enable_queue_while_paper_alert_active(self):
        with patch.object(app.requests, 'get', return_value=response(PAPER_OUT)), patch.object(app, 'cups_action') as run:
            result = self.client.post('/api/print/continue', headers=self.headers)
        self.assertFalse(result.json['continued'])
        run.assert_not_called()

    def test_continue_enables_queue_when_alert_clears(self):
        with patch.object(app.requests, 'get', return_value=response(b'<ProductStatus/>')), patch.object(app, 'cups_action', return_value={'ok': True}) as run:
            result = self.client.post('/api/print/continue', headers=self.headers)
        self.assertTrue(result.json['continued'])
        self.assertEqual(run.call_args.args[0], 'resume')

    def test_api_requires_session_and_disallows_caching(self):
        other = app.app.test_client()
        result = other.get('/api/printer/jobs')
        self.assertEqual(result.status_code, 401)
        self.assertEqual(result.headers['Cache-Control'], 'no-store')


if __name__ == '__main__':
    unittest.main()
