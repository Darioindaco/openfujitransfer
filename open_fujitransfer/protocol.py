"""
Het Fuji WiFi-protocol (camera als accesspoint, "Draadloze communicatie").

De camera zit op 192.168.0.1 en praat op TCP-poort 55740 een Fuji-variant van
PTP/IP: een eigen init-pakket, daarna gewone PTP "USB-containers" over TCP.

Reverse-engineered door anderen; deze code volgt de flow van libfuji
(https://github.com/petabyt/libfuji, MIT) en camlib
(https://github.com/petabyt/camlib, Apache-2.0). Dank aan Daniel Cook.
"""

from __future__ import annotations

import os
import socket
import struct
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Constanten
# ---------------------------------------------------------------------------

CAMERA_IP = "192.168.0.1"
CMD_PORT = 55740       # commando's
EVENT_PORT = 55741     # events (alleen geopend omdat de camera dat verwacht)
VIDEO_PORT = 55742     # liveview (idem, we gooien de data weg)

FUJI_PROTOCOL_VERSION = 0x8F53E4F2
FUJI_REMOTE_VERSION = 0x2000C        # wat de officiële app instelt
MAX_CHUNK = 0x100000                 # 1 MB per GetPartialObject, zoals libfuji

# PTP-containertypes
CT_COMMAND, CT_DATA, CT_RESPONSE = 1, 2, 3

# Standaard PTP-operaties
OC_OPEN_SESSION = 0x1002
OC_CLOSE_SESSION = 0x1003
OC_GET_OBJECT_INFO = 0x1008
OC_GET_DEVICE_PROP_VALUE = 0x1015
OC_SET_DEVICE_PROP_VALUE = 0x1016
OC_TERMINATE_OPEN_CAPTURE = 0x1018
OC_GET_PARTIAL_OBJECT = 0x101B
OC_INITIATE_OPEN_CAPTURE = 0x101C

RC_OK = 0x2001
RC_SESSION_ALREADY_OPEN = 0x201E

# Fuji-eigenschappen (device properties)
P_EVENTS = 0xD212
P_OBJECT_COUNT = 0xD222
P_ENABLE_CORRECT_FILE_SIZE = 0xD227
P_STORAGE_ID = 0xD244
P_CAMERA_STATE = 0xDF00
P_CLIENT_STATE = 0xDF01
P_IMAGE_GET_VERSION = 0xDF21
P_GET_OBJECT_VERSION = 0xDF22
P_REMOTE_VERSION = 0xDF24
P_REMOTE_GET_OBJECT_VERSION = 0xDF25

# Waarden van P_CAMERA_STATE
CAM_WAIT_FOR_ACCESS = 0
CAM_MULTIPLE_TRANSFER = 1
CAM_FULL_ACCESS = 2
CAM_PC_AUTO_SAVE = 3
CAM_REMOTE_ACCESS = 6

# Waarden van P_CLIENT_STATE
CLIENT_VIEW_MULTIPLE = 1
CLIENT_VIEW_ALL_IMGS = 2
CLIENT_OLD_REMOTE = 3
CLIENT_REMOTE_MODE = 5
CLIENT_REMOTE_IMG_VIEW = 11

GOODBYE_PACKET = b"\x08\x00\x00\x00\xff\xff\xff\xff"

logger = logging.getLogger("open_fujitransfer")


def log(msg: str) -> None:
    logger.debug(msg)


class FujiError(Exception):
    pass


class Cancelled(Exception):
    """De gebruiker heeft op Stop gedrukt."""


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass
class ObjectInfo:
    handle: int
    filename: str
    size: int
    capture_date: str   # "20260131T142233" of ""
    format_code: int

    @property
    def date_folder(self) -> str:
        """'2026-01-31' uit de opnamedatum, of 'onbekend'."""
        d = self.capture_date
        if len(d) >= 8 and d[:8].isdigit():
            return f"{d[0:4]}-{d[4:6]}-{d[6:8]}"
        return "onbekend"


def read_ptp_string(buf: bytes, offset: int) -> tuple[str, int]:
    """PTP-string: 1 byte aantal tekens (incl. nul), dan UTF-16LE."""
    if offset >= len(buf):
        return "", offset
    n = buf[offset]
    offset += 1
    raw = buf[offset:offset + n * 2]
    offset += n * 2
    return raw.decode("utf-16-le", errors="replace").rstrip("\x00"), offset


def parse_object_info(handle: int, data: bytes) -> ObjectInfo:
    """Standaard PTP ObjectInfo-dataset (de vaste velden zijn 52 bytes)."""
    if len(data) < 52:
        raise FujiError(f"ObjectInfo te kort ({len(data)} bytes)")
    format_code = struct.unpack_from("<H", data, 4)[0]
    size = struct.unpack_from("<I", data, 8)[0]
    offset = 52
    filename, offset = read_ptp_string(data, offset)
    capture_date, offset = read_ptp_string(data, offset)
    return ObjectInfo(handle, filename, size, capture_date, format_code)


# ---------------------------------------------------------------------------
# De camera
# ---------------------------------------------------------------------------

class FujiCamera:
    def __init__(self, host: str = CAMERA_IP, client_name: str = "Open FujiTransfer",
                 on_status=None):
        self.host = host
        # Wordt aangeroepen met korte statusberichten voor de gebruiker
        self.on_status = on_status or (lambda msg: None)
        self.client_name = client_name
        self.sock: socket.socket | None = None
        self.extra_socks: list[socket.socket] = []
        self.transaction_id = 1
        self.camera_name = "?"

        # Bijgewerkt door get_events()
        self.camera_state = -1
        self.num_objects = -1

        # Versie-eigenschappen, ingelezen in setup()
        self.remote_version = -1
        self.remote_image_view_version = -1

    # ---- laag 1: bytes over TCP ------------------------------------------

    def _recv_exact(self, n: int) -> bytes:
        assert self.sock is not None
        chunks = []
        while n > 0:
            chunk = self.sock.recv(min(n, 1 << 20))
            if not chunk:
                raise FujiError("Camera heeft de verbinding verbroken")
            chunks.append(chunk)
            n -= len(chunk)
        return b"".join(chunks)

    def _send_container(self, ctype: int, code: int, payload: bytes) -> None:
        assert self.sock is not None
        header = struct.pack("<IHHI", 12 + len(payload), ctype, code, self.transaction_id)
        log(f"-> type={ctype} code=0x{code:04X} tid={self.transaction_id} "
            f"payload={payload[:32].hex(' ')}")
        self.sock.sendall(header + payload)

    def _recv_container(self) -> tuple[int, int, int, bytes]:
        length, ctype, code, tid = struct.unpack("<IHHI", self._recv_exact(12))
        if length < 12:
            raise FujiError(f"Ongeldig pakket van camera (lengte {length})")
        payload = self._recv_exact(length - 12)
        log(f"<- type={ctype} code=0x{code:04X} tid={tid} len={len(payload)} "
            f"payload={payload[:32].hex(' ')}")
        return ctype, code, tid, payload

    # ---- laag 2: één PTP-transactie --------------------------------------

    def transaction(self, code: int, params: tuple[int, ...] = (),
                    data_out: bytes | None = None,
                    timeout: float = 15.0) -> tuple[int, bytes]:
        """Stuur een commando (+ evt. data), geef (responscode, ontvangen data)."""
        assert self.sock is not None
        self.sock.settimeout(timeout)

        self._send_container(CT_COMMAND, code, b"".join(struct.pack("<I", p) for p in params))
        if data_out is not None:
            self._send_container(CT_DATA, code, data_out)

        data_in = b""
        try:
            while True:
                ctype, rcode, _tid, payload = self._recv_container()
                if ctype == CT_DATA:
                    data_in += payload
                elif ctype == CT_RESPONSE:
                    break
                else:
                    raise FujiError(f"Onverwacht pakkettype {ctype}")
        except socket.timeout:
            raise FujiError(f"Camera antwoordt niet op 0x{code:04X}") from None

        self.transaction_id += 1
        return rcode, data_in

    # ---- laag 3: eigenschappen en events ---------------------------------

    def get_prop(self, prop: int) -> bytes | None:
        rc, data = self.transaction(OC_GET_DEVICE_PROP_VALUE, (prop,))
        if rc != RC_OK:
            log(f"GetDevicePropValue 0x{prop:04X} gaf 0x{rc:04X}")
            return None
        return data

    def get_prop_int(self, prop: int) -> int:
        """Waarde als getal, of -1 als de camera hem niet kent / leeg geeft."""
        data = self.get_prop(prop)
        if not data:
            return -1
        if len(data) == 1:
            return data[0]
        if len(data) == 2:
            return struct.unpack("<H", data)[0]
        return struct.unpack_from("<I", data)[0]

    def set_prop(self, prop: int, value: int, size: int, timeout: float = 15.0) -> None:
        fmt = "<H" if size == 2 else "<I"
        rc, _ = self.transaction(OC_SET_DEVICE_PROP_VALUE, (prop,),
                                 data_out=struct.pack(fmt, value), timeout=timeout)
        if rc != RC_OK:
            raise FujiError(f"Camera weigerde eigenschap 0x{prop:04X}={value} (0x{rc:04X})")

    def get_events(self) -> None:
        """Fuji pollt veranderingen via eigenschap D212: lijst van (code, waarde)."""
        data = self.get_prop(P_EVENTS)
        if not data or len(data) < 2:
            return
        count = struct.unpack_from("<H", data, 0)[0]
        for i in range(count):
            off = 2 + i * 6
            if off + 6 > len(data):
                break
            code, value = struct.unpack_from("<HI", data, off)
            log(f"event 0x{code:04X} = {value}")
            if code == P_CAMERA_STATE:
                self.camera_state = value
            elif code == P_OBJECT_COUNT:
                self.num_objects = value

    # ---- verbinden ---------------------------------------------------------

    def connect(self) -> None:
        self.on_status(f"Verbinden met camera op {self.host} ...")
        try:
            self.sock = socket.create_connection((self.host, CMD_PORT), timeout=5)
        except OSError as e:
            raise FujiError(
                f"Kan de camera niet bereiken ({e}).\n"
                "  - Staat de camera in 'Draadloze communicatie'?\n"
                "  - Is je Mac verbonden met het WiFi-netwerk FUJIFILM-X-T3-....?"
            ) from None
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._handshake()

    def _handshake(self) -> None:
        """Fuji-specifiek init-pakket (0x52 bytes) in plaats van PTP/IP Init."""
        assert self.sock is not None
        name = self.client_name.encode("utf-16-le")[:52].ljust(54, b"\x00")
        guid = struct.pack("<IIII", 0x5D48A5AD, 0x0B7FB287, 0xD0DED5D3, 0x0)
        packet = struct.pack("<III", 0x52, 1, FUJI_PROTOCOL_VERSION) + guid + name
        assert len(packet) == 0x52
        self.sock.settimeout(30)
        self.sock.sendall(packet)

        length = struct.unpack("<I", self._recv_exact(4))[0]
        rest = self._recv_exact(length - 4)
        ptype = struct.unpack_from("<I", rest, 0)[0]
        log(f"init-antwoord type={ptype} {rest.hex(' ')}")
        if ptype == 5:
            raise FujiError("Camera weigerde de verbinding (INIT_FAIL). "
                            "Herstart de draadloze modus op de camera en probeer opnieuw.")
        # Antwoord (na de lengte): [type 4][4 bytes][x1..x4 16][cameranaam UTF-16]
        raw_name = rest[24:]
        raw_name = raw_name[:len(raw_name) // 2 * 2]
        self.camera_name = raw_name.decode("utf-16-le", errors="ignore").split("\x00")[0] or "?"
        self.on_status(f"Verbonden met {self.camera_name}")

    def setup(self) -> None:
        """Volgt fuji_setup() uit libfuji voor 'Draadloze communicatie'."""
        time.sleep(0.05)  # Fuji wil minstens 50 ms rust voor OpenSession

        # Fuji begint, tegen de PTP-standaard in, met transactie-id 1.
        self.transaction_id = 1
        rc, _ = self.transaction(OC_OPEN_SESSION, (1,))
        if rc not in (RC_OK, RC_SESSION_ALREADY_OPEN):
            raise FujiError(f"OpenSession mislukt (0x{rc:04X})")

        self.camera_state = -1
        self.num_objects = -1
        self.get_events()
        if self.camera_state in (CAM_WAIT_FOR_ACCESS, -1):
            self.on_status("Druk op OK op de camera om toegang te geven ...")
            deadline = time.time() + 120
            while self.camera_state in (CAM_WAIT_FOR_ACCESS, -1):
                if time.time() > deadline:
                    raise FujiError("Geen toestemming gekregen op de camera (2 min gewacht)")
                time.sleep(0.1)
                self.get_events()

        self.get_prop_int(P_GET_OBJECT_VERSION)
        self.remote_image_view_version = self.get_prop_int(P_REMOTE_GET_OBJECT_VERSION)
        self.get_prop_int(P_IMAGE_GET_VERSION)
        self.remote_version = self.get_prop_int(P_REMOTE_VERSION)
        log(f"camera_state={self.camera_state} remote_version=0x{self.remote_version & 0xFFFFFFFF:X} "
            f"remote_image_view_version={self.remote_image_view_version}")

        # Modus kiezen. Op nieuwere camera's verschijnt hierna een ja/nee-dialoog,
        # dus we wachten ruim op antwoord.
        if self.remote_version != -1 or self.camera_state == CAM_REMOTE_ACCESS:
            mode = CLIENT_REMOTE_MODE
        elif self.camera_state == CAM_MULTIPLE_TRANSFER:
            mode = CLIENT_VIEW_MULTIPLE
        elif self.camera_state == CAM_PC_AUTO_SAVE:
            mode = CLIENT_OLD_REMOTE
        else:
            mode = CLIENT_VIEW_ALL_IMGS
        log(f"client state -> {mode}")
        self.set_prop(P_CLIENT_STATE, mode, 2, timeout=120)
        self.get_events()

        if self.camera_state == CAM_MULTIPLE_TRANSFER:
            raise FujiError(
                "De camera staat in 'stuur geselecteerde foto's'-modus. Kies op de camera "
                "liever 'Draadloze communicatie' zodat de app zelf alles kan ophalen.")

        # Versie-eigenschappen terugzetten, zoals de officiële app doet
        if self.remote_version == -1:
            v = self.get_prop_int(P_GET_OBJECT_VERSION)
            if v != -1:
                self.set_prop(P_GET_OBJECT_VERSION, v, 4)
        else:
            self.set_prop(P_REMOTE_VERSION, FUJI_REMOTE_VERSION, 4)

        self.get_prop(P_STORAGE_ID)

        if self.remote_version != -1 and self.camera_state == CAM_REMOTE_ACCESS:
            # Camera's met remote-modus (X-T2/X-T3/...) blijven anders hangen:
            # eerst liveview openen en meteen weer sluiten, dan pas de galerij.
            self._open_and_close_liveview()
            self._config_image_gallery()

        if self.num_objects < 0:
            self.get_events()

    def _open_and_close_liveview(self) -> None:
        log("liveview openen en sluiten (vereist door camera)")
        open_tid = self.transaction_id
        rc, _ = self.transaction(OC_INITIATE_OPEN_CAPTURE, (0, 0))
        if rc != RC_OK:
            raise FujiError(f"Remote-modus starten mislukt (0x{rc:04X})")
        self.get_events()
        time.sleep(0.05)

        for port in (EVENT_PORT, VIDEO_PORT):
            try:
                s = socket.create_connection((self.host, port), timeout=3)
            except OSError as e:
                log(f"poort {port} niet geopend: {e}")
                continue
            self.extra_socks.append(s)
            # Data die hier binnenkomt (liveview-frames) lezen we weg, anders loopt
            # de buffer van de camera vol.
            threading.Thread(target=_drain, args=(s,), daemon=True).start()

        self.get_events()
        rc, _ = self.transaction(OC_TERMINATE_OPEN_CAPTURE, (open_tid,))
        if rc != RC_OK:
            log(f"TerminateOpenCapture gaf 0x{rc:04X}")

    def _config_image_gallery(self) -> None:
        if self.remote_image_view_version == -1:
            return
        log("galerij-modus instellen")
        self.get_events()
        self.set_prop(P_CAMERA_STATE, CAM_REMOTE_ACCESS, 2)
        self.get_events()
        self.get_events()
        self.set_prop(P_CLIENT_STATE, CLIENT_REMOTE_IMG_VIEW, 2)
        self.get_events()
        self.remote_image_view_version = self.get_prop_int(P_REMOTE_GET_OBJECT_VERSION)
        self.set_prop(P_REMOTE_GET_OBJECT_VERSION, 5, 4)
        self.get_events()

    # ---- foto's ------------------------------------------------------------

    def object_handles(self) -> list[int]:
        """Over WiFi nummert Fuji de foto's gewoon 1..N."""
        if self.num_objects <= 0:
            return []
        return list(range(1, self.num_objects + 1))

    def get_object_info(self, handle: int) -> ObjectInfo:
        rc, data = self.transaction(OC_GET_OBJECT_INFO, (handle,))
        if rc != RC_OK:
            raise FujiError(f"Geen info voor foto {handle} (0x{rc:04X})")
        return parse_object_info(handle, data)

    def with_correct_sizes(self):
        """Contextmanager: zonder D227=1 meldt de camera altijd ~100 KB als grootte."""
        cam = self

        class _Ctx:
            def __enter__(self_inner):
                cam.get_events()
                cam.set_prop(P_ENABLE_CORRECT_FILE_SIZE, 1, 2, timeout=30)

            def __exit__(self_inner, exc_type, exc, tb):
                if exc_type is None:
                    cam.get_events()
                    cam.set_prop(P_ENABLE_CORRECT_FILE_SIZE, 0, 2)
                return False

        return _Ctx()

    def download(self, info: ObjectInfo, dest: Path, progress=None, cancel=None) -> None:
        """Download in blokken van 1 MB naar een .part-bestand, dan hernoemen."""
        tmp = dest.with_name(dest.name + ".part")
        received = 0
        with open(tmp, "wb") as f:
            while received < info.size:
                if cancel is not None and cancel.is_set():
                    f.close()
                    tmp.unlink(missing_ok=True)
                    raise Cancelled()
                want = min(MAX_CHUNK, info.size - received)
                rc, data = self.transaction(OC_GET_PARTIAL_OBJECT,
                                            (info.handle, received, want), timeout=60)
                if rc != RC_OK:
                    raise FujiError(f"Download van {info.filename} mislukt (0x{rc:04X})")
                if not data:
                    raise FujiError(f"Camera stuurde lege data voor {info.filename}")
                f.write(data)
                received += len(data)
                if progress:
                    progress(received, info.size)
        os.replace(tmp, dest)

    # ---- afsluiten ---------------------------------------------------------

    def close(self) -> None:
        if self.sock is None:
            return
        try:
            self.transaction(OC_CLOSE_SESSION, timeout=3)
        except (FujiError, OSError):
            pass
        try:
            self.sock.sendall(GOODBYE_PACKET)
        except OSError:
            pass
        for s in [self.sock, *self.extra_socks]:
            try:
                s.close()
            except OSError:
                pass
        self.sock = None


def _drain(s: socket.socket) -> None:
    s.settimeout(None)
    try:
        while s.recv(65536):
            pass
    except OSError:
        pass
