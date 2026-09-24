import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.request
import urllib.error
import server


class DownloadResultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original = dict(server.JOB)
        self.last = patch.object(server, 'LAST_JOB', str(Path(self.tmp.name)/'last.json'))
        self.last.start()
        server.JOB.update(active=True, cancel=False, done=0, bytes=0, log=[],
                          ok=0, skipped=0, failed=0, error=None, total=501)

    def tearDown(self):
        server.JOB.clear();server.JOB.update(self.original)
        self.last.stop();self.tmp.cleanup()

    def test_failure_count_survives_log_limit(self):
        with patch.object(server, 'download_one', side_effect=[('missing',0)]+[('ok',1)]*500):
            server.run_job('r', [f'r--{i}' for i in range(501)], ['rlog'], self.tmp.name)
        self.assertEqual(server.JOB['failed'], 1)
        self.assertEqual(server.JOB['ok'], 500)
        self.assertEqual(len(server.JOB['log']), 500)
        self.assertFalse(server.JOB['active'])
        self.assertEqual(json.loads(Path(server.LAST_JOB).read_text(encoding='utf-8'))['failed'], 1)

    def test_directory_failure_finishes_with_error(self):
        with patch.object(server.os, 'makedirs', side_effect=OSError('disk unavailable')):
            server.run_job('r', ['r--0'], ['rlog'], self.tmp.name)
        self.assertFalse(server.JOB['active'])
        self.assertIn('disk unavailable', server.JOB['error'])

    def test_truncated_file_never_becomes_completed_file(self):
        response=io.BytesIO(b'ab');response.headers={'Content-Length':'10'}
        with patch.object(server.urllib.request, 'urlopen', return_value=response):
            status,_=server.download_one('r--0','rlog',self.tmp.name)
        self.assertTrue(status.startswith('실패'))
        self.assertFalse((Path(self.tmp.name)/'r--0--rlog.zst').exists())

    def test_folder_endpoint_opens_only_registered_directory(self):
        folder=str(Path(self.tmp.name).resolve())
        http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
        thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
        url=f'http://127.0.0.1:{http.server_port}/api/open-download-folder'
        def request(path):
            return urllib.request.urlopen(urllib.request.Request(url,data=json.dumps({'path':path}).encode(),headers={'Content-Type':'application/json'}))
        try:
            with patch.object(server,'OPENABLE_FOLDERS',{folder}),patch.object(server.os,'startfile',create=True) as opened:
                with request(folder) as r:self.assertTrue(json.load(r)['ok'])
                opened.assert_called_once_with(folder)
                with self.assertRaises(urllib.error.HTTPError) as error:request(str(Path(folder).parent))
                error.exception.close()
        finally:http.shutdown();http.server_close();thread.join()

if __name__=='__main__':unittest.main()
