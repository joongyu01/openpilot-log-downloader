# -*- coding: utf-8 -*-
"""
로그 다운로더 — 로컬 서버 + 브라우저 UI.

    python log_downloader/server.py                 # 기기 자동 탐색
    python log_downloader/server.py --ip 192.168.0.30
    python log_downloader/server.py --no-browser

왜 HTML 단독이 아닌가:
  1. 기기(Carrot Web)에 CORS 헤더가 없다 → 브라우저가 기기 API를 직접 못 읽는다.
  2. 브라우저는 저장 위치를 못 고른다 → raw/ 에 넣으려면 서버가 받아야 한다.
  이 서버가 둘 다 해결한다. 브라우저는 localhost 만 보고, 파일은 서버가 raw/ 에 쓴다.

엔드포인트:
  GET  /                     UI
  GET  /dev/<path>           기기 API 프록시 (routes·segments·thumbnail·video)
  GET  /api/config           현재 기기 IP·raw 경로
  POST /api/scan             같은 대역에서 기기 찾기
  POST /api/setip            기기 IP 지정
  POST /api/download         {route, segments[], kinds[]} → 백그라운드 작업 시작
  GET  /api/job              진행 상황
  POST /api/cancel           중단
"""

import argparse
import ipaddress
import json
import math
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
import uuid
from urllib.parse import unquote, parse_qs
import radar_review
from recording_time import clock_samples, recover_times
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.normpath(os.path.join(HERE, "..",
    "downloads" if os.path.basename(HERE) == "app" and os.path.isdir(os.path.join(HERE, "resources")) else "raw"))
SETTINGS = os.path.join(HERE, "settings.json")
try:
    with open(SETTINGS, encoding="utf-8") as settings_file:
        saved_path = json.load(settings_file).get("downloadDirectory")
        if saved_path and os.path.isabs(saved_path):
            RAW = os.path.normpath(saved_path)
except (OSError, ValueError, TypeError):
    pass
KIND_EXT = {"rlog": "rlog.zst", "qlog": "qlog.zst", "qcamera": "qcamera.ts"}

STATE = {"ip": None}
JOB = {"active": False, "cancel": False, "done": 0, "total": 0, "bytes": 0,
       "current": "", "log": [], "out": "", "started": 0}
JOB_LOCK = threading.Lock()
TIME_CACHE = {}
OPENABLE_FOLDERS = set()
LAST_JOB = os.path.join(HERE, ".last-download.json")
try:
    with open(LAST_JOB, encoding="utf-8") as f:
        previous = json.load(f)
    if not previous.get("active") and previous.get("out"):
        JOB.update(previous)
        OPENABLE_FOLDERS.add(os.path.realpath(previous["out"]))
except (OSError, ValueError, TypeError):
    pass


# ─────────────────────────────────────────────── 기기 탐색

def local_ip():
    """이 PC 가 LAN 에서 쓰는 주소."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


def port_open(host, port=7000, timeout=0.6):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def is_carrot(host):
    try:
        with urllib.request.urlopen(f"http://{host}:7000/api/heartbeat_status", timeout=3) as r:
            return json.loads(r.read().decode()).get("ok") is True
    except Exception:
        return False


def scan_devices():
    """같은 /24 에서 7000 포트가 열리고 heartbeat 가 응답하는 호스트."""
    base = local_ip()
    if not base:
        return []
    net = ipaddress.ip_network(base + "/24", strict=False)
    hosts = [str(h) for h in net.hosts()]
    with ThreadPoolExecutor(max_workers=128) as ex:
        open_hosts = [h for h, ok in zip(hosts, ex.map(port_open, hosts)) if ok]
    return [h for h in open_hosts if is_carrot(h)]


# ─────────────────────────────────────────────── 다운로드

def dev_url(path):
    return f"http://{STATE['ip']}:7000{path}"


def download_one(segment, kind, out_dir):
    name = KIND_EXT[kind]
    dest = os.path.join(out_dir, f"{segment}--{name}")
    try:
        with urllib.request.urlopen(dev_url(f"/api/dashcam/download/{segment}/{kind}"),
                                    timeout=90) as r:
            total = int(r.headers.get("Content-Length") or 0)
            if os.path.isfile(dest) and total and os.path.getsize(dest) == total:
                return ("skip", total)
            tmp = dest + ".part"
            got = 0
            with open(tmp, "wb") as f:
                while True:
                    if JOB["cancel"]:
                        break
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    with JOB_LOCK:
                        JOB["bytes"] += len(chunk)
            if JOB["cancel"]:
                os.remove(tmp)
                return ("cancel", 0)
            if total and got != total:
                return (f"실패: 파일 크기 불일치 ({got}/{total})", 0)
            os.replace(tmp, dest)
            return ("ok", got)
    except urllib.error.HTTPError as e:
        return ("missing" if e.code == 404 else f"HTTP {e.code}", 0)
    except Exception as e:
        return (f"실패: {e}", 0)


def recording_folder(route, start_epoch, end_epoch, segment_count):
    """Use recording bounds in KST, never the PC's download date."""
    try:
        start, end = float(start_epoch), float(end_epoch)
        if not (math.isfinite(start) and math.isfinite(end) and 0 < start < end):
            raise ValueError("invalid recording time")
        kst = timezone(timedelta(hours=9))
        first = datetime.fromtimestamp(start, kst)
        last = datetime.fromtimestamp(end, kst)
        name = f"{first:%Y%m%d}_{first:%H%M}_{last:%H%M}({segment_count})"
        return os.path.join(RAW, name)
    except (TypeError, ValueError, OverflowError, OSError):
        # An unknown/corrupt device clock must not become today's date.
        return os.path.join(RAW, "날짜미확인", route)


def run_job(route, segments, kinds, out):
    try:
        os.makedirs(out, exist_ok=True)
        for seg in segments:
            if JOB["cancel"]:
                break
            for kind in kinds:
                if JOB["cancel"]:
                    break
                with JOB_LOCK:
                    JOB["current"] = f"{seg} · {kind}"
                status, size = download_one(seg, kind, out)
                with JOB_LOCK:
                    JOB["done"] += 1
                    key = {"ok": "ok", "skip": "skipped", "cancel": "cancelledFiles"}.get(status, "failed")
                    JOB[key] = JOB.get(key, 0) + 1
                    JOB["log"].append({"seg": seg, "kind": kind, "status": status, "size": size})
                    JOB["log"] = JOB["log"][-500:]
    except Exception as e:
        with JOB_LOCK:
            JOB["error"] = str(e)
    finally:
        with JOB_LOCK:
            JOB["active"] = False
            JOB["current"] = "중단됨" if JOB["cancel"] else "오류" if JOB.get("error") or JOB.get("failed") else "완료"
            try:
                with open(LAST_JOB + ".tmp", "w", encoding="utf-8") as f:
                    json.dump(JOB, f, ensure_ascii=False)
                os.replace(LAST_JOB + ".tmp", LAST_JOB)
            except OSError:
                pass


# ─────────────────────────────────────────────── HTTP

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError):
            pass

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/radar_view.js":
            with open(os.path.join(HERE, "radar_view.js"), "rb") as f:
                self._send(200, f.read(), "text/javascript; charset=utf-8")
        elif p.startswith("/api/radar-review/"):
            query = parse_qs(self.path.partition("?")[2])
            code, payload = radar_review.response(STATE["ip"], unquote(p.removeprefix("/api/radar-review/")),
                query.get("sensor", ["auto"])[0], query.get("radar_track_flip", ["recorded"])[0],
                retry=query.get("retry", ["0"])[0] == "1")
            self._send(code, payload)
        elif p in ("/", "/index.html"):
            f = os.path.join(HERE, "index.html")
            try:
                with open(f, "rb") as fh:
                    self._send(200, fh.read(), "text/html; charset=utf-8")
            except FileNotFoundError:
                self._send(500, "index.html 이 없다", "text/plain; charset=utf-8")
        elif p == "/api/config":
            self._send(200, {"ip": STATE["ip"], "raw": RAW,
                             "openDownloadFolder": True, "jobResults": True,
                             "folderNaming": "YYYYMMDD_HHmm_HHmm(count)", "timeRecovery": True})
        elif p.startswith("/api/route-times/"):
            route = unquote(p.removeprefix("/api/route-times/"))
            if not re.fullmatch(r"[A-Za-z0-9_-]+", route):
                return self._send(400, {"error": "잘못된 주행 이름"})
            try:
                with urllib.request.urlopen(dev_url(f"/api/dashcam/segments/{route}?limit=2000"), timeout=15) as response:
                    data = json.load(response)
                if data.get("hasMore"):
                    raise ValueError("전체 구간을 불러오지 못했습니다")
                cache_key = (STATE["ip"], route, json.dumps(data, sort_keys=True))
                cached = TIME_CACHE.get(cache_key)
                if cached and time.monotonic() - cached[0] < 30:
                    return self._send(200, cached[1])
                segments = sorted(data.get("segments", []), key=lambda s: int(s.rsplit("--", 1)[1]))
                if not segments:
                    raise ValueError("주행 구간이 없습니다")
                samples = {}
                probes = list(dict.fromkeys([segments[0], segments[-1]] + list(reversed(segments[-3:-1]))))
                for seg in probes:
                    last_samples = samples.get(segments[-1])
                    if seg not in (segments[0], segments[-1]) and (last_samples or getattr(last_samples, "start_mono", None) is not None):
                        break
                    with urllib.request.urlopen(dev_url(f"/api/dashcam/download/{seg}/qlog"), timeout=20) as response:
                        compressed = response.read(8 * 1024 * 1024 + 1)
                    if len(compressed) > 8 * 1024 * 1024:
                        raise ValueError("시간 확인용 로그 크기 제한 초과")
                    samples[seg] = clock_samples(compressed)
                    if seg not in (segments[0], segments[-1]) and samples[seg]:
                        break
                recovered = recover_times(data, samples)
                TIME_CACHE[cache_key] = (time.monotonic(), recovered)
                while len(TIME_CACHE) > 128:
                    TIME_CACHE.pop(next(iter(TIME_CACHE)))
                self._send(200, recovered)
            except Exception as e:
                self._send(422, {"error": str(e)})
        elif p == "/api/job":
            with JOB_LOCK:
                self._send(200, dict(JOB))
        elif p.startswith("/dev/"):
            if not STATE["ip"]:
                return self._send(503, {"error": "기기 IP 가 설정되지 않았다"})
            url = f"http://{STATE['ip']}:7000/{self.path[5:]}"
            try:
                with urllib.request.urlopen(url, timeout=30) as r:
                    self._send(200, r.read(),
                               r.headers.get("Content-Type", "application/octet-stream"))
            except urllib.error.HTTPError as e:
                self._send(e.code, {"error": f"기기가 거부했다 (HTTP {e.code})"})
            except Exception as e:
                self._send(502, {"error": f"기기에 못 붙는다: {e}"})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        global RAW
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n).decode() or "{}")
        except Exception:
            body = {}
        p = self.path.split("?")[0]
        if p == "/api/open-download-folder":
            origin = self.headers.get("Origin")
            if origin and origin not in (f"http://{self.headers.get('Host')}",):
                return self._send(403, {"error": "이 프로그램 화면에서 요청해 주세요"})
            path = os.path.realpath(str(body.get("path") or ""))
            if path not in OPENABLE_FOLDERS or not os.path.isdir(path):
                return self._send(400, {"error": "저장한 폴더를 찾을 수 없습니다"})
            try:
                os.startfile(path)
                self._send(200, {"ok": True})
            except Exception as e:
                self._send(500, {"error": f"탐색기를 열지 못했습니다: {e}"})
        elif p in ("/api/set-download-directory", "/api/choose-download-directory"):
            if JOB["active"]:
                return self._send(409, {"error": "다운로드가 끝난 후 저장 위치를 바꿔 주세요"})
            try:
                path = (body.get("path") or "").strip()
                if p == "/api/choose-download-directory":
                    picker = "import tkinter as t;from tkinter import filedialog;import json,sys;r=t.Tk();r.withdraw();r.attributes('-topmost',True);p=filedialog.askdirectory(title='로그 다운로드 폴더 선택',initialdir=sys.argv[1]);print(json.dumps(p));r.destroy()"
                    result = subprocess.run([sys.executable, "-c", picker, RAW],
                                            capture_output=True, text=True, check=True)
                    path = json.loads(result.stdout)
                    if not path:
                        return self._send(200, {"raw": RAW, "cancelled": True})
                if not path or not os.path.isabs(path):
                    return self._send(400, {"error": "전체 폴더 경로를 입력해 주세요"})
                path = os.path.normpath(path)
                os.makedirs(path, exist_ok=True)
                with open(SETTINGS + ".tmp", "w", encoding="utf-8") as f:
                    json.dump({"downloadDirectory": path}, f, ensure_ascii=False)
                os.replace(SETTINGS + ".tmp", SETTINGS)
                RAW = path
                self._send(200, {"raw": RAW})
            except Exception as e:
                self._send(400, {"error": f"저장 폴더를 지정하지 못했습니다: {e}"})
        elif p == "/api/scan":
            found = scan_devices()
            if found:
                STATE["ip"] = found[0]
            self._send(200, {"found": found, "ip": STATE["ip"]})
        elif p == "/api/setip":
            STATE["ip"] = (body.get("ip") or "").strip() or None
            self._send(200, {"ip": STATE["ip"]})
        elif p == "/api/download":
            if JOB["active"]:
                return self._send(409, {"error": "이미 받는 중이다"})
            route = (body.get("route") or "").strip()
            segs = body.get("segments") or []
            kinds = [k for k in (body.get("kinds") or []) if k in KIND_EXT]
            if not route or not segs or not kinds:
                return self._send(400, {"error": "route·세그먼트·종류가 필요하다"})
            if not re.fullmatch(r"[A-Za-z0-9_-]+", route) or any(
                not isinstance(seg, str) or not re.fullmatch(re.escape(route) + r"--\d+", seg)
                for seg in segs
            ):
                return self._send(400, {"error": "잘못된 주행 또는 구간 이름입니다"})
            segs = list(dict.fromkeys(segs))
            out = recording_folder(route, body.get("recordingStartEpoch"),
                                   body.get("recordingEndEpoch"), len(segs))
            with JOB_LOCK:
                if JOB["active"]:
                    return self._send(409, {"error": "이미 받는 중입니다"})
                job_id = uuid.uuid4().hex
                JOB.update(id=job_id, active=True, cancel=False, done=0, bytes=0, log=[],
                           out=out, total=len(segs)*len(kinds), started=time.time(), current="준비 중",
                           ok=0, skipped=0, failed=0, cancelledFiles=0, error=None)
                OPENABLE_FOLDERS.add(os.path.realpath(out))
            threading.Thread(target=run_job, args=(route, segs, kinds, out), daemon=True).start()
            self._send(200, {"ok": True, "id": job_id, "total": len(segs) * len(kinds), "out": out})
        elif p == "/api/cancel":
            JOB["cancel"] = True
            self._send(200, {"ok": True})
        else:
            self._send(404, {"error": "not found"})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ip", help="기기 IP (생략하면 매번 자동 탐색)")
    ap.add_argument("--port", type=int, default=8778)
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()

    url = f"http://127.0.0.1:{a.port}/"

    # 이미 떠 있으면 브라우저만 열고 끝낸다 (.bat 를 두 번 눌러도 안전)
    if port_open("127.0.0.1", a.port, timeout=0.4):
        print(f"이미 실행 중이다 → {url}")
        if not a.no_browser:
            webbrowser.open(url)
        return

    # IP 는 DHCP 라 계속 바뀐다 → 캐시하지 않고 시작할 때마다 스캔한다.
    if a.ip:
        STATE["ip"] = a.ip
    else:
        print("기기 탐색 중… (같은 /24, 7000 포트 + heartbeat)")
        t0 = time.time()
        found = scan_devices()
        if found:
            STATE["ip"] = found[0]
            print(f"  찾음: {', '.join(found)}  ({time.time()-t0:.1f}초)")
        else:
            print(f"  못 찾음 ({time.time()-t0:.1f}초) — 화면에서 IP 를 넣거나 「자동 탐색」을 누른다")

    print(f"기기 {STATE['ip'] or '(미설정)'}")
    print(f"저장 {RAW}")
    print(f"열기 {url}   (이 창을 닫으면 종료된다)")
    if not a.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        ThreadingHTTPServer(("127.0.0.1", a.port), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\n종료")


if __name__ == "__main__":
    main()
