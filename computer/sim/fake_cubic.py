"""Fake altek Cubic for testing without hardware.

Implements the camera side of what the original app expects: PTP/IP on
15740 (command + event channels), the vendor ops, and RTSP/RTP-JPEG live view.
It is a protocol simulator built from the decompiled app, not real firmware.

    python sim/fake_cubic.py --ptp-port 15740 --rtsp-port 8554
"""

from __future__ import annotations

import argparse
import io
import math
import re
import socket
import struct
import threading
import time

from PIL import Image, ImageDraw

OK = 0x2001


def recv_exact(s, n):
    b = bytearray()
    while len(b) < n:
        c = s.recv(n - len(b))
        if not c:
            raise ConnectionError
        b += c
    return bytes(b)


def read_pkt(s):
    ln = struct.unpack("<I", recv_exact(s, 4))[0]
    rest = recv_exact(s, ln - 4)
    return struct.unpack("<I", rest[:4])[0], rest[4:]


def ptp_str(s):
    if not s:
        return b"\x00"
    s += "\x00"
    return bytes([len(s)]) + s.encode("utf-16-le")


def make_jpeg(text, size=(640, 480), quality=80):
    img = Image.new("RGB", size, (30, 90, 140))
    d = ImageDraw.Draw(img)
    t = time.time()
    for i in range(12):
        x = size[0] / 2 + math.cos(t + i / 2) * 200
        y = size[1] / 2 + math.sin(t * 1.3 + i / 2) * 150
        d.ellipse([x - 20, y - 20, x + 20, y + 20], fill=(255, 200 - i * 10, 80))
    d.text((20, 20), text, fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality, subsampling=2, optimize=False)
    return buf.getvalue()


class FakeCubic:
    def __init__(self, ptp_port, rtsp_port, verbose=True, bad_timestamps=False):
        self.bad_timestamps = bad_timestamps
        self.ptp_port, self.rtsp_port, self.verbose = ptp_port, rtsp_port, verbose
        self.evt_sock = None
        self.session = False
        self.props = {
            0x5001: (2, 87),                     # battery UINT8
            0x5002: (4, 0x8001),
            0x5003: (6, 0),
            0x500C: (4, 1, [1, 2, 3]),           # flash UINT16 enum
            0x5010: (5, 0, [2000, 1000, 0, -1000, -2000]),
            0x5031: (6, 1, [1, 2]),              # scene: photo, video
            0x5032: (6, 1, [1, 2, 3, 4, 5, 6, 7]),
            0x5034: (6, 0),
            0x5035: (6, 1),
            0x5071: (6, 1, [1, 2, 3]),
            0x5081: (6, 3, [0, 1, 2, 3]),
            0x5091: (6, 2, [1, 2]),
        }
        self.objects = {}       # handle -> (name, jpeg)
        self.next_handle = 0x100
        self.zoom = 0
        self.recording = False
        self.log_ops = []
        for i in range(3):
            self._add_object(make_jpeg(f"stored photo {i + 1}", (1280, 960)))

    def log(self, *a):
        if self.verbose:
            print("[fake]", *a, flush=True)

    def _add_object(self, jpeg):
        h = self.next_handle
        self.next_handle += 1
        self.objects[h] = (f"CUBC{h:04d}.JPG", jpeg)
        return h

    # ---------------------------------------------------------- PTP/IP
    def serve_ptp(self):
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", self.ptp_port))
        srv.listen(4)
        while True:
            c, _ = srv.accept()
            threading.Thread(target=self.handle_conn, args=(c,), daemon=True).start()

    def handle_conn(self, c):
        try:
            t, p = read_pkt(c)
            if t == 1:
                guid = p[:16]
                name = p[16:-4]
                ver = struct.unpack("<I", p[-4:])[0]
                self.log(f"InitCommand guid={guid!r} name={name!r} ver=0x{ver:08X}")
                assert guid == b"cubic" + b"\xfc" * 11, "unexpected GUID"
                assert ver == 0x13000, "unexpected protocol version"
                body = struct.pack("<I", 1) + b"CUBC" + b"\x00" * 12 + ptp_str("Cubic") + struct.pack("<I", 0x10000)
                c.sendall(struct.pack("<II", 8 + len(body), 2) + body)
                self.command_loop(c)
            elif t == 3:
                assert struct.unpack("<I", p[:4])[0] == 1
                c.sendall(struct.pack("<II", 8, 4))
                self.evt_sock = c
                self.log("event channel up")
        except (ConnectionError, OSError):
            self.log("connection closed")

    def event(self, code, *params):
        if self.evt_sock:
            body = struct.pack("<HI", code, 0xFFFFFFFF) + b"".join(struct.pack("<I", x) for x in params)
            self.evt_sock.sendall(struct.pack("<II", 8 + len(body), 8) + body)

    def send_data(self, c, tid, data, chunk=200000):
        c.sendall(struct.pack("<IIIQ", 20, 9, tid, len(data)))
        pos = 0
        while len(data) - pos > chunk:
            c.sendall(struct.pack("<III", 12 + chunk, 10, tid) + data[pos:pos + chunk])
            pos += chunk
        c.sendall(struct.pack("<III", 12 + len(data) - pos, 12, tid) + data[pos:])

    def resp(self, c, tid, code=OK, *params):
        body = struct.pack("<HI", code, tid) + b"".join(struct.pack("<I", x & 0xFFFFFFFF) for x in params)
        c.sendall(struct.pack("<II", 8 + len(body), 7) + body)

    def command_loop(self, c):
        pending = None
        while True:
            t, p = read_pkt(c)
            if t == 13:
                c.sendall(struct.pack("<II", 8, 14))
                continue
            if t == 9:
                continue
            if t == 12:
                tid = struct.unpack_from("<I", p, 0)[0]
                op, params = pending
                self.handle_data_out(c, tid, op, params, p[4:])
                continue
            if t != 6:
                raise AssertionError(f"unexpected packet {t}")
            phase, op, tid = struct.unpack_from("<IHI", p, 0)
            n = (len(p) - 10) // 4
            params = list(struct.unpack_from(f"<{n}I", p, 10))
            self.log_ops.append((op, params))
            self.log(f"op 0x{op:04X} tid={tid} params={params}")
            if op == 0x1016:
                pending = (op, params)
                continue
            self.handle_op(c, tid, op, params)

    def handle_data_out(self, c, tid, op, params, data):
        prop = params[0]
        if prop == 0x5011:
            n = data[0]
            self.log("datetime set to", data[1:1 + 2 * n].decode("utf-16-le"))
        else:
            v = struct.unpack("<i", data[:4])[0]
            dt, _, *rest = self.props.get(prop, (5, 0))
            self.props[prop] = (dt, v, *rest)
            self.log(f"set prop 0x{prop:04X} = {v}")
            self.event(0x4006, prop)
        self.resp(c, tid)

    def handle_op(self, c, tid, op, params):
        fmt = {2: "<B", 4: "<H", 5: "<i", 6: "<I"}
        if op == 0x1002:
            self.session = True
            self.listed = sorted(self.objects)   # real camera: list frozen until reconnect
            return self.resp(c, tid)
        if not self.session:
            return self.resp(c, tid, 0x2003)
        if op == 0x1003:
            self.session = False
            return self.resp(c, tid)
        if op == 0x1001:
            data = struct.pack("<HIH", 100, 0x6, 100) + ptp_str("altek") + struct.pack("<H", 0)
            ops = [0x1001, 0x1002, 0x1003, 0x1004, 0x1007, 0x1008, 0x101B, 0x90B1, 0x90B7]
            for arr in (ops, [0x4002, 0x400D], list(self.props), [0x3801], [0x3801]):
                data += struct.pack("<I", len(arr)) + struct.pack(f"<{len(arr)}H", *arr)
            data += ptp_str("altek") + ptp_str("Cubic") + ptp_str("1.15") + ptp_str("FAKE0001")
            self.send_data(c, tid, data)
            return self.resp(c, tid)
        if op == 0x1004:
            self.send_data(c, tid, struct.pack("<II", 1, 0x00010001))
            return self.resp(c, tid)
        if op == 0x1014:
            prop = params[0]
            if prop not in self.props:
                return self.resp(c, tid, 0x200A)
            dt, cur, *rest = self.props[prop]
            f = fmt[dt]
            data = struct.pack("<HHB", prop, dt, 1) + struct.pack(f, cur) + struct.pack(f, cur)
            if rest:
                vals = rest[0]
                data += b"\x02" + struct.pack("<H", len(vals)) + b"".join(struct.pack(f, v) for v in vals)
            else:
                data += b"\x00"
            self.send_data(c, tid, data)
            return self.resp(c, tid)
        if op == 0x1015:
            prop = params[0]
            if prop not in self.props:
                return self.resp(c, tid, 0x200A)
            dt, cur, *_ = self.props[prop]
            self.send_data(c, tid, struct.pack(fmt[dt], cur))
            return self.resp(c, tid)
        if op == 0x90D8:
            return self.resp(c, tid, OK, 11500)
        if op == 0x90D3:
            return self.resp(c, tid, OK, 512 if params[0] == 1 else 600)
        if op == 0x90DA:
            return self.resp(c, tid, OK, 300)
        if op in (0x90D9, 0x90DC, 0x90D6, 0x90DB, 0x90B2, 0x90B5):
            return self.resp(c, tid)
        if op == 0x90B1:  # S1 lock -> focus result: count, then (count+1) * 5 shorts
            data = struct.pack("<I", 1) + struct.pack("<hhhhh", 0, 0, 0, 0, 0) + struct.pack("<hhhhh", 1, 280, 200, 360, 280)
            self.send_data(c, tid, data)
            return self.resp(c, tid)
        if op == 0x100E and self.props[0x5031][1] == 2:   # video mode: shutter toggles recording
            # real camera: answers DeviceBusy when stopping, but stops and saves anyway
            self.resp(c, tid, 0x2019 if self.recording else OK)
            if not self.recording:
                self.event(0x400D)
            self.recording = not self.recording
            self.log("recording" if self.recording else "stopped recording")
            if not self.recording:
                def saved():
                    time.sleep(0.8)
                    h = self.next_handle
                    self.next_handle += 1
                    self.objects[h] = (f"CUBC{h:04d}.MOV", b"\x00\x00\x00\x14ftypqt  " + b"\x00" * 50000)
                    self.event(0x4002, h)
                threading.Thread(target=saved, daemon=True).start()
            return
        if op == 0x100E:
            self.resp(c, tid)

            def after():
                time.sleep(0.3)
                self.event(0x400D)
                self.last_shot = make_jpeg("NEW SHOT", (1280, 960))
                h = self._add_object(self.last_shot)
                time.sleep(0.2)
                self.event(0xC0A1)
                time.sleep(0.2)
                self.event(0x4002, h)
            threading.Thread(target=after, daemon=True).start()
            return
        if op == 0x90B3:
            self.send_data(c, tid, make_jpeg("quick view", (640, 480)))
            return self.resp(c, tid)
        if op == 0x90B7:
            if params[1] == 1:
                self.zoom = max(0, min(10, self.zoom + (1 if params[0] else -1)))
            self.log("zoom level", self.zoom)
            return self.resp(c, tid)
        if op == 0x90C1:   # hidden in the real app; real camera saves nothing
            self.log("VIDEO_REC_KEY (no file)")
            return self.resp(c, tid)
        if op == 0x1007:
            hs = [h for h in getattr(self, "listed", sorted(self.objects)) if h in self.objects]
            self.send_data(c, tid, struct.pack(f"<I{len(hs)}I", len(hs), *hs))
            return self.resp(c, tid)
        if op in (0x1008, 0x100A, 0x101B, 0x100B):
            h = params[0]
            if h not in self.objects:
                return self.resp(c, tid, 0x2009)
            name, jpeg = self.objects[h]
            if op == 0x1008:
                data = struct.pack("<IHHIHIIIIIIIHII", 0x00010001, 0x3801, 0, len(jpeg), 0x3808, 0, 160, 120,
                                   1280, 960, 24, 0, 0, 0, 0)
                data += ptp_str(name) + ptp_str("20140101T120000") + ptp_str("") + ptp_str("")
                self.send_data(c, tid, data)
            elif op == 0x100A:
                if not name.endswith(".JPG"):
                    return self.resp(c, tid, 0x2010)
                im = Image.open(io.BytesIO(jpeg))
                im.thumbnail((160, 120))
                b = io.BytesIO()
                im.save(b, "JPEG")
                self.send_data(c, tid, b.getvalue())
            elif op == 0x101B:
                off, mx = params[1], params[2]
                self.send_data(c, tid, jpeg[off:off + mx])
            else:
                del self.objects[h]
            return self.resp(c, tid)
        if op == 0x100F:
            self.objects.clear()
            return self.resp(c, tid)
        self.log(f"unsupported op 0x{op:04X}")
        return self.resp(c, tid, 0x2005)

    # ---------------------------------------------------------- RTSP / RTP
    def serve_rtsp(self):
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", self.rtsp_port))
        srv.listen(2)
        while True:
            c, addr = srv.accept()
            threading.Thread(target=self.handle_rtsp, args=(c, addr), daemon=True).start()

    def handle_rtsp(self, c, addr):
        client_port = None
        alive = {"on": True}
        try:
            while True:
                req = c.recv(4096).decode()
                if not req:
                    alive["on"] = False
                    return
                first = req.splitlines()[0]
                self.log("RTSP", first)
                assert first.endswith("/MJPG RTSP/1.0"), first
                cseq = re.search(r"CSeq: (\d+)", req).group(1)
                extra = ""
                if first.startswith("DESCRIBE"):
                    sdp = "v=0\r\nm=video 0 RTP/AVP 26\r\n"
                    extra = f"Content-Type: application/sdp\r\nContent-Length: {len(sdp)}\r\n\r\n{sdp}"
                elif first.startswith("SETUP"):
                    client_port = int(re.search(r"client_port=(\d+)", req).group(1))
                c.sendall(f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\n{extra or chr(13) + chr(10)}".encode())
                if first.startswith("PLAY"):
                    threading.Thread(target=self.stream, args=(addr[0], client_port, alive), daemon=True).start()
        except (ConnectionError, OSError):
            alive["on"] = False

    @staticmethod
    def split_jpeg(jpeg):
        """Return (luma table, chroma table, w, h, scan data) from a baseline JPEG."""
        pos, tables, w, h = 2, {}, 0, 0
        while True:
            assert jpeg[pos] == 0xFF
            m = jpeg[pos + 1]
            ln = struct.unpack(">H", jpeg[pos + 2:pos + 4])[0]
            seg = jpeg[pos + 4:pos + 2 + ln]
            if m == 0xDB:
                i = 0
                while i < len(seg):
                    tables[seg[i] & 0x0F] = seg[i + 1:i + 65]
                    i += 65
            elif m == 0xC0:
                h, w = struct.unpack(">HH", seg[1:5])
            elif m == 0xDA:
                scan = jpeg[pos + 2 + ln:]
                if scan.endswith(b"\xff\xd9"):
                    scan = scan[:-2]
                return tables[0], tables[1], w, h, scan
            pos += 2 + ln

    def stream(self, host, port, alive):
        self.log(f"streaming RTP JPEG to {host}:{port}")
        u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        seq, n = 0, 0
        try:
            while alive["on"]:
                jpeg = make_jpeg(f"LIVE frame {n}  zoom={self.zoom}{'  REC' if self.recording else ''}")
                lq, cq, w, h, scan = self.split_jpeg(jpeg)
                ts = int(time.time() * 90000) & 0xFFFFFFFF
                off, first = 0, True
                while off < len(scan):
                    chunk = scan[off:off + 1400]
                    last = off + len(chunk) >= len(scan)
                    pts = (ts + seq * 7) & 0xFFFFFFFF if self.bad_timestamps else ts
                    rtp = struct.pack(">BBHII", 0x80, 26 | (0x80 if last else 0), seq & 0xFFFF, pts, 0x12345678)
                    # alternate between in-band tables (Q=255) and standard Q=80
                    q = 255 if n % 2 == 0 else 80
                    jh = struct.pack(">I", off) + bytes([1, q, w // 8, h // 8])
                    if first and q >= 128:
                        jh += struct.pack(">BBH", 0, 0, 128) + lq + cq
                    u.sendto(rtp + jh + chunk, (host, port))
                    off += len(chunk)
                    seq += 1
                    first = False
                n += 1
                time.sleep(1 / 15)
        except OSError:
            self.log("stream stopped")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ptp-port", type=int, default=15740)
    ap.add_argument("--rtsp-port", type=int, default=8554)
    ap.add_argument("-q", "--quiet", action="store_true")
    ap.add_argument("--bad-timestamps", action="store_true", help="different RTP timestamp on every packet")
    a = ap.parse_args()
    cam = FakeCubic(a.ptp_port, a.rtsp_port, verbose=not a.quiet, bad_timestamps=a.bad_timestamps)
    threading.Thread(target=cam.serve_rtsp, daemon=True).start()
    print(f"fake Cubic listening: PTP/IP 127.0.0.1:{a.ptp_port}, RTSP 127.0.0.1:{a.rtsp_port}", flush=True)
    cam.serve_ptp()


if __name__ == "__main__":
    main()
