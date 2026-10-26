#!/usr/bin/env python3
"""Command-line control for the altek Cubic.

Connect your computer to the camera's Wi-Fi (SSID starts with "Cubic"), then:

    python cubic_cli.py info
    python cubic_cli.py shoot
    python cubic_cli.py zoom tele 0.5
    python cubic_cli.py list
    python cubic_cli.py download --all -o photos/
    python cubic_cli.py live --frames 30 -o frames/
    python cubic_cli.py props
    python cubic_cli.py set EXPOSURE_COMP 1000
"""

import argparse
import os
import sys
import threading
import time

from cubic import CubicCamera, LiveView, Prop
from cubic.ptpip import Evt, PROP_NAMES, PTPError, parse_device_info


def main():
    ap = argparse.ArgumentParser(description="altek Cubic controller")
    ap.add_argument("--host", default="192.168.0.1")
    ap.add_argument("--port", type=int, default=15740)
    ap.add_argument("--rtsp-port", type=int, default=554)
    ap.add_argument("--rtp-port", type=int, default=65362)
    ap.add_argument("-v", "--verbose", action="store_true", help="print every packet-level step")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("info", help="connect and show camera status")
    sub.add_parser("props", help="dump all known property descriptors")
    s = sub.add_parser("set", help="set a property (name or hex code) to an integer value")
    s.add_argument("prop")
    s.add_argument("value", type=lambda x: int(x, 0))
    s = sub.add_parser("shoot", help="take a photo and save the quick-view JPEG")
    s.add_argument("-o", "--out", default=".")
    s.add_argument("--full", action="store_true", help="also download the full-size file")
    s = sub.add_parser("zoom", help="zoom wide/tele for N seconds")
    s.add_argument("direction", choices=["wide", "tele"])
    s.add_argument("seconds", type=float, nargs="?", default=0.3)
    s = sub.add_parser("record", help="record video for N seconds")
    s.add_argument("seconds", type=float)
    sub.add_parser("list", help="list files on the card")
    s = sub.add_parser("download", help="download files")
    s.add_argument("handles", nargs="*", type=lambda x: int(x, 0))
    s.add_argument("--all", action="store_true")
    s.add_argument("-o", "--out", default="downloads")
    s = sub.add_parser("delete", help="delete files by handle")
    s.add_argument("handles", nargs="+", type=lambda x: int(x, 0))
    s = sub.add_parser("live", help="save live-view frames as JPEG")
    s.add_argument("--frames", type=int, default=10)
    s.add_argument("-o", "--out", default="frames")
    s = sub.add_parser("raw", help="send any operation code (advanced)")
    s.add_argument("op", type=lambda x: int(x, 0))
    s.add_argument("params", nargs="*", type=lambda x: int(x, 0))
    a = ap.parse_args()

    log = (lambda m: print(f"  · {m}", file=sys.stderr)) if a.verbose else None
    cam = CubicCamera(a.host, a.port, log=log)
    try:
        cam.connect()
        info = cam.setup()
    except (OSError, PTPError) as e:
        sys.exit(f"連線失敗：{e}\n請確認電腦已連上相機的 Wi-Fi（名稱以 Cubic 開頭）。")

    try:
        run(a, cam, info)
    finally:
        cam.close()


def run(a, cam, info):
    if a.cmd == "info":
        print(f"韌體編號    : {info['firmware']}")
        print(f"電量        : {info['battery']}%")
        print(f"儲存        : {'microSD' if info['has_sd'] else '內建記憶體'} {[hex(x) for x in info['storage_ids']]}")
        print(f"可拍張數    : {info['photos_left']}")
        print(f"可錄秒數    : {cam.video_seconds_remaining()}")
        if "auto_power_off" in info:
            print(f"自動關機    : {info['auto_power_off']} 秒")
        try:
            d = cam.device_info()
            print(f"型號        : {d['manufacturer']} {d['model']} v{d['version']} SN {d['serial']}")
            print(f"支援的指令  : {' '.join(hex(x) for x in d['operations'])}")
            print(f"支援的屬性  : {' '.join(hex(x) for x in d['properties'])}")
        except PTPError as e:
            print(f"GetDeviceInfo 失敗：{e}")

    elif a.cmd == "props":
        codes = list(PROP_NAMES)
        try:
            codes = sorted(set(codes) | set(cam.device_info()["properties"]))
        except PTPError:
            pass
        for code in codes:
            try:
                print(cam.get_prop_desc(code))
            except PTPError as e:
                print(f"{PROP_NAMES.get(code, hex(code))}: {e}")

    elif a.cmd == "set":
        code = getattr(Prop, a.prop.upper(), None) or int(a.prop, 0)
        cam.set_prop(code, a.value)
        print(f"{PROP_NAMES.get(code, hex(code))} 已設為 {a.value}")

    elif a.cmd == "shoot":
        os.makedirs(a.out, exist_ok=True)
        qv_ready, added = threading.Event(), {}

        def on_event(ev):
            if ev.code == Evt.QV_COMPLETE:
                qv_ready.set()
            elif ev.code == Evt.OBJECT_ADDED and ev.params:
                added["h"] = ev.params[0]

        cam.event_handlers.append(on_event)
        cam.capture()
        print("已按下快門，等待相機…")
        if qv_ready.wait(10):
            path = os.path.join(a.out, time.strftime("cubic_qv_%Y%m%d_%H%M%S.jpg"))
            with open(path, "wb") as f:
                f.write(cam.get_quickview())
            print(f"預覽圖已存：{path}")
        else:
            print("沒收到 QV 完成事件（照片可能仍已存到卡上）")
        if a.full:
            for _ in range(50):
                if "h" in added:
                    break
                time.sleep(0.2)
            if "h" in added:
                save_object(cam, added["h"], a.out)

    elif a.cmd == "zoom":
        cam.zoom_step(a.direction == "tele", a.seconds)
        print("完成")

    elif a.cmd == "record":
        added = threading.Event()
        cam.event_handlers.append(lambda ev: ev.code == Evt.OBJECT_ADDED and added.set())
        cam.set_mode(video=True)
        cam.record_toggle()
        print(f"錄影中 {a.seconds} 秒…")
        time.sleep(a.seconds)
        cam.record_toggle()
        print("已停止，等待相機存檔…")
        print("已存檔" if added.wait(30) else "30 秒內沒收到新檔案事件")
        cam.set_mode(video=False)

    elif a.cmd == "list":
        hs = cam.list_handles()
        print(f"共 {len(hs)} 個檔案")
        for h in hs:
            oi = cam.object_info(h)
            print(f"  0x{h:08X}  {oi['filename']:<16} {oi['size']:>10,} B  {oi['width']}x{oi['height']}  {oi['capture_date']}")

    elif a.cmd == "download":
        os.makedirs(a.out, exist_ok=True)
        for h in (cam.list_handles() if a.all else a.handles):
            save_object(cam, h, a.out)

    elif a.cmd == "delete":
        for h in a.handles:
            cam.delete(h)
            print(f"已刪除 0x{h:08X}")

    elif a.cmd == "live":
        os.makedirs(a.out, exist_ok=True)
        lv = LiveView(a.host, a.rtsp_port, a.rtp_port, log=cam.log)
        lv.start()
        n = 0
        try:
            for i in range(a.frames):
                n, jpg = lv.wait_frame(n, timeout=5)
                if jpg is None:
                    print("5 秒內沒收到畫面（檢查防火牆是否擋住 UDP 65362）")
                    break
                path = os.path.join(a.out, f"frame_{i:04d}.jpg")
                with open(path, "wb") as f:
                    f.write(jpg)
            st = lv.stats()
            print(f"UDP 封包 {st['packets']} 個（非 JPEG {st['non_jpeg']}），組成 {st['frames']} 張，丟棄 {st['dropped']} 張，存在 {a.out}/")
            if st["frames"] == 0:
                print("\n--- 診斷資訊（請整段貼給 Claude）---")
                for m, r in lv.responses:
                    print(f"[{m}]\n{r.strip()}\n")
                for i, p in enumerate(lv.assembler.samples):
                    print(f"packet {i}: {p.hex()}")
        finally:
            lv.stop()

    elif a.cmd == "raw":
        r = cam.transaction(a.op, a.params, check=False)
        print(f"回應 0x{r.code:04X} 參數 {r.params}")
        if r.data:
            print(f"資料 {len(r.data)} bytes: {r.data[:256].hex()}")


def save_object(cam, h, out):
    oi = cam.object_info(h)
    path = os.path.join(out, oi["filename"] or f"{h:08X}.bin")
    with open(path, "wb") as f:
        n = cam.download(h, f, progress=lambda d, t: print(f"\r  {oi['filename']} {d * 100 // max(t, 1)}%", end=""))
    print(f"\r  {path}  ({n:,} bytes)")


if __name__ == "__main__":
    main()
