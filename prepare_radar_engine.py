"""Copy only source/schema/DBC files for the local radar engine into a release.

Usage: python prepare_radar_engine.py CARROTPILOT_ROOT APP_DIRECTORY
No routes, logs, settings, images or personal files are copied.
"""
from pathlib import Path
import shutil
import sys

FOLDERS = (
    "openpilot/selfdrive/carrot/radar",
    "openpilot/selfdrive/carrot/radar_motion",
    "openpilot/selfdrive/carrot/cluster",
    "openpilot/selfdrive/controls/lib",
    "openpilot/common",
    "openpilot/cereal",
    "opendbc_repo/opendbc",
)


def prepare(source, destination):
    target = destination / "resources" / "radar_engine"
    count = 0
    for relative in FOLDERS:
        for path in (source / relative).rglob("*"):
            if not path.is_file() or path.suffix not in {".py", ".capnp", ".dbc"} or "__pycache__" in path.parts:
                continue
            output = target / path.relative_to(source)
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, output)
            count += 1
    # Preserve real package initializers without copying unrelated root files.
    for relative in ("openpilot/__init__.py", "openpilot/selfdrive/__init__.py", "openpilot/selfdrive/carrot/__init__.py", "openpilot/selfdrive/controls/__init__.py"):
        path = source / relative
        if path.is_file():
            output = target / relative
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, output)
    for relative in ("LICENSE", "opendbc_repo/LICENSE"):
        path = source / relative
        if path.is_file():
            output = target / relative
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, output)
    print(f"Copied {count} radar source/schema/DBC files to {target}")


if __name__ == "__main__":
    prepare(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve())
