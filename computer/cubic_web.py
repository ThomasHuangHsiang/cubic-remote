#!/usr/bin/env python3
"""Cubic Remote — browser-based replacement for the discontinued Cubic Camera app.

1. Join the camera's Wi-Fi (SSID starts with "Cubic").
2. python cubic_web.py
3. Your browser opens http://127.0.0.1:8080

Only the Python standard library is needed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
import traceback
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from cubic import CubicCamera, LiveView, Prop
from cubic.ptpip import Evt, PTPError, RESP_NAMES

HERE = os.path.dirname(os.path.abspath(__file__))

# Settings exposed in the UI. "kind": prop = SetDevicePropValue, op = vendor op.
SETTINGS = [
    {"key": "ev", "label": "亮度", "kind": "prop", "code": Prop.EXPOSURE_COMP, "default": 0,
     "options": [[2000, "最亮"], [1000, "微亮"], [0, "正常"], [-1000, "微暗"], [-2000, "最暗"]]},
    {"key": "flash", "label": "閃光燈", "kind": "prop", "code": Prop.FLASH_MODE, "default": 1,
     "options": [[1, "自動"], [2, "關閉"], [3, "強制"]]},
    {"key": "res", "label": "相片解析度", "kind": "prop", "code": Prop.STILL_RESOLUTION, "default": 0,
     "options": [[0, "13M"], [1, "10M"], [2, "8M"]]},
    {"key": "hq", "label": "儲存高解析度相片", "kind": "prop", "code": Prop.SAVE_HIGH_QUALITY, "default": 1,
     "options": [[1, "開"], [0, "關"]]},
    {"key": "skin", "label": "美肌", "kind": "prop", "code": Prop.SOFT_SKIN, "default": 0,
     "options": [[3, "強"], [2, "中"], [1, "弱"], [0, "關"]]},
    {"key": "touch", "label": "觸控拍攝", "kind": "prop", "code": Prop.TOUCH_SHOT, "default": 2,
     "options": [[1, "開"], [2, "關"]]},
    {"key": "video", "label": "錄影模式", "kind": "videosize", "code": Prop.VIDEO_SIZE, "default": 0,
     "options": [[0, "Full HD"], [1, "YouTube (≤60 秒)"], [2, "Instagram (≤15 秒)"]]},
    {"key": "timer", "label": "定時拍照", "kind": "op", "op": "self_timer", "default": 0,
     "options": [[0, "關"], [3, "3 秒"]]},
    {"key": "macro", "label": "近拍模式", "kind": "op", "op": "macro", "default": 0,
     "options": [[0, "關"], [1, "開"]]},
    {"key": "sound", "label": "相機聲音", "kind": "prop", "code": Prop.SOUND_VOLUME, "default": 3,
     "options": [[3, "開"], [0, "關"]]},
    {"key": "apo", "label": "自動關機", "kind": "op", "op": "apo", "default": 300,
     "options": [[60, "1 分鐘"], [300, "5 分鐘"], [57600, "關閉"]]},
]


class Service:
    def __init__(self, host, port, rtsp_port, rtp_port, outdir):
        self.host, self.port, self.rtsp_port, self.rtp_port = host, port, rtsp_port, rtp_port
        self.outdir = outdir
        self.cam: CubicCamera | None = None
        self.lv: LiveView | None = None
        self.info: dict = {}
        self.state = "disconnected"
        self.error = ""
        self.logs = deque(maxlen=300)
        self.log_seq = 0
        self.values = {s["key"]: s["default"] for s in SETTINGS}
        self.video_mode = False
        self.recording = False
        self.rec_started = 0.0
        self.rec_limit = 0
        self.last_shot: bytes | None = None
        self.last_shot_no = 0
        self.busy = threading.Lock()
        # GetObjectHandles does not update until reconnect; remember OBJECT_ADDED handles (like the app)
        self.added_handles: list[int] = []
        self.connect_lock = threading.Lock()

    def log(self, msg):
        self.log_seq += 1
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        self.logs.append((self.log_seq, line))
        print(line, flush=True)

    # ------------------------------------------------------------ connection
    def connect(self):
        if not self.connect_lock.acquire(blocking=False):
            return
        try:
            self.disconnect(quiet=True)
            self.state, self.error = "connecting", ""
            self.log(f"連線到相機 {self.host} …")
            cam = CubicCamera(self.host, self.port, log=self._camlog)
            cam.connect()
            cam.event_handlers.append(self._on_event)
            self.cam = cam
            self.added_handles = []
            self.info = cam.setup()
            self.info["video_left"] = cam.video_seconds_remaining()
            self.log(f"已連線：韌體 {self.info['firmware']}、電量 {self.info['battery']}%、可拍 {self.info['photos_left']} 張")
            self._read_back_settings()
            try:
                cam.sync_clock()
            except PTPError as e:
                self.log(f"同步時間失敗：{e}")
            self.lv = LiveView(self.host, self.rtsp_port, self.rtp_port, log=self._camlog)
            try:
                self.lv.start()
                self.log("即時預覽已啟動")
            except OSError as e:
                self.log(f"即時預覽啟動失敗：{e}")
            self.state = "connected"
            threading.Thread(target=self._status_loop, args=(cam,), daemon=True).start()
        except Exception as e:
            self.state, self.error = "error", str(e)
            self.log(f"連線失敗：{e}")
            self.disconnect(quiet=True)
            self.state = "error"
        finally:
            self.connect_lock.release()

    def _status_loop(self, cam):
        # refresh battery / remaining count every 30 s while this session lasts
        while self.cam is cam:
            time.sleep(30)
            if self.cam is cam and self.state == "connected" and not self.recording:
                try:
                    self.refresh()
                except Exception as e:
                    self.log(f"更新狀態失敗：{e}")

    def disconnect(self, quiet=False):
        if self.lv:
            self.lv.stop()
            self.lv = None
        if self.cam:
            self.cam.close()
            self.cam = None
        self.recording = False
        if not quiet:
            self.state = "disconnected"
            self.log("已中斷連線")

    def _camlog(self, msg):
        if msg.startswith(("event:", "RTSP", "firmware", "InitCommandAck", "first RTP", "bad RTP")) or "->" in msg and "OK" not in msg:
            self.log("  " + msg)

    def _read_back_settings(self):
        cam = self.cam
        for s in SETTINGS:
            if s["kind"] in ("prop", "videosize"):
                try:
                    d = cam.descs.get(s["code"]) or cam.get_prop_desc(s["code"])
                    if s["kind"] == "videosize":
                        if d.current in d.enum:
                            self.values[s["key"]] = d.enum.index(d.current)
                    else:
                        self.values[s["key"]] = d.current
                except (PTPError, KeyError, ValueError):
                    pass
        if "auto_power_off" in self.info:
            self.values["apo"] = self.info["auto_power_off"]
        try:
            scene = cam.descs.get(Prop.SCENE_MODE) or cam.get_prop_desc(Prop.SCENE_MODE)
            self.video_mode = len(scene.enum) > 1 and scene.current == scene.enum[1]
        except PTPError:
            pass

    def require(self) -> CubicCamera:
        if not self.cam or self.state != "connected":
            raise PTPError("尚未連線到相機")
        return self.cam

    # ------------------------------------------------------------ events
    def _on_event(self, ev):
        if ev.code == Evt.QV_COMPLETE:
            threading.Thread(target=self._fetch_qv, daemon=True).start()
        elif ev.code == Evt.OBJECT_ADDED:
            self.log(f"新檔案 handle=0x{ev.params[0]:X}" if ev.params else "新檔案")
            if ev.params and ev.params[0] not in self.added_handles:
                self.added_handles.append(ev.params[0])
            if self.recording and self.video_mode and time.time() - self.rec_started > 2:
                # a file appearing while we think we are recording means the camera stopped on its own
                self.recording = False
                self.log("相機已停止錄影並存檔")
        elif ev.code == Evt.SELF_TIMER_SHOT:
            self.log("自拍倒數中…")
        elif ev.code in (Evt.AUTO_POWER_OFF, Evt.DSC_POWER_OFF):
            self.log("相機即將關機")
        elif ev.code == Evt.CAMERA_ERROR:
            self.log(f"相機回報錯誤 {ev.params}")

    def _fetch_qv(self):
        try:
            jpg = self.require().get_quickview()
            if jpg:
                self.last_shot = jpg
                self.last_shot_no += 1
                os.makedirs(self.outdir, exist_ok=True)
                path = os.path.join(self.outdir, time.strftime("cubic_%Y%m%d_%H%M%S.jpg"))
                with open(path, "wb") as f:
                    f.write(jpg)
                self.log(f"預覽圖已存到 {path}")
            self.info["photos_left"] = self.cam.photos_remaining()
        except Exception as e:
            self.log(f"取得預覽圖失敗：{e}")

    # ------------------------------------------------------------ actions
    def status(self):
        d = {"state": self.state, "error": self.error, "info": self.info, "values": self.values,
             "video_mode": self.video_mode, "recording": self.recording,
             "rec_seconds": int(time.time() - self.rec_started) if self.recording else 0,
             "rec_limit": self.rec_limit,
             "last_shot_no": self.last_shot_no, "log_seq": self.log_seq,
             "live": bool(self.lv and self.lv.frame_no), "settings": SETTINGS,
             "lv_stats": self.lv.stats() if self.lv else None}
        return d

    def restart_live(self):
        self.require()
        if self.lv:
            self.lv.stop()
        self.lv = LiveView(self.host, self.rtsp_port, self.rtp_port, log=self._camlog)
        self.lv.start()
        self.log("即時預覽已重新啟動")

    def live_diag(self):
        lv = self.lv
        if not lv:
            return {"running": False}
        return {"running": True, "stats": lv.stats(),
                "rtsp": [{"method": m, "response": r} for m, r in lv.responses],
                "samples": [p.hex() for p in lv.assembler.samples]}

    def refresh(self):
        cam = self.require()
        self.info["battery"] = cam.battery()
        self.info["photos_left"] = cam.photos_remaining()
        self.info["video_left"] = cam.video_seconds_remaining()

    def shoot(self):
        cam = self.require()
        if self.video_mode:
            raise PTPError("目前是錄影模式")
        code = cam.capture(beep=self.values.get("sound", 3) != 0)
        self.log("快門" if code == 0x2001 else f"快門（相機回應 {RESP_NAMES.get(code, hex(code))}）")

    def touch(self, x, y):
        self.require().touch_shot(int(x), int(y))
        self.log(f"觸控拍攝 ({int(x)}, {int(y)})")

    def zoom(self, tele, press):
        self.require().zoom(bool(tele), bool(press))

    def set_mode(self, video):
        cam = self.require()
        if self.recording:
            raise PTPError("錄影中不能切換")
        cam.set_mode(bool(video))
        self.video_mode = bool(video)
        self.log("切換到錄影模式" if video else "切換到拍照模式")

    def record(self):
        cam = self.require()
        with self.busy:
            if not self.video_mode:
                self.set_mode(True)
            if not self.recording:
                # same limits as the app: YouTube 61 s, Instagram 16 s, Full HD = remaining time
                left = cam.video_seconds_remaining()
                cap = {1: 61, 2: 16}.get(self.values.get("video", 0))
                self.rec_limit = min(cap, left) if cap else left
                if self.rec_limit <= 0:
                    raise PTPError("記憶空間不足，無法錄影")
                code = cam.record_toggle(beep=self.values.get("sound", 3) != 0)
                if code != 0x2001:
                    self.log(f"  （開始錄影時相機回應 {RESP_NAMES.get(code, hex(code))}）")
                self.recording = True
                self.rec_started = time.time()
                self.log(f"開始錄影（最長 {self.rec_limit} 秒）")
                threading.Thread(target=self._rec_watchdog, args=(self.rec_started,), daemon=True).start()
            else:
                code = cam.record_toggle(beep=self.values.get("sound", 3) != 0)
                if code != 0x2001:
                    # the camera answers DeviceBusy while it is already stopping; the app ignores it too
                    self.log(f"  （停止時相機回應 {RESP_NAMES.get(code, hex(code))}，屬正常現象）")
                self.recording = False
                self.log(f"停止錄影（{int(time.time() - self.rec_started)} 秒），相機存檔中…")

    def _rec_watchdog(self, started):
        while self.recording and self.rec_started == started:
            if time.time() - started >= self.rec_limit:
                self.log("已達錄影時間上限，自動停止")
                try:
                    self.record()
                except Exception as e:
                    self.log(f"自動停止失敗：{e}")
                return
            time.sleep(0.5)

    def set_setting(self, key, value):
        cam = self.require()
        s = next(x for x in SETTINGS if x["key"] == key)
        value = int(value)
        if s["kind"] == "prop":
            cam.set_prop(s["code"], value)
        elif s["kind"] == "videosize":
            cam.set_prop(s["code"], cam.enum_value(s["code"], value))
        elif s["op"] == "self_timer":
            cam.set_self_timer(value)
        elif s["op"] == "macro":
            cam.set_macro(bool(value))
        elif s["op"] == "apo":
            cam.set_auto_power_off(value)
        self.values[key] = value
        label = dict((o[0], o[1]) for o in s["options"]).get(value, value)
        self.log(f"{s['label']} → {label}")

    def files(self):
        cam = self.require()
        # like the app: browse mode while reading the card, then back to control mode
        try:
            cam.set_prop(Prop.FUNCTIONAL_MODE, 0x8001)
        except PTPError as e:
            self.log(f"切換瀏覽模式失敗：{e}")
        try:
            try:
                listed = cam.list_handles()
            except PTPError as e:
                self.log(f"讀取檔案清單失敗：{e}")
                listed = []
            order = []
            for h in list(reversed(self.added_handles)) + list(reversed(listed)):
                if h not in order:
                    order.append(h)
            out = []
            for h in order:
                try:
                    oi = cam.object_info(h)
                    out.append({"handle": h, "name": oi["filename"], "size": oi["size"],
                                "w": oi["width"], "h": oi["height"], "date": oi["capture_date"]})
                except PTPError as e:
                    out.append({"handle": h, "name": f"0x{h:X}", "error": str(e)})
            return out
        finally:
            try:
                cam.set_prop(Prop.FUNCTIONAL_MODE, 0x8002)
                if cam.firmware > 11000:
                    cam.set_prop(Prop.EXPOSURE_COMP, self.values.get("ev", 0))
                    cam.set_prop(Prop.STILL_RESOLUTION, self.values.get("res", 0))
            except PTPError as e:
                self.log(f"切回遙控模式失敗：{e}")


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    svc: Service = None  # set in main
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body: bytes, ctype="application/json", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode())

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def do_GET(self):
        svc, path = self.svc, urlparse(self.path).path
        try:
            if path == "/":
                with open(os.path.join(HERE, "web", "index.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if path == "/api/status":
                return self._json(svc.status())
            if path == "/api/log":
                return self._json([line for _, line in svc.logs])
            if path == "/live.jpg":
                if not svc.lv or not svc.lv.latest:
                    return self._send(404, b"no frame", "text/plain")
                return self._send(200, svc.lv.latest, "image/jpeg")
            if path == "/stream.mjpg":
                return self._mjpeg()
            if path == "/last.jpg":
                if not svc.last_shot:
                    return self._send(404, b"none", "text/plain")
                return self._send(200, svc.last_shot, "image/jpeg")
            if path == "/api/livediag":
                return self._json(svc.live_diag())
            if path == "/api/files":
                return self._json(svc.files())
            m = re.match(r"^/api/thumb/(\d+)$", path)
            if m:
                return self._send(200, svc.require().thumbnail(int(m.group(1))), "image/jpeg",
                                  {"Cache-Control": "max-age=3600"})
            m = re.match(r"^/api/file/(\d+)$", path)
            if m:
                return self._download(int(m.group(1)))
            self._send(404, b"not found", "text/plain")
        except (PTPError, OSError) as e:
            self._json({"error": str(e)}, 500)
        except Exception as e:
            traceback.print_exc()
            self._json({"error": repr(e)}, 500)

    def do_POST(self):
        svc, path = self.svc, urlparse(self.path).path
        try:
            b = self._body()
            if path == "/api/connect":
                threading.Thread(target=svc.connect, daemon=True).start()
            elif path == "/api/disconnect":
                svc.disconnect()
            elif path == "/api/restartlive":
                svc.restart_live()
            elif path == "/api/refresh":
                svc.refresh()
            elif path == "/api/shoot":
                svc.shoot()
            elif path == "/api/touch":
                svc.touch(b["x"], b["y"])
            elif path == "/api/zoom":
                svc.zoom(b["tele"], b["press"])
            elif path == "/api/mode":
                svc.set_mode(b["video"])
            elif path == "/api/record":
                svc.record()
            elif path == "/api/setting":
                svc.set_setting(b["key"], b["value"])
            elif (m := re.match(r"^/api/delete/(\d+)$", path)):
                svc.require().delete(int(m.group(1)))
                if int(m.group(1)) in svc.added_handles:
                    svc.added_handles.remove(int(m.group(1)))
                svc.log(f"已刪除 0x{int(m.group(1)):X}")
            elif path == "/api/format":
                svc.require().format_card()
                svc.added_handles = []
                svc.log("記憶卡已格式化")
            else:
                return self._send(404, b"not found", "text/plain")
            self._json({"ok": True})
        except (PTPError, OSError, KeyError, StopIteration) as e:
            self._json({"error": str(e)}, 400)
        except Exception as e:
            traceback.print_exc()
            self._json({"error": repr(e)}, 500)

    def _mjpeg(self):
        lv = self.svc.lv
        if not lv:
            return self._send(503, b"live view not running", "text/plain")
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        n = 0
        try:
            while self.svc.lv is lv:
                n, jpg = lv.wait_frame(n, timeout=2)
                if jpg is None:
                    continue
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(jpg))
                self.wfile.write(jpg)
                self.wfile.write(b"\r\n")
        except (BrokenPipeError, ConnectionResetError):
            pass
        self.close_connection = True

    def _download(self, handle):
        cam = self.svc.require()
        oi = cam.object_info(handle)
        self.send_response(200)
        ext = os.path.splitext(oi["filename"].upper())[1]
        ctype = {".JPG": "image/jpeg", ".MOV": "video/quicktime", ".MP4": "video/mp4", ".AVI": "video/x-msvideo"}.get(ext, "application/octet-stream")
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(oi["size"]))
        self.send_header("Content-Disposition", f'attachment; filename="{oi["filename"]}"')
        self.end_headers()
        cam.download(handle, self.wfile)


def main():
    ap = argparse.ArgumentParser(description="Cubic Remote web app")
    ap.add_argument("--camera", default="192.168.0.1", help="camera IP (default 192.168.0.1)")
    ap.add_argument("--ptp-port", type=int, default=15740)
    ap.add_argument("--rtsp-port", type=int, default=554)
    ap.add_argument("--rtp-port", type=int, default=65362)
    ap.add_argument("--listen", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--out", default=os.path.join(HERE, "photos"), help="where quick-view photos are saved")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--no-autoconnect", action="store_true")
    a = ap.parse_args()

    svc = Service(a.camera, a.ptp_port, a.rtsp_port, a.rtp_port, a.out)
    Handler.svc = svc
    srv = ThreadingHTTPServer((a.listen, a.port), Handler)
    srv.daemon_threads = True
    url = f"http://{'127.0.0.1' if a.listen in ('0.0.0.0', '') else a.listen}:{a.port}"
    print(f"Cubic Remote 已啟動：{url}  (Ctrl+C 結束)")
    if not a.no_autoconnect:
        threading.Thread(target=svc.connect, daemon=True).start()
    if not a.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        svc.disconnect(quiet=True)


if __name__ == "__main__":
    main()
