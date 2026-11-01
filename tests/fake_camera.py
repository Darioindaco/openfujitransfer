#!/usr/bin/env python3
"""
Nep-Fuji-camera voor testen zonder echte camera.

Speelt een X-T3-achtige camera na op 127.0.0.1 (poorten 55740-55742), met een
paar verzonnen JPEG's/RAF's. Test alleen of Open FujiTransfer het protocol zoals libfuji
het beschrijft correct spreekt — niet of een echte camera precies zo reageert.

    python3 tests/fake_camera.py &
    python3 -m open_fujitransfer --host 127.0.0.1 --out /tmp/fujitest
"""

import os
import random
import socket
import struct
import sys
import threading
import time

# Vertraging per blok in seconden, om de voortgang in de UI te kunnen zien
DELAY = float(os.environ.get("FAKE_DELAY", "0"))

HOST = "127.0.0.1"

_rng = random.Random(42)  # vaste inhoud, zodat tests de bytes kunnen vergelijken
FILES = [
    ("DSCF0001.JPG", "20261030T101500", _rng.randbytes(2_500_000)),
    ("DSCF0001.RAF", "20261030T101500", _rng.randbytes(3_100_000)),
    ("DSCF0002.JPG", "20261101T093000", _rng.randbytes(1_200_000)),
]
# FAKE_FILES=60 voegt extra (kleine) foto's toe, handig om het bladervenster te zien
for _n in range(int(os.environ.get("FAKE_FILES", "0"))):
    _day = 1 + _n // 12
    FILES.append((f"DSCF{_n + 100:04d}.JPG", f"202609{_day:02d}T{8 + _n % 12:02d}0000",
                  _rng.randbytes(50_000 + _n * 997)))


def make_thumb(i):
    """Een echte kleine JPEG als Pillow er is, anders nep-bytes (genoeg voor tests)."""
    try:
        from PIL import Image, ImageDraw
        import io
        hue = (i * 47) % 255
        img = Image.new("RGB", (160, 120), (hue, 120, 255 - hue))
        d = ImageDraw.Draw(img)
        d.ellipse([50, 30, 110, 90], fill=(240, 240, 230))
        d.text((6, 100), FILES[i][0], fill=(255, 255, 255))
        buf = io.BytesIO()
        img.save(buf, "JPEG")
        return buf.getvalue()
    except ImportError:
        return b"\xff\xd8" + bytes([i % 256]) * 400 + b"\xff\xd9"

state = {
    0xDF00: 0,         # camera_state: eerst wachten op OK
    0xDF01: 0,
    0xDF21: 3, 0xDF22: 4, 0xDF24: 0x20004, 0xDF25: 2,
    0xD227: 0,
    0xD244: 0x10000001,
}
polls = {"n": 0}


def ptp_string(s):
    if not s:
        return b"\x00"
    s += "\x00"
    return bytes([len(s)]) + s.encode("utf-16-le")


def object_info(i):
    name, date, data = FILES[i]
    size = len(data) if state[0xD227] == 1 else 100_000  # Fuji-gedrag zonder D227
    fixed = struct.pack("<IHHIHIIIIIIIHII", 0x10000001, 0x3801, 0, size, 0x3808, 0,
                        0, 0, 6240, 4160, 24, 0, 0, 0, 0)
    assert len(fixed) == 52
    return fixed + ptp_string(name) + ptp_string(date) + ptp_string(date) + b"\x00"


def recv_exact(c, n):
    b = b""
    while len(b) < n:
        chunk = c.recv(n - len(b))
        if not chunk:
            raise ConnectionError
        b += chunk
    return b


def send(c, ctype, code, tid, payload=b""):
    c.sendall(struct.pack("<IHHI", 12 + len(payload), ctype, code, tid) + payload)


def handle(c):
    # Handshake
    pkt = recv_exact(c, 0x52)
    assert struct.unpack_from("<III", pkt) == (0x52, 1, 0x8F53E4F2), pkt[:12].hex()
    name = "X-T3".encode("utf-16-le") + b"\x00" * 50
    resp = struct.pack("<II", 2, 0) + b"\x00" * 16 + name
    c.sendall(struct.pack("<I", 4 + len(resp)) + resp)

    session_open = False
    last_info = None  # nieuwere Fuji's: GetThumb alleen direct na GetObjectInfo
    while True:
        try:
            hdr = recv_exact(c, 4)
        except ConnectionError:
            return
        length = struct.unpack("<I", hdr)[0]
        if length == 8:
            recv_exact(c, 4)  # goodbye
            print("[fake] goodbye ontvangen")
            return
        rest = recv_exact(c, length - 4)
        ctype, code, tid = struct.unpack_from("<HHI", rest)
        params = struct.unpack_from(f"<{(length - 12) // 4}I", rest, 8)
        assert ctype == 1

        if code == 0x1002:
            assert tid == 1, "Fuji verwacht tid 1 bij OpenSession"
            session_open = True
            send(c, 3, 0x2001, tid)
        elif not session_open:
            send(c, 3, 0x2003, tid)
        elif code == 0x1015:
            prop = params[0]
            if prop == 0xD212:
                polls["n"] += 1
                if polls["n"] == 3:
                    state[0xDF00] = 6  # "gebruiker drukte OK"
                ev = [(0xDF00, state[0xDF00]), (0xD222, len(FILES))]
                payload = struct.pack("<H", len(ev)) + b"".join(struct.pack("<HI", *e) for e in ev)
            elif prop in state:
                v = state[prop]
                payload = struct.pack("<H", v) if prop in (0xDF00, 0xDF01, 0xD227) else struct.pack("<I", v)
            else:
                payload = b""
            send(c, 2, code, tid, payload)
            send(c, 3, 0x2001, tid)
        elif code == 0x1016:
            dhdr = recv_exact(c, 12)
            dlen, dtype, dcode, dtid = struct.unpack("<IHHI", dhdr)
            assert dtype == 2 and dcode == 0x1016 and dtid == tid
            data = recv_exact(c, dlen - 12)
            prop = params[0]
            state[prop] = int.from_bytes(data, "little")
            print(f"[fake] set 0x{prop:04X} = 0x{state[prop]:X}")
            send(c, 3, 0x2001, tid)
        elif code == 0x101C:
            send(c, 3, 0x2001, tid)
        elif code == 0x1018:
            send(c, 3, 0x2001, tid)
        elif code == 0x1008:
            h = params[0]
            if 1 <= h <= len(FILES):
                last_info = h
                time.sleep(DELAY / 20)
                send(c, 2, code, tid, object_info(h - 1))
                send(c, 3, 0x2001, tid)
            else:
                send(c, 3, 0x2009, tid)
        elif code == 0x100A:
            h = params[0]
            if h != last_info:
                print(f"[fake] GetThumb {h} zonder GetObjectInfo ervoor (echte camera hangt hier)")
                send(c, 3, 0x2002, tid)
            else:
                time.sleep(DELAY / 10)
                send(c, 2, code, tid, make_thumb(h - 1))
                send(c, 3, 0x2001, tid)
            last_info = None
        elif code == 0x101B:
            h, off, n = params
            data = FILES[h - 1][2][off:off + n]
            time.sleep(DELAY)
            send(c, 2, code, tid, data)
            send(c, 3, 0x2001, tid)
        elif code == 0x1003:
            send(c, 3, 0x2001, tid)
        else:
            send(c, 3, 0x2005, tid)


def side_port(port):
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((HOST, port))
    s.listen()
    while True:
        c, _ = s.accept()
        c.sendall(b"\x00" * 1000)  # wat liveview-ruis


def main():
    for p in (55741, 55742):
        threading.Thread(target=side_port, args=(p,), daemon=True).start()
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((HOST, 55740))
    s.listen()
    print("[fake] X-T3 luistert op 55740", flush=True)
    once = "--once" in sys.argv
    while True:
        c, _ = s.accept()
        polls["n"] = 0
        state[0xDF00] = 0
        try:
            handle(c)
        except (ConnectionError, OSError):
            pass  # client hing op (bv. een poort-check); gewoon doorgaan
        c.close()
        if once:
            return


if __name__ == "__main__":
    main()
