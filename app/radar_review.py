"""Local-only, bounded radar replay jobs using the route vault's production engine."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

_jobs = {}
_lock = threading.Lock()
_gate = threading.Semaphore(1)
ROOT = Path(__file__).resolve().parent
CACHE = Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir())) / "OpenpilotLogDownloader" / "radar-cache"


def engine_root():
    candidates = [ROOT / "resources" / "radar_engine", ROOT.parents[1] / "carrotpilot"]
    return next((p for p in candidates if (p / "openpilot/selfdrive/carrot/radar/tools/radar_web_export.py").is_file()), None)


def response(ip, segment, sensor="auto", flip="recorded", retry=False):
    if not ip or not re.fullmatch(r"[A-Za-z0-9_-]+--\d+", segment):
        return 400, {"error": "기기와 녹화 구간을 먼저 선택해 주세요."}
    if sensor not in {"auto", "front", "corner"} or flip not in {"recorded", "normal", "flipped"}:
        return 400, {"error": "레이더 분석 옵션이 올바르지 않습니다."}
    engine = engine_root()
    if engine is None:
        return 422, {"error": "레이더 분석 엔진이 없습니다. 최신 전체 배포본으로 실행해 주세요."}
    # Jobs are shared only within this process: every restart uses fresh code and logs.
    key = hashlib.sha256(f"{ip}|{segment}|{sensor}|{flip}".encode()).hexdigest()
    with _lock:
        job = _jobs.get(key)
        if job and job["status"] == "error" and (retry or time.time() - job["created"] > 600):
            _jobs.pop(key, None)
            job = None
        if job:
            if job["status"] == "ready":
                return 200, job["payload"]
            if job["status"] == "error":
                return 422, {"error": job["error"]}
            return 202, {"status": "preparing", "message": job["message"]}
        if sum(j["status"] == "preparing" for j in _jobs.values()) >= 4:
            return 503, {"status": "busy"}
        # Limit resident JSON results and let the user retry failed jobs later.
        for old_key, old in list(_jobs.items()):
            if old["status"] != "preparing" and (len(_jobs) >= 8 or time.time() - old["created"] > 600):
                _jobs.pop(old_key, None)
        job = {"status": "preparing", "message": "분석 순서를 기다리고 있습니다…", "created": time.time()}
        _jobs[key] = job
        threading.Thread(target=_prepare, args=(job, engine, ip, segment, sensor, flip), daemon=True).start()
    return 202, {"status": "preparing", "message": job["message"]}


def _prepare(job, engine, ip, segment, sensor, flip):
    try:
        with _gate:
            CACHE.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="review-", dir=CACHE) as temporary:
                folder = Path(temporary)
                job["message"] = "이 구간의 로그를 기기에서 읽고 있습니다…"
                source = None
                for kind in ("rlog", "qlog"):
                    path = folder / f"{kind}.zst"
                    try:
                        with urllib.request.urlopen(f"http://{ip}:7000/api/dashcam/download/{segment}/{kind}", timeout=45) as remote, path.open("wb") as out:
                            expected = remote.headers.get("Content-Length")
                            size = 0
                            while chunk := remote.read(1024 * 1024):
                                size += len(chunk)
                                if size > 512 * 1024 * 1024:
                                    raise ValueError("분석용 로그가 512 MB 제한을 초과했습니다.")
                                out.write(chunk)
                            if expected and size != int(expected):
                                raise ValueError("로그를 모두 받지 못했습니다. 기기 연결을 확인하고 다시 불러와 주세요.")
                        if size:
                            source = path
                            break
                    except urllib.error.HTTPError as exc:
                        if exc.code not in (404, 410):
                            raise
                if source is None:
                    raise ValueError("이 구간에는 분석할 rlog 또는 qlog가 없습니다.")
                job["message"] = "레이더와 선행차 판정을 분석하고 있습니다…"
                output = folder / "radar.json"
                proc = subprocess.run([sys.executable, str(ROOT / "radar_export_runner.py"), str(engine), str(source), str(output), sensor, flip],
                                      cwd=str(ROOT), capture_output=True, timeout=300,
                                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if proc.returncode:
                    stderr = proc.stderr.decode("utf-8", errors="replace")
                    if "no aligned liveTracks/modelV2" in stderr:
                        raise ValueError("이 로그에 함께 기록된 레이더·모델 프레임이 없어 분석할 수 없습니다. 영상은 재생할 수 있습니다.")
                    if "ModuleNotFoundError" in stderr:
                        raise ValueError("레이더 분석 구성요소가 없습니다. 최신 전체 배포본으로 실행해 주세요.")
                    raise ValueError("로그 분석에 실패했습니다. 지원되지 않는 로그 형식이거나 기록이 손상되었을 수 있습니다.")
                payload = json.loads(output.read_text(encoding="utf-8"))
                if payload.get("schemaVersion") != 1 or not payload.get("frames"):
                    raise ValueError("이 구간에 표시할 레이더 프레임이 없습니다.")
                job.update(status="ready", payload=payload)
    except subprocess.TimeoutExpired:
        job.update(status="error", error="분석 시간이 5분을 초과했습니다. 더 짧은 구간으로 다시 시도해 주세요.")
    except Exception as exc:
        job.update(status="error", error=str(exc))
