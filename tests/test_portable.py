"""Regression: pycapnp must load bundled schemas from a Korean folder path."""
import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(importlib.util.find_spec('capnp') and importlib.util.find_spec('zstandard'),
                     'Install requirements.txt to test bundled schema loading')
class PortableSchemaTest(unittest.TestCase):
    def test_schema_in_korean_path(self):
        app = Path(__file__).resolve().parents[1] / 'app'
        with tempfile.TemporaryDirectory(prefix='다운로더 한글 ') as tmp:
            dest = Path(tmp) / 'app'
            dest.mkdir()
            shutil.copy2(app / 'recording_time.py', dest)
            shutil.copytree(app / 'resources', dest / 'resources')
            script = (
                'from pathlib import Path; import recording_time,zstandard; '
                'before=Path.cwd(); '
                'recording_time.clock_samples(zstandard.ZstdCompressor().compress(b"")); '
                'assert Path.cwd()==before'
            )
            result = subprocess.run([sys.executable, '-E', '-s', '-c', script],
                                    cwd=dest, capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
