"""Live view: RTSP session + RTP/JPEG (RFC 2435) depacketizer.

Mirrors the original app: it opens rtsp://<cam>/MJPG on TCP 554, sends
OPTIONS / DESCRIBE / SETUP (client_port=65362-65363) / PLAY, then the camera
streams RTP JPEG fragments to UDP 65362 on this machine. Each frame is rebuilt
into a complete JPEG using the app's own header template.
"""

from __future__ import annotations

import random
import socket
import struct
import threading
import time
from typing import Callable, Optional

from .jpeg_header import CHROMA_Q_POS, HEIGHT_POS, JPG_HEADER, LUMA_Q_POS, WIDTH_POS

RTSP_PORT = 554
RTP_PORT = 65362
USER_AGENT = "User-Agent: XXXX Company RTSP Client 0.0001"  # as sent by the app

LUMA_Q = bytes([16, 11, 12, 14, 12, 10, 16, 14, 13, 14, 18, 17, 16, 19, 24, 40, 26, 24, 22, 22, 24, 49,
                35, 37, 29, 40, 58, 51, 61, 60, 57, 51, 56, 55, 64, 72, 92, 78, 64, 68, 87, 69, 55, 56,
                80, 109, 81, 87, 95, 98, 103, 104, 103, 62, 77, 113, 121, 112, 100, 120, 92, 101, 103, 99])
CHROMA_Q = bytes([17, 18, 18, 24, 21, 24, 47, 26, 26, 47, 99, 66, 56, 66] + [99] * 50)


def make_tables(q: int) -> tuple[bytes, bytes]:
    """RFC 2435 Q -> quantisation tables (same arithmetic as the app)."""
    factor = min(max(q, 1), 99)
    scale = 5000 // factor if q < 50 else 200 - factor * 2
    lq = bytes(min(max((b * scale + 50) // 100, 1), 255) for b in LUMA_Q)
    cq = bytes(min(max((b * scale + 50) // 100, 1), 255) for b in CHROMA_Q)
    return lq, cq


class FrameAssembler:
    """Rebuilds JPEG frames from RTP fragments the same way the original app does.

    The app ignores the RTP timestamp entirely: a frame starts at the packet whose
    fragment offset is 0, continues with consecutive sequence numbers, and ends at
    the packet with the marker bit set. Each fragment is written at its offset.
    It also always uses a fixed 12-byte RTP header and never skips a restart
    header, so we do the same.
    """

    def __init__(self, on_frame: Callable[[bytes], None]):
        self.on_frame = on_frame
        self.buf: Optional[bytearray] = None   # header + scan data of frame in progress
        self.next_seq: Optional[int] = None
        self.packets = 0        # every UDP datagram
        self.non_jpeg = 0       # rejected (too short / not payload type 26)
        self.frames = 0
        self.dropped = 0
        self.samples: list[bytes] = []          # first few raw packets, for debugging

    def feed(self, pkt: bytes) -> None:
        self.packets += 1
        if len(self.samples) < 5:
            self.samples.append(pkt[:64])
        if len(pkt) < 16:
            self.non_jpeg += 1
            return
        b1, seq = pkt[1], struct.unpack_from(">H", pkt, 2)[0]
        if (b1 & 0x7F) != 26:          # payload type 26 = JPEG
            self.non_jpeg += 1
            return
        marker = bool(b1 & 0x80)
        pos = 12
        off = struct.unpack_from(">I", pkt, pos)[0] & 0xFFFFFF
        q, w8, h8 = pkt[pos + 5], pkt[pos + 6], pkt[pos + 7]
        pos += 8

        if off == 0:
            if self.buf is not None:
                self.dropped += 1
            if q >= 128:
                # in-band tables: MBZ, precision, length(2), then luma(64) + chroma(64)
                lq, cq = pkt[pos + 4:pos + 68], pkt[pos + 68:pos + 132]
                pos += 4 + 128
            else:
                lq, cq = make_tables(q)
            hdr = bytearray(JPG_HEADER)
            hdr[LUMA_Q_POS:LUMA_Q_POS + 64] = lq
            hdr[CHROMA_Q_POS:CHROMA_Q_POS + 64] = cq
            struct.pack_into(">H", hdr, HEIGHT_POS, h8 * 8)
            struct.pack_into(">H", hdr, WIDTH_POS, w8 * 8)
            self.buf = hdr
        elif self.buf is None:
            return                      # waiting for the start of a frame
        elif seq != self.next_seq:
            self.dropped += 1           # lost a packet: discard this frame
            self.buf, self.next_seq = None, None
            return

        data = pkt[pos:]
        at = len(JPG_HEADER) + off
        if at > len(self.buf):
            self.buf.extend(b"\x00" * (at - len(self.buf)))
        self.buf[at:at + len(data)] = data
        self.next_seq = (seq + 1) & 0xFFFF

        if marker:
            frame, self.buf, self.next_seq = self.buf, None, None
            if not frame.endswith(b"\xff\xd9"):
                frame += b"\xff\xd9"
            self.frames += 1
            self.on_frame(bytes(frame))


class LiveView:
    def __init__(self, host: str = "192.168.0.1", rtsp_port: int = RTSP_PORT, rtp_port: int = RTP_PORT,
                 log: Optional[Callable[[str], None]] = None, newline: str = "\n"):
        self.host, self.rtsp_port, self.rtp_port = host, rtsp_port, rtp_port
        self.log = log or (lambda s: None)
        self.newline = newline          # the app (Android) used "\n"
        self.rtsp: Optional[socket.socket] = None
        self.udp: Optional[socket.socket] = None
        self.cseq = 0
        self.responses: list[tuple[str, str]] = []
        self.session = random.randint(0, 0x7FFFFFFF)
        self.latest: Optional[bytes] = None
        self.frame_event = threading.Condition()
        self.frame_no = 0
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self.assembler = FrameAssembler(self._on_frame)
        self.callbacks: list[Callable[[bytes], None]] = []

    def _request(self, method: str, extra: list[str]) -> str:
        nl = self.newline
        msg = f"{method} rtsp://{self.host}/MJPG RTSP/1.0{nl}CSeq: {self.cseq}{nl}{USER_AGENT}{nl}"
        msg += "".join(h + nl for h in extra) + nl
        self.rtsp.sendall(msg.encode("ascii"))
        resp = self.rtsp.recv(4096).decode("utf-8", errors="replace")
        self.log(f"RTSP {method} -> {resp.splitlines()[0] if resp else '(no reply)'}")
        self.responses.append((method, resp))
        if "RTSP/1.0 200 OK" in resp:
            self.cseq += 1
        return resp

    def start(self) -> None:
        # Bind UDP first so no early packets are lost.
        # The app always uses 65362; if it is taken, fall back to another port and
        # tell the camera through RTSP SETUP's client_port.
        last = None
        for port in [self.rtp_port] + list(range(65364, 65401, 2)):
            u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                u.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
            except OSError:
                pass
            try:
                u.bind(("0.0.0.0", port))
            except OSError as e:
                u.close()
                last = e
                continue
            if port != self.rtp_port:
                self.log(f"UDP {self.rtp_port} is busy, using {port}")
            self.rtp_port = port
            self.udp = u
            break
        else:
            raise OSError(f"no free UDP port for live view: {last}")
        self.udp.settimeout(0.5)

        self.rtsp = socket.create_connection((self.host, self.rtsp_port), timeout=5)
        self.cseq = 0
        self._request("OPTIONS", [])
        self._request("DESCRIBE", ["Accept: Applecation/sdp"])  # typo is the app's
        self._request("SETUP", [f"Transport: RTP/AVP;unicast;client_port={self.rtp_port}-{self.rtp_port + 1}"])
        self._request("PLAY", [f"Session: {self.session}", "Range: npt=0.000-"])

        self._running = True
        self._thread = threading.Thread(target=self._rx_loop, name="cubic-rtp", daemon=True)
        self._thread.start()

    def stats(self) -> dict:
        a = self.assembler
        return {"packets": a.packets, "non_jpeg": a.non_jpeg, "frames": a.frames, "dropped": a.dropped}

    def _rx_loop(self) -> None:
        while self._running:
            try:
                pkt, src = self.udp.recvfrom(65536)
                if self.assembler.packets == 0:
                    self.log(f"first RTP packet from {src[0]}:{src[1]} ({len(pkt)} bytes): {pkt[:32].hex()}")
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                self.assembler.feed(pkt)
            except Exception as e:
                self.log(f"bad RTP packet: {e}")

    def _on_frame(self, jpeg: bytes) -> None:
        with self.frame_event:
            self.latest = jpeg
            self.frame_no += 1
            self.frame_event.notify_all()
        for cb in list(self.callbacks):
            cb(jpeg)

    def wait_frame(self, after: int = 0, timeout: float = 5.0) -> tuple[int, Optional[bytes]]:
        deadline = time.time() + timeout
        with self.frame_event:
            while self.frame_no <= after:
                left = deadline - time.time()
                if left <= 0:
                    break
                self.frame_event.wait(left)
            return self.frame_no, self.latest

    def stop(self) -> None:
        self._running = False
        for s in (self.rtsp, self.udp):
            try:
                if s:
                    s.close()
            except OSError:
                pass
        self.rtsp = self.udp = None
