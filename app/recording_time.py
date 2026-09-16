"""Recover device file timestamps using valid qlog clocks and monotonic time.

Only boundary qlogs are read. No segment-number * 60 reconstruction is used.
"""
import io
import math
import os
from pathlib import Path
from statistics import median
import threading

_schema = None
_lock = threading.Lock()
_boot_offsets = {}
_valid_windows = []


class ClockSamples(list):
    boot_id = ""
    start_mono = None
    end_mono = None
    init_offset = None


def clock_samples(data):
    import capnp
    import zstandard
    global _schema
    with _lock:
        if _schema is None:
            root = Path(__file__).resolve().parents[2]
            local = Path(__file__).resolve().parent / "resources" / "cereal"
            candidates = [local] + [root / name / "openpilot/cereal" for name in
                          ("openpilot-carrot-wip", "carrotpilot")]
            folder = next((p for p in candidates if (p / "log.capnp").exists()), None)
            if folder is None:
                raise ValueError("로그 시간 확인용 cereal 스키마를 찾지 못했습니다")
            capnp.remove_import_hook()
            # pycapnp on Windows can abort on a non-ASCII absolute schema path.
            # Use relative names while holding the initialization lock. All
            # server file operations use absolute paths; always restore cwd.
            previous_cwd = Path.cwd()
            includes = [".", os.path.relpath(folder.parent / "capnp_include", folder),
                        os.path.relpath(root / "log_analyze/tools/capnp_include", folder)]
            try:
                os.chdir(folder)
                _schema = capnp.load("log.capnp", imports=includes)
            finally:
                os.chdir(previous_cwd)
        with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(data)) as reader:
            raw = reader.read(80 * 1024 * 1024 + 1)
        if len(raw) > 80 * 1024 * 1024:
            raise ValueError("시간 확인용 로그 크기 제한 초과")
        samples = ClockSamples()
        try:
            for event in _schema.Event.read_multiple_bytes(raw):
                kind = event.which()
                if kind == "initData":
                    samples.boot_id = str(event.initData.bootlogId)
                    samples.init_offset = (event.initData.wallTimeNanos - event.logMonoTime) / 1e9
                if kind in ("can", "carState", "deviceState", "clocks", "roadCameraState", "roadEncodeIdx"):
                    mono = event.logMonoTime / 1e9
                    if mono > 0:
                        samples.start_mono = mono if samples.start_mono is None else min(samples.start_mono, mono)
                        samples.end_mono = mono if samples.end_mono is None else max(samples.end_mono, mono)
                if event.which() == "clocks":
                    mono = event.logMonoTime / 1e9
                    wall = event.clocks.wallTimeNanos / 1e9
                    if mono > 0 and wall > 0:
                        samples.append((mono, wall, bool(event.valid)))
        except capnp.KjException:
            # Power loss can leave an incomplete last event. Complete preceding
            # clocks remain usable; do not discard the entire qlog.
            if not samples and samples.start_mono is None:
                raise ValueError("읽을 수 있는 로그 시계가 없습니다")
        offsets = [w - m for m, w, ok in samples if ok]
        if samples.boot_id and offsets and max(offsets) - min(offsets) <= 10:
            _boot_offsets[samples.boot_id] = median(offsets)
        if offsets and max(offsets) - min(offsets) <= 10:
            _valid_windows.append((median(offsets), samples.start_mono, samples.end_mono))
            del _valid_windows[:-128]
        return samples


def recover_times(data, boundary_samples):
    """Keep normal mtime values; fix only offsets proven invalid by logs."""
    samples = [s for group in boundary_samples.values() for s in group]
    valid = [w - m for m, w, ok in samples if ok]
    if not valid:
        # Short adjacent sessions may contain no clocks event. A shared
        # bootlogId proves their monotonic clock belongs to the same boot.
        boots = {getattr(group, "boot_id", "") for group in boundary_samples.values()}
        if len(boots) == 1 and next(iter(boots)) in _boot_offsets:
            valid = [_boot_offsets[next(iter(boots))]]
        if not valid:
            # Some devices omit bootlogId. Match BOTH wall-minus-monotonic
            # offset and adjacent monotonic coverage; route order alone is not
            # evidence that sessions share a clock.
            matched = []
            for group in boundary_samples.values():
                init = getattr(group, "init_offset", None)
                start = getattr(group, "start_mono", None)
                if init is None or start is None:
                    continue
                for anchor, lo, hi in _valid_windows:
                    if abs(init - anchor) <= 2 and start <= hi + 300 and group.end_mono >= lo - 300:
                        matched.append(anchor)
            if matched and max(matched) - min(matched) <= 2:
                valid = matched
    if not valid:
        raise ValueError("정상 시각이 기록된 로그가 없어 날짜를 복구할 수 없습니다")
    offset = median(valid)
    if max(valid) - min(valid) > 10:
        raise ValueError("로그의 정상 시각 기준이 서로 달라 자동 보정을 중단했습니다")
    invalid = [w - m for m, w, ok in samples if not ok and abs(w - m - offset) > 300]
    shifts = []
    for value in invalid:
        delta = offset - value
        if not any(abs(delta - existing) < 2 for existing in shifts):
            shifts.append(delta)
    monos = [m for m, _, _ in samples]
    for group in boundary_samples.values():
        if getattr(group, "start_mono", None) is not None:
            monos.extend([group.start_mono, group.end_mono])
    if not monos:
        raise ValueError("녹화 경과시간이 없습니다")
    low = min(monos) + offset
    high = max(monos) + offset

    def normalize(value):
        try:
            value = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(value) or value <= 0:
            return None
        if low - 90 <= value <= high + 90:
            return value
        candidates = [value + delta for delta in shifts
                      if low - 90 <= value + delta <= high + 90]
        return candidates[0] if len(candidates) == 1 else None

    times = {}
    for segment in data.get("segments", []):
        original = data.get("segmentTimes", {}).get(segment, {})
        start, end = normalize(original.get("startEpoch")), normalize(original.get("endEpoch"))
        clocks = boundary_samples.get(segment, [])
        if getattr(clocks, "start_mono", None) is not None:
            start = clocks.start_mono + offset
            end = clocks.end_mono + offset
        elif clocks:
            # Boundary qlogs directly provide recording coverage, including
            # log-only segments without qcamera.ts.
            start = min(m for m, _, _ in clocks) + offset
            end = max(m for m, _, _ in clocks) + offset
        if start is not None and end is not None and 0 < end - start <= 300:
            times[segment] = {"startEpoch": start, "endEpoch": end}
    if not times:
        raise ValueError("복구할 수 있는 구간 시각이 없습니다")
    ordered = sorted(data.get("segments", []), key=lambda s: int(s.rsplit("--", 1)[1]))
    known = [times[s] for s in ordered if s in times]
    first, last = known[0], known[-1]
    return {**data, "segmentTimes": times,
            "routeStartEpoch": first["startEpoch"] if first else 0,
            "routeEndEpoch": last["endEpoch"] if last else 0,
            "timeSource": "qlog-clocks", "timeCorrected": True,
            "unresolvedSegments": len(ordered) - len(times)}
