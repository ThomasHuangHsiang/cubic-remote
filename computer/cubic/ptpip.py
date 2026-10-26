"""PTP/IP client for the altek Cubic (model C01), reverse-engineered from
Cubic Camera app 1.3.29 (com.altek.camera_control_app).

The wire format deliberately mirrors what the original app sends, including
its quirks (non-standard friendly name, DataPhase always 0, INT32 for every
property write, strings without a NUL terminator). The camera firmware was
only ever tested against that app, so we copy it rather than "fix" it.
"""

from __future__ import annotations

import socket
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

CAMERA_IP = "192.168.0.1"
PTPIP_PORT = 15740
PROTOCOL_VERSION = 0x00013000  # 77824 in the app

# GUID used by the app: b"cubic" + 11 x 0xFC
APP_GUID = b"cubic" + b"\xfc" * 11

# ---- PTP/IP packet types ----
PKT_INIT_CMD_REQ = 1
PKT_INIT_CMD_ACK = 2
PKT_INIT_EVENT_REQ = 3
PKT_INIT_EVENT_ACK = 4
PKT_INIT_FAIL = 5
PKT_OPERATION_REQ = 6
PKT_OPERATION_RESP = 7
PKT_EVENT = 8
PKT_START_DATA = 9
PKT_DATA = 10
PKT_CANCEL = 11
PKT_END_DATA = 12
PKT_PROBE_REQ = 13
PKT_PROBE_RESP = 14


class Op:
    """Operation codes (standard PTP + altek vendor extensions)."""
    GET_DEVICE_INFO = 0x1001
    OPEN_SESSION = 0x1002
    CLOSE_SESSION = 0x1003
    GET_STORAGE_IDS = 0x1004
    GET_STORAGE_INFO = 0x1005
    GET_NUM_OBJECTS = 0x1006
    GET_OBJECT_HANDLES = 0x1007
    GET_OBJECT_INFO = 0x1008
    GET_OBJECT = 0x1009
    GET_THUMB = 0x100A
    DELETE_OBJECT = 0x100B
    INITIATE_CAPTURE = 0x100E
    FORMAT_STORE = 0x100F
    GET_DEVICE_PROP_DESC = 0x1014
    GET_DEVICE_PROP_VALUE = 0x1015
    SET_DEVICE_PROP_VALUE = 0x1016
    GET_PARTIAL_OBJECT = 0x101B
    # altek vendor ops
    GET_RTSP_INFO = 0x90A0
    PLAY_RTSP = 0x90A1
    STOP_RTSP = 0x90A2
    S1_LOCK = 0x90B1          # half-press (AF); param: speaker on(1)/off
    S1_RELEASE = 0x90B2
    GET_QV_IMAGE = 0x90B3     # quick-view JPEG of last shot
    TOUCH_SHOT = 0x90B5       # params: x, y (live-view coords)
    ZOOM = 0x90B7             # params: direction (0=wide,1=tele), press(1)/release(0)
    VIDEO_REC_KEY = 0x90C1    # toggles recording
    GET_SELECT_OBJHANDLE = 0x90D1
    CANCEL_OPERATION = 0x90D2
    GET_STORAGE_FREE = 0x90D3  # param: 1=photos remaining, 2=video seconds remaining
    GET_CID_INFO = 0x90D5
    RESET_AUTO_POWER_OFF = 0x90D6  # param: seconds
    GET_ALTEK_ID = 0x90D7
    GET_DSC_INFO = 0x90D8     # resp param0 = firmware version number
    SET_MACRO = 0x90D9        # param: 0/1
    GET_AUTO_POWER_OFF = 0x90DA
    AGREE_AUTO_POWER_OFF = 0x90DB
    SELF_TIMER_SHOT = 0x90DC  # param: countdown seconds (0 = off)


class Evt:
    OBJECT_ADDED = 0x4002
    DEVICE_PROP_CHANGED = 0x4006
    CAPTURE_COMPLETE = 0x400D
    QV_COMPLETE = 0xC0A1
    S2 = 0xC0A3
    SELF_TIMER_SHOT = 0xC0A4
    DSC_POWER_OFF = 0xC0A2
    AC_IN = 0xC0B1
    AUTO_POWER_OFF = 0xC0B2
    CAMERA_ERROR = 0xC0B3


class Prop:
    BATTERY_LEVEL = 0x5001      # UINT8 0..100
    FUNCTIONAL_MODE = 0x5002    # app sets 0x8002 (= control mode) on connect
    STILL_RESOLUTION = 0x5003
    FOCAL_LENGTH = 0x5008
    FLASH_MODE = 0x500C
    EXPOSURE_COMP = 0x5010      # 2000, 1000, 0, -1000, -2000
    DATETIME = 0x5011           # string "YYYYMMDDThhmmss"
    SCENE_MODE = 0x5031         # enum[0] = photo, enum[1] = video
    ISO = 0x5032
    EXPOSURE_TIME = 0x5033
    SOFT_SKIN = 0x5034
    SAVE_HIGH_QUALITY = 0x5035
    VIDEO_SIZE = 0x5071
    SOUND_VOLUME = 0x5081
    TOUCH_SHOT = 0x5091


FUNCTIONAL_MODE_CONTROL = 0x8002
FUNCTIONAL_MODE_BROWSE = 0x8001

RESP_NAMES = {
    0x2001: "OK", 0x2002: "GeneralError", 0x2003: "SessionNotOpen",
    0x2004: "InvalidTransactionID", 0x2005: "OperationNotSupported",
    0x2006: "ParameterNotSupported", 0x2007: "IncompleteTransfer",
    0x2008: "InvalidStorageID", 0x2009: "InvalidObjectHandle",
    0x200A: "DevicePropNotSupported", 0x200C: "StoreFull",
    0x2013: "StoreNotAvailable", 0x2019: "DeviceBusy",
    0x201C: "InvalidDevicePropValue", 0x201D: "InvalidParameter",
    0x201E: "SessionAlreadyOpen",
}

PROP_NAMES = {v: k for k, v in vars(Prop).items() if k.isupper()}
OP_NAMES = {v: k for k, v in vars(Op).items() if k.isupper()}
EVT_NAMES = {v: k for k, v in vars(Evt).items() if k.isupper()}

# PTP data types
DT_INT8, DT_UINT8, DT_INT16, DT_UINT16 = 1, 2, 3, 4
DT_INT32, DT_UINT32, DT_INT64, DT_UINT64 = 5, 6, 7, 8
DT_STR = 0xFFFF
_DT_FMT = {1: "<b", 2: "<B", 3: "<h", 4: "<H", 5: "<i", 6: "<I", 7: "<q", 8: "<Q"}


class PTPError(Exception):
    pass


class PTPResponseError(PTPError):
    def __init__(self, op: int, code: int):
        self.op, self.code = op, code
        super().__init__(f"{OP_NAMES.get(op, hex(op))} -> {RESP_NAMES.get(code, hex(code))} (0x{code:04X})")


@dataclass
class Response:
    code: int
    params: list[int]
    data: bytes = b""

    @property
    def ok(self) -> bool:
        return self.code == 0x2001


@dataclass
class Event:
    code: int
    tid: int
    params: list[int]

    def __str__(self) -> str:
        return f"{EVT_NAMES.get(self.code, hex(self.code))} params={[hex(p) for p in self.params]}"


@dataclass
class PropDesc:
    code: int
    datatype: int
    getset: int
    default: object
    current: object
    form: int                      # 0 none, 1 range, 2 enum
    enum: list = field(default_factory=list)
    range: tuple = ()

    def __str__(self) -> str:
        name = PROP_NAMES.get(self.code, hex(self.code))
        extra = f" enum={self.enum}" if self.form == 2 else (f" range={self.range}" if self.form == 1 else "")
        return f"{name}: current={self.current} default={self.default} type={self.datatype}{extra}"


# ---------------------------------------------------------------- helpers

def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise PTPError("connection closed by camera")
        buf += chunk
    return bytes(buf)


def read_packet(sock: socket.socket) -> tuple[int, bytes]:
    """Read one PTP/IP packet. Returns (type, payload-after-type)."""
    length = struct.unpack("<I", _recv_exact(sock, 4))[0]
    if length < 8 or length > 64 * 1024 * 1024:
        raise PTPError(f"bad packet length {length}")
    rest = _recv_exact(sock, length - 4)
    ptype = struct.unpack("<I", rest[:4])[0]
    return ptype, rest[4:]


def ptp_string(buf: bytes, pos: int) -> tuple[str, int]:
    n = buf[pos]
    pos += 1
    s = buf[pos:pos + 2 * n].decode("utf-16-le", errors="replace").rstrip("\x00")
    return s, pos + 2 * n


def _read_value(dt: int, buf: bytes, pos: int):
    if dt == DT_STR:
        return ptp_string(buf, pos)
    fmt = _DT_FMT.get(dt)
    if fmt is None:
        raise PTPError(f"unsupported datatype {dt}")
    size = struct.calcsize(fmt)
    return struct.unpack_from(fmt, buf, pos)[0], pos + size


def parse_prop_desc(data: bytes) -> PropDesc:
    code, dt = struct.unpack_from("<HH", data, 0)
    getset = data[4]
    pos = 5
    default, pos = _read_value(dt, data, pos)
    current, pos = _read_value(dt, data, pos)
    form = data[pos]
    pos += 1
    desc = PropDesc(code, dt, getset, default, current, form)
    if form == 1:
        mn, pos = _read_value(dt, data, pos)
        mx, pos = _read_value(dt, data, pos)
        st, pos = _read_value(dt, data, pos)
        desc.range = (mn, mx, st)
    elif form == 2:
        n = struct.unpack_from("<H", data, pos)[0]
        pos += 2
        for _ in range(n):
            v, pos = _read_value(dt, data, pos)
            desc.enum.append(v)
    return desc


def parse_device_info(data: bytes) -> dict:
    pos = 0
    std_ver, vext_id, vext_ver = struct.unpack_from("<HIH", data, pos)
    pos += 8
    vext_desc, pos = ptp_string(data, pos)
    func_mode = struct.unpack_from("<H", data, pos)[0]
    pos += 2

    def arr16():
        nonlocal pos
        n = struct.unpack_from("<I", data, pos)[0]
        pos += 4
        vals = list(struct.unpack_from(f"<{n}H", data, pos))
        pos += 2 * n
        return vals

    ops, events, props, cap_fmts, img_fmts = arr16(), arr16(), arr16(), arr16(), arr16()
    manuf, pos = ptp_string(data, pos)
    model, pos = ptp_string(data, pos)
    version, pos = ptp_string(data, pos)
    serial, pos = ptp_string(data, pos)
    return dict(standard_version=std_ver, vendor_ext_id=vext_id, vendor_ext_version=vext_ver,
                vendor_ext_desc=vext_desc, functional_mode=func_mode, operations=ops,
                events=events, properties=props, capture_formats=cap_fmts,
                image_formats=img_fmts, manufacturer=manuf, model=model,
                version=version, serial=serial)


def parse_object_info(data: bytes) -> dict:
    (storage, fmt, prot, size, thumb_fmt, thumb_size, thumb_w, thumb_h,
     img_w, img_h, depth, parent, assoc_type, assoc_desc, seq) = struct.unpack_from("<IHHIHIIIIIIIHII", data, 0)
    pos = 52
    filename, pos = ptp_string(data, pos)
    capture_date, pos = ptp_string(data, pos)
    mod_date, pos = ptp_string(data, pos)
    return dict(storage=storage, format=fmt, size=size, thumb_format=thumb_fmt,
                thumb_size=thumb_size, width=img_w, height=img_h, filename=filename,
                capture_date=capture_date, modification_date=mod_date)


# ---------------------------------------------------------------- client

class CubicCamera:
    """Talks to the camera exactly the way Cubic Camera 1.3.29 does."""

    def __init__(self, host: str = CAMERA_IP, port: int = PTPIP_PORT,
                 friendly_name: str = "02:00:00:00:00:00", timeout: float = 10.0,
                 log: Optional[Callable[[str], None]] = None):
        self.host, self.port = host, port
        # The app sends the phone's Wi-Fi MAC string as raw ASCII here.
        self.friendly_name = friendly_name
        self.timeout = timeout
        self.log = log or (lambda s: None)
        self.cmd: Optional[socket.socket] = None
        self.evt: Optional[socket.socket] = None
        self.tid = 0
        self.lock = threading.RLock()
        self.firmware = 0
        self.storage_ids: list[int] = []
        self.descs: dict[int, PropDesc] = {}
        self.event_handlers: list[Callable[[Event], None]] = []
        self._running = False
        self._threads: list[threading.Thread] = []
        self._first_capture = True

    # ---------- connection ----------
    def connect(self, start_keepalive: bool = True) -> None:
        self.log(f"connecting to {self.host}:{self.port}")
        last = None
        for _ in range(3):
            try:
                self.cmd = socket.create_connection((self.host, self.port), timeout=self.timeout)
                break
            except OSError as e:
                last = e
                time.sleep(0.5)
        else:
            raise PTPError(f"cannot connect to camera: {last}")
        self.cmd.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        # InitCommandRequest: guid(16) + friendly name (raw ASCII, as the app does) + version
        body = APP_GUID + self.friendly_name.encode("ascii") + struct.pack("<I", PROTOCOL_VERSION)
        self.cmd.sendall(struct.pack("<II", 8 + len(body), PKT_INIT_CMD_REQ) + body)
        ptype, payload = read_packet(self.cmd)
        if ptype != PKT_INIT_CMD_ACK:
            raise PTPError(f"InitCommand rejected (packet type {ptype})")
        conn_no = struct.unpack_from("<I", payload, 0)[0]
        cam_guid = payload[4:20]
        cam_name = payload[20:-4]
        self.log(f"InitCommandAck: conn={conn_no} guid={cam_guid.hex()} name={cam_name!r}")

        # Event channel. The app hard-codes connection number 1 here.
        self.evt = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.evt.sendall(struct.pack("<III", 12, PKT_INIT_EVENT_REQ, 1))
        ptype, _ = read_packet(self.evt)
        if ptype != PKT_INIT_EVENT_ACK:
            raise PTPError(f"InitEvent rejected (packet type {ptype})")
        self.evt.settimeout(1.0)
        self.log("event channel ready")

        self.tid = 0
        self._running = True
        t = threading.Thread(target=self._event_loop, name="cubic-events", daemon=True)
        t.start()
        self._threads.append(t)

        self.transaction(Op.OPEN_SESSION, [1])
        self.log("session open")
        if start_keepalive:
            t = threading.Thread(target=self._keepalive_loop, name="cubic-keepalive", daemon=True)
            t.start()
            self._threads.append(t)

    def setup(self) -> dict:
        """Replicates the app's initialisation sequence after connecting."""
        self.set_prop(Prop.FUNCTIONAL_MODE, FUNCTIONAL_MODE_CONTROL)
        self.firmware = self.transaction(Op.GET_DSC_INFO).params[0]
        self.log(f"firmware number {self.firmware}")
        self.storage_ids = self.get_storage_ids()
        battery = self.get_prop(Prop.BATTERY_LEVEL, DT_UINT8)
        for p in (Prop.FLASH_MODE, Prop.VIDEO_SIZE, Prop.TOUCH_SHOT, Prop.SCENE_MODE):
            try:
                self.get_prop_desc(p)
            except PTPError as e:
                self.log(f"prop desc {PROP_NAMES.get(p)} failed: {e}")
        info = dict(firmware=self.firmware, storage_ids=self.storage_ids, battery=battery,
                    has_sd=self.has_sd_card, photos_left=self.photos_remaining())
        if self.firmware > 8000:
            info["auto_power_off"] = self.transaction(Op.GET_AUTO_POWER_OFF).params[0]
            self.transaction(Op.SET_MACRO, [0])
        return info

    def close(self) -> None:
        self._running = False
        try:
            with self.lock:
                if self.cmd:
                    self.transaction(Op.CLOSE_SESSION)
        except Exception:
            pass
        for s in (self.evt, self.cmd):
            try:
                if s:
                    s.close()
            except OSError:
                pass
        self.cmd = self.evt = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    # ---------- background loops ----------
    def _event_loop(self) -> None:
        while self._running and self.evt:
            try:
                ptype, payload = read_packet(self.evt)
            except socket.timeout:
                continue
            except (OSError, PTPError) as e:
                if self._running:
                    self.log(f"event channel closed: {e}")
                break
            if ptype != PKT_EVENT or len(payload) < 6:
                continue
            code, tid = struct.unpack_from("<HI", payload, 0)
            n = (len(payload) - 6) // 4
            params = list(struct.unpack_from(f"<{n}I", payload, 6))
            ev = Event(code, tid, params)
            self.log(f"event: {ev}")
            for h in list(self.event_handlers):
                try:
                    h(ev)
                except Exception as e:  # never kill the loop
                    self.log(f"event handler error: {e}")

    def _keepalive_loop(self) -> None:
        # The app sends a PTP/IP Probe on the command socket once per second.
        while self._running:
            time.sleep(1.0)
            if not self._running:
                break
            if self.lock.acquire(timeout=0.05):
                try:
                    self.probe()
                except Exception as e:
                    self.log(f"probe failed: {e}")
                finally:
                    self.lock.release()

    def probe(self) -> bool:
        with self.lock:
            self.cmd.sendall(struct.pack("<II", 8, PKT_PROBE_REQ))
            ptype, _ = read_packet(self.cmd)
            return ptype == PKT_PROBE_RESP

    # ---------- core transaction ----------
    def transaction(self, op: int, params: list[int] | None = None, data_out: bytes | None = None,
                    check: bool = True, on_data: Optional[Callable[[bytes], None]] = None) -> Response:
        params = params or []
        with self.lock:
            tid = self.tid
            self.tid += 1
            # The app always sends DataPhaseInfo = 0, even when a data phase follows.
            pkt = struct.pack("<IIIHI", 0, PKT_OPERATION_REQ, 0, op, tid)
            pkt += b"".join(struct.pack("<I", p & 0xFFFFFFFF) for p in params)
            pkt = struct.pack("<I", len(pkt)) + pkt[4:]
            self.cmd.sendall(pkt)

            if data_out is not None:
                self.cmd.sendall(struct.pack("<IIIQ", 20, PKT_START_DATA, tid, len(data_out)))
                self.cmd.sendall(struct.pack("<III", 12 + len(data_out), PKT_END_DATA, tid) + data_out)

            data = bytearray()
            while True:
                ptype, payload = read_packet(self.cmd)
                if ptype == PKT_START_DATA:
                    continue
                if ptype in (PKT_DATA, PKT_END_DATA):
                    chunk = payload[4:]  # skip transaction id
                    if on_data:
                        on_data(chunk)
                    else:
                        data += chunk
                    continue
                if ptype == PKT_OPERATION_RESP:
                    code = struct.unpack_from("<H", payload, 0)[0]
                    n = (len(payload) - 6) // 4
                    rparams = list(struct.unpack_from(f"<{n}I", payload, 6))
                    resp = Response(code, rparams, bytes(data))
                    break
                if ptype == PKT_PROBE_RESP:
                    continue
                raise PTPError(f"unexpected packet type {ptype} during {OP_NAMES.get(op, hex(op))}")
        self.log(f"{OP_NAMES.get(op, hex(op))}{params} -> {RESP_NAMES.get(resp.code, hex(resp.code))} {resp.params} ({len(resp.data)} bytes)")
        if check and not resp.ok:
            raise PTPResponseError(op, resp.code)
        return resp

    # ---------- properties ----------
    def get_prop_desc(self, prop: int) -> PropDesc:
        d = parse_prop_desc(self.transaction(Op.GET_DEVICE_PROP_DESC, [prop]).data)
        self.descs[prop] = d
        return d

    def get_prop(self, prop: int, datatype: int | None = None):
        data = self.transaction(Op.GET_DEVICE_PROP_VALUE, [prop]).data
        if datatype is None:
            datatype = self.descs[prop].datatype if prop in self.descs else {1: DT_UINT8, 2: DT_UINT16}.get(len(data), DT_INT32)
        return _read_value(datatype, data, 0)[0]

    def set_prop(self, prop: int, value: int) -> Response:
        # The app writes every numeric property as a 4-byte INT32.
        return self.transaction(Op.SET_DEVICE_PROP_VALUE, [prop], data_out=struct.pack("<i", value))

    def set_prop_string(self, prop: int, text: str) -> Response:
        # App format: [num chars][UTF-16LE chars], no NUL terminator.
        data = bytes([len(text)]) + text.encode("utf-16-le")
        return self.transaction(Op.SET_DEVICE_PROP_VALUE, [prop], data_out=data)

    def sync_clock(self) -> None:
        self.set_prop_string(Prop.DATETIME, time.strftime("%Y%m%dT%H%M%S"))

    def enum_value(self, prop: int, index: int) -> int:
        """The app picks property values by index into the camera's enum list."""
        if prop not in self.descs:
            self.get_prop_desc(prop)
        return self.descs[prop].enum[index]

    # ---------- storage / status ----------
    def get_storage_ids(self) -> list[int]:
        data = self.transaction(Op.GET_STORAGE_IDS).data
        n = struct.unpack_from("<I", data, 0)[0]
        return list(struct.unpack_from(f"<{n}I", data, 4))

    @property
    def has_sd_card(self) -> bool:
        return 0x00010001 in self.storage_ids

    @property
    def storage(self) -> int:
        return self.storage_ids[0] if self.storage_ids else 0x00010001

    def battery(self) -> int:
        return self.get_prop(Prop.BATTERY_LEVEL, DT_UINT8)

    def photos_remaining(self) -> int:
        return self.transaction(Op.GET_STORAGE_FREE, [1]).params[0]

    def video_seconds_remaining(self) -> int:
        return self.transaction(Op.GET_STORAGE_FREE, [2]).params[0]

    def device_info(self) -> dict:
        return parse_device_info(self.transaction(Op.GET_DEVICE_INFO).data)

    # ---------- shooting ----------
    def half_press(self, beep: bool = True) -> dict:
        r = self.transaction(Op.S1_LOCK, [1 if beep else 0])
        result = {"raw": r.data.hex()}
        if len(r.data) >= 4:
            n = struct.unpack_from("<I", r.data, 0)[0]
            areas = []
            pos = 4
            for _ in range(n + 1):
                if pos + 10 > len(r.data):
                    break
                areas.append(struct.unpack_from("<hhhhh", r.data, pos))
                pos += 10
            result["areas"] = areas
            result["focused"] = len(areas) > 1 and areas[1][0] == 1
        return result

    def release(self) -> None:
        self.transaction(Op.S1_RELEASE)

    def capture(self, beep: bool = True) -> int:
        """Shutter: half-press (AF), capture, release — like the app's shutter button.

        Like the app, the InitiateCapture response is not treated as an error:
        when stopping a recording the camera answers DeviceBusy while it is
        already stopping and saving the file. S1 release is always sent.
        Returns the InitiateCapture response code.
        """
        if self._first_capture:
            self._first_capture = False
            try:
                self.sync_clock()
            except PTPError as e:
                self.log(f"clock sync failed: {e}")
        self.half_press(beep)
        try:
            r = self.transaction(Op.INITIATE_CAPTURE, [2 if self.firmware > 10000 else 4], check=False)
        finally:
            self.transaction(Op.S1_RELEASE, check=False)
        return r.code

    def touch_shot(self, x: int, y: int) -> None:
        self.transaction(Op.TOUCH_SHOT, [x, y])

    def zoom(self, tele: bool, press: bool) -> None:
        self.transaction(Op.ZOOM, [1 if tele else 0, 1 if press else 0])

    def zoom_step(self, tele: bool, seconds: float = 0.3) -> None:
        self.zoom(tele, True)
        time.sleep(seconds)
        self.zoom(tele, False)

    def video_rec_key(self) -> None:
        # NOTE: the original app only wires this to a hidden button; on a real
        # camera it does not produce a video file. Use record_toggle() instead.
        self.transaction(Op.VIDEO_REC_KEY)

    def record_toggle(self, beep: bool = True) -> int:
        """Start/stop recording. In video mode the app uses the normal shutter
        sequence (S1 lock -> InitiateCapture -> S1 release) for both."""
        return self.capture(beep)

    def set_mode(self, video: bool) -> None:
        self.set_prop(Prop.SCENE_MODE, self.enum_value(Prop.SCENE_MODE, 1 if video else 0))

    def get_quickview(self) -> bytes:
        return self.transaction(Op.GET_QV_IMAGE).data

    def set_self_timer(self, seconds: int) -> None:
        self.transaction(Op.SELF_TIMER_SHOT, [seconds])

    def set_macro(self, on: bool) -> None:
        self.transaction(Op.SET_MACRO, [1 if on else 0])

    def set_auto_power_off(self, seconds: int) -> None:
        self.transaction(Op.RESET_AUTO_POWER_OFF, [seconds])

    # ---------- files ----------
    def list_handles(self) -> list[int]:
        data = self.transaction(Op.GET_OBJECT_HANDLES, [self.storage, 0]).data
        n = struct.unpack_from("<I", data, 0)[0]
        return list(struct.unpack_from(f"<{n}I", data, 4))

    def object_info(self, handle: int) -> dict:
        return parse_object_info(self.transaction(Op.GET_OBJECT_INFO, [handle]).data)

    def thumbnail(self, handle: int) -> bytes:
        return self.transaction(Op.GET_THUMB, [handle]).data

    def download(self, handle: int, out, chunk: int = 1843200,
                 progress: Optional[Callable[[int, int], None]] = None) -> int:
        """Download with GetPartialObject in chunks (1843200 bytes, as the app does)."""
        size = self.object_info(handle)["size"]
        offset = 0
        while offset < size:
            want = min(chunk, size - offset)
            got = 0

            def sink(b: bytes):
                nonlocal got
                out.write(b)
                got += len(b)

            self.transaction(Op.GET_PARTIAL_OBJECT, [handle, offset, want], on_data=sink)
            if got == 0:
                break
            offset += got
            if progress:
                progress(offset, size)
        return offset

    def delete(self, handle: int) -> None:
        self.transaction(Op.DELETE_OBJECT, [handle])

    def format_card(self) -> None:
        self.transaction(Op.FORMAT_STORE, [self.storage, 0])
