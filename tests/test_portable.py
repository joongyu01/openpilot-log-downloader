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
    def test_radar_schema_loader_in_korean_path(self):
        app = Path(__file__).resolve().parents[1] / 'app'
        with tempfile.TemporaryDirectory(prefix='레이더 한글 ') as tmp:
            dest = Path(tmp)
            shutil.copy2(app / 'radar_export_runner.py', dest)
            schema = dest / 'schema.capnp'
            schema.write_text('@0xe780aee956effacc; struct Sample { value @0 :UInt32; }', encoding='utf-8')
            script = (
                'from pathlib import Path; import radar_export_runner; '
                'radar_export_runner.windows_schema_paths(); import capnp; '
                'p=str(Path("schema.capnp").resolve()); '
                'a=capnp.load(p); b=capnp.SchemaParser().load(p); '
                'assert a.Sample.new_message(value=7).value==7; '
                'assert b.Sample.new_message(value=9).value==9'
            )
            result = subprocess.run([sys.executable, '-E', '-s', '-c', script],
                                    cwd=dest, capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))

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
