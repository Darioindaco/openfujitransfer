"""
De eigenlijke 'haal alles op'-logica, gedeeld door de app en de command line.

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
    def files_found(self, files: list[ObjectInfo]) -> None: ...
    def file_started(self, info: ObjectInfo, index: int, total: int) -> None: ...
    def file_progress(self, info: ObjectInfo, done: int, total: int, bytes_per_sec: float) -> None: ...
    def file_finished(self, info: ObjectInfo, outcome: str) -> None: ...   # "nieuw" | "bestond al" | "mislukt"


def destination(out_dir: Path, info: ObjectInfo) -> Path:
    return out_dir / info.date_folder / info.filename


def already_have(out_dir: Path, info: ObjectInfo) -> bool:
    dest = destination(out_dir, info)
    return dest.exists() and dest.stat().st_size == info.size


def select_files(infos: list[ObjectInfo], opts: SyncOptions) -> list[ObjectInfo]:
    """Nieuwste eerst, dan filteren op type, dan eventueel de N nieuwste."""
    infos = sorted(infos, key=lambda i: (i.capture_date, i.filename), reverse=True)
    if opts.only:
        exts = tuple("." + e.lower().lstrip(".") for e in opts.only)
        infos = [i for i in infos if i.filename.lower().endswith(exts)]
    if opts.latest:
        infos = infos[:opts.latest]
    return infos


def run_sync(opts: SyncOptions, listener: SyncListener,
             cancel: threading.Event | None = None) -> SyncResult:
    """Verbind, lees de kaart, download wat nieuw is. Gooit FujiError of Cancelled."""
    cancel = cancel or threading.Event()
    result = SyncResult()
    cam = FujiCamera(host=opts.host, client_name=opts.client_name, on_status=listener.status)
    try:
        cam.connect()
        cam.setup()
        handles = cam.object_handles()
        if not handles:
            listener.status("Geen foto's gevonden op de kaart.")
            listener.files_found([])
            return result

        listener.status(f"Kaart uitlezen ({len(handles)} bestanden) ...")
        with cam.with_correct_sizes():
            infos = []
            for h in handles:
                if cancel.is_set():
                    raise Cancelled()
                try:
                    infos.append(cam.get_object_info(h))
                except FujiError as e:
                    listener.status(f"Overgeslagen: {e}")

            files = select_files(infos, opts)
            result.files = files
            listener.files_found(files)
            if opts.list_only:
                listener.status(f"{len(files)} bestanden op de kaart.")
                return result

            for index, info in enumerate(files, start=1):
                if cancel.is_set():
                    raise Cancelled()
                listener.file_started(info, index, len(files))
                if already_have(opts.out_dir, info):
                    result.skipped += 1
                    listener.file_finished(info, "bestond al")
                    continue

                dest = destination(opts.out_dir, info)
                dest.parent.mkdir(parents=True, exist_ok=True)
                started = time.time()

                def progress(done, total, info=info, started=started):
                    speed = done / max(time.time() - started, 0.001)
                    listener.file_progress(info, done, total, speed)

                listener.status(f"Downloaden {index}/{len(files)}: {info.filename}")
                cam.download(info, dest, progress, cancel)
                result.new += 1
                listener.file_finished(info, "nieuw")

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
