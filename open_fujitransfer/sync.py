"""
De 'haal alles op'-logica, gedeeld door de app en de command line.

De GUI en de CLI geven elk een SyncListener mee; run_sync() roept die aan bij
elke stap. Zo weet deze module niks van vensters of terminals.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import APP_NAME
from .protocol import CAMERA_IP, Cancelled, FujiCamera, FujiError, ObjectInfo

DEFAULT_OUT = Path("~/Pictures/Open FujiTransfer").expanduser()


@dataclass
class SyncOptions:
    out_dir: Path = DEFAULT_OUT
    only: tuple[str, ...] = ()          # bv. ("jpg",) of ("jpg", "raf"); leeg = alles
    latest: int | None = None           # alleen de N nieuwste
    list_only: bool = False
    host: str = CAMERA_IP
    client_name: str = APP_NAME


@dataclass
class SyncResult:
    new: int = 0
    skipped: int = 0
    failed: int = 0
    files: list[ObjectInfo] = field(default_factory=list)


class SyncListener:
    """Overschrijf wat je nodig hebt. Alles wordt vanuit de sync-thread aangeroepen."""

    def status(self, message: str) -> None: ...
    def scan_progress(self, done: int, total: int) -> None: ...
    def files_found(self, files: list[ObjectInfo]) -> None: ...
    def file_started(self, info: ObjectInfo, index: int, total: int) -> None: ...
    def file_progress(self, info: ObjectInfo, done: int, total: int, bytes_per_sec: float) -> None: ...
    def file_finished(self, info: ObjectInfo, outcome: str) -> None: ...   # "nieuw" | "bestond al"


def destination(out_dir: Path, info: ObjectInfo) -> Path:
    return out_dir / info.date_folder / info.filename


def already_have(out_dir: Path, info: ObjectInfo) -> bool:
    """Vereist een info met de échte grootte (zie FujiCamera.download_object)."""
    dest = destination(out_dir, info)
    return dest.exists() and dest.stat().st_size == info.size


def probably_have(out_dir: Path, info: ObjectInfo) -> bool:
    """Snelle check op naam alleen, voor in de lijst (de grootte is daar nog onbekend)."""
    return destination(out_dir, info).exists()


def select_files(infos: list[ObjectInfo], opts: SyncOptions) -> list[ObjectInfo]:
    """Nieuwste eerst, dan filteren op type, dan eventueel de N nieuwste."""
    infos = sorted(infos, key=lambda i: (i.capture_date, i.filename), reverse=True)
    if opts.only:
        exts = tuple("." + e.lower().lstrip(".") for e in opts.only)
        infos = [i for i in infos if i.filename.lower().endswith(exts)]
    if opts.latest:
        infos = infos[:opts.latest]
    return infos


def scan_card(cam: FujiCamera, listener: SyncListener, cancel: threading.Event,
              on_info=None) -> list[ObjectInfo]:
    """Lees naam en datum van alle foto's, met voortgang."""
    handles = cam.object_handles()
    infos = []
    last_report = 0.0
    for n, h in enumerate(handles, start=1):
        if cancel.is_set():
            raise Cancelled()
        try:
            info = cam.get_object_info(h)
            infos.append(info)
            if on_info:
                on_info(info)
        except FujiError as e:
            listener.status(f"Overgeslagen: {e}")
        if time.time() - last_report > 0.2 or n == len(handles):
            listener.scan_progress(n, len(handles))
            last_report = time.time()
    return infos


def download_one(cam: FujiCamera, handle: int, out_dir: Path, listener: SyncListener,
                 cancel: threading.Event, listed: ObjectInfo) -> str:
    """Download één foto naar out_dir/JJJJ-MM-DD/. Geeft 'nieuw' of 'bestond al'."""
    started = time.time()

    def choose(info: ObjectInfo):
        if already_have(out_dir, info):
            return None
        dest = destination(out_dir, info)
        dest.parent.mkdir(parents=True, exist_ok=True)
        return dest

    def progress(done, total):
        listener.file_progress(listed, done, total, done / max(time.time() - started, 0.001))

    info, dest = cam.download_object(handle, choose, progress, cancel)
    listed.size = info.size  # nu weten we de echte grootte
    return "nieuw" if dest is not None else "bestond al"


def run_sync(opts: SyncOptions, listener: SyncListener,
             cancel: threading.Event | None = None) -> SyncResult:
    """Verbind, lees de kaart, download wat nieuw is. Gooit FujiError of Cancelled."""
    cancel = cancel or threading.Event()
    result = SyncResult()
    cam = FujiCamera(host=opts.host, client_name=opts.client_name, on_status=listener.status)
    try:
        cam.connect()
        cam.setup()
        total = len(cam.object_handles())
        if not total:
            listener.status("Geen foto's gevonden op de kaart.")
            listener.files_found([])
            return result

        listener.status(f"Kaart uitlezen ({total} bestanden) ...")
        files = select_files(scan_card(cam, listener, cancel), opts)
        result.files = files
        listener.files_found(files)
        if opts.list_only:
            listener.status(f"{len(files)} bestanden op de kaart.")
            return result

        for index, info in enumerate(files, start=1):
            if cancel.is_set():
                raise Cancelled()
            listener.file_started(info, index, len(files))
            listener.status(f"Bestand {index}/{len(files)}: {info.filename}")
            outcome = download_one(cam, info.handle, opts.out_dir, listener, cancel, info)
            if outcome == "nieuw":
                result.new += 1
            else:
                result.skipped += 1
            listener.file_finished(info, outcome)

        listener.status(f"Klaar: {result.new} nieuw, {result.skipped} had je al.")
        return result
    finally:
        cam.close()


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)
