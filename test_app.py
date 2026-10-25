import os
import tempfile
import threading
import unittest
from unittest.mock import patch
import app as api


class Downloader:
    def __init__(self, options):
        self.options = options
        api.last_options = options
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def extract_info(self, url, download):
        if download:
            path = self.options['outtmpl'].replace('%(ext)s', 'mp4')
            with open(path, 'wb') as f:
                f.write(b'video')
        return {'title': 'Test video', 'formats': []}


class DownloadTests(unittest.TestCase):
    def setUp(self):
        api.hits.clear()
        api.sem = threading.BoundedSemaphore(2)
        self.client = api.app.test_client()
        self.safe = patch.object(api, 'safe_url', return_value=True)
        self.safe.start()
        self.addCleanup(self.safe.stop)
        self.dlp = patch.object(api.yt_dlp, 'YoutubeDL', Downloader)
        self.dlp.start()
        self.addCleanup(self.dlp.stop)

    def assert_slots_free(self):
        self.assertTrue(api.sem.acquire(blocking=False))
        self.assertTrue(api.sem.acquire(blocking=False))
        self.assertFalse(api.sem.acquire(blocking=False))
        api.sem.release()
        api.sem.release()

    def get(self):
        return self.client.get('/api/download?url=https://example.org/video&q=720')

    def test_temp_setup_failure_releases_slot(self):
        with patch.object(api.tempfile, 'mkdtemp', side_effect=OSError('full disk')):
            response = self.get()
        self.assertEqual(response.status_code, 422)
        self.assert_slots_free()

    def test_processed_file_obeys_cap(self):
        with patch.object(api, 'MAX_BYTES', 4):
            response = self.get()
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json['error'], 'too_big')
        self.assert_slots_free()

    def test_send_file_failure_cleans_and_releases(self):
        with tempfile.TemporaryDirectory() as root:
            folder = os.path.join(root, 'download')
            os.mkdir(folder)
            with patch.object(api.tempfile, 'mkdtemp', return_value=folder), patch.object(api, 'send_file', side_effect=OSError('cannot serve')):
                response = self.get()
            self.assertEqual(response.status_code, 422)
            self.assertFalse(os.path.exists(folder))
        self.assert_slots_free()

    def test_success_still_returns_attachment(self):
        response = self.get()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b'video')
        self.assertIn('Test video.mp4', response.headers['Content-Disposition'])
        response.close()
        self.assert_slots_free()

    def test_busy_does_not_start_download(self):
        api.sem.acquire(); api.sem.acquire()
        with patch.object(api.tempfile, 'mkdtemp') as mk:
            response = self.get()
            mk.assert_not_called()
        self.assertEqual(response.status_code, 503)
        api.sem.release(); api.sem.release()

    def test_invalid_quality_rejected(self):
        # Any height in 144..4320 is accepted by design; values outside the
        # range (or non-numeric) must be rejected.
        for bad in ('100', '5000', 'abc', ''):
            response = self.client.get('/api/download?url=https://example.org/video&q=' + bad)
            self.assertEqual(response.status_code, 400, bad)
        response = self.client.get('/api/download?url=https://example.org/video&q=999')
        self.assertEqual(response.status_code, 200)
        response.close()
        self.assert_slots_free()

    def test_download_uses_node_runtime(self):
        # /api/info passes the Node runtime for YouTube's JS solver; the
        # download path must too or videos that pass info still fail here.
        response = self.get()
        self.assertEqual(response.status_code, 200)
        response.close()
        self.assertEqual(api.last_options.get('js_runtimes'), {'node': {}})

    def test_rate_limit_uses_rightmost_forwarded_ip(self):
        # The leftmost X-Forwarded-For entries are client-controlled; only the
        # proxy-added rightmost one identifies the client. Spoofing the left
        # side must not reset the rate-limit bucket.
        for i in range(6):
            response = self.client.get('/api/download?url=https://example.org/video&q=720',
                                       headers={'X-Forwarded-For': f'198.51.100.{i}, 203.0.113.7'})
            self.assertEqual(response.status_code, 200)
            response.close()
        response = self.client.get('/api/download?url=https://example.org/video&q=720',
                                   headers={'X-Forwarded-For': '198.51.100.99, 203.0.113.7'})
        self.assertEqual(response.status_code, 429)

    def test_stale_rate_limit_entries_swept(self):
        api.last_sweep = 0.0  # force the sweep on the next request
        api.hits[('dl', '192.0.2.1')] = [0.0]  # long outside any window
        response = self.get()
        self.assertEqual(response.status_code, 200)
        response.close()
        self.assertNotIn(('dl', '192.0.2.1'), api.hits)


if __name__ == '__main__':
    unittest.main()
