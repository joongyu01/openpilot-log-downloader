"""Run the unmodified production radar exporter in an isolated Python process."""
from pathlib import Path
import os
import runpy
import sys


def windows_schema_paths(base=None):
    """pycapnp's Windows file opener rejects some non-ASCII absolute paths.

    Keep the original schemas and engine intact; only present relative filenames
    to capnp while the isolated exporter process is inside one engine directory.
    Keeping the same base across schemas also preserves parser import identity.
    Both capnp.load and independent SchemaParser instances occur in the engine.
    """
    if sys.platform != "win32":
        return
    import capnp
    original_load, original_parser = capnp.load, capnp.SchemaParser
    base = Path(base or Path.cwd()).resolve()

    def relative_load(loader, filename, *args, **kwargs):
        schema = Path(filename).resolve()
        previous = Path.cwd()
        options = dict(kwargs)
        if "imports" in options:
            options["imports"] = [Path(os.path.relpath(Path(item).resolve(), base)).as_posix() for item in options["imports"]]
        try:
            os.chdir(base)
            return loader(Path(os.path.relpath(schema, base)).as_posix(), *args, **options)
        finally:
            os.chdir(previous)

    class RelativeSchemaParser:
        def __init__(self, *args, **kwargs):
            self.parser = original_parser(*args, **kwargs)

        def load(self, filename, *args, **kwargs):
            return relative_load(self.parser.load, filename, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self.parser, name)

    capnp.load = lambda filename, *args, **kwargs: relative_load(original_load, filename, *args, **kwargs)
    capnp.SchemaParser = RelativeSchemaParser


if __name__ == "__main__":
    engine, source, output, sensor, flip = sys.argv[1:]
    engine, source, output = (str(Path(path).resolve()) for path in (engine, source, output))
    sys.path[:0] = [engine, str(Path(engine) / "opendbc_repo")]
    windows_schema_paths(engine)
    sys.argv = ["radar_web_export", source, output, "--sensor", sensor, "--radar-track-flip", flip]
    runpy.run_module("openpilot.selfdrive.carrot.radar.tools.radar_web_export", run_name="__main__")
