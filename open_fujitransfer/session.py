"""
Een open verbinding met de camera, voor het bladervenster.

De camera kan maar één ding tegelijk, dus al het camerawerk gebeurt in één
worker-thread die taken uit een prioriteitsrij haalt:

    0 = downloaden  (gaat altijd voor)
    1 = thumbnails  (nieuwste aanvraag eerst, zodat wat je nu ziet als eerste laadt)

Alles wat er gebeurt gaat als bericht (tuple) naar de `events`-queue; het
venster leest die uit. Deze module raakt zelf geen Tkinter aan.

Berichten:
    ("status", tekst)
    ("connected", cameranaam, aantal)
    ("scan", gedaan, totaal)
    ("info", ObjectInfo)
    ("scan_done", [ObjectInfo, ...])
    ("thumb", handle, jpeg-bytes of None)
    ("dl_start", info, nummer, totaal)
    ("dl_progress", info, gedaan, totaal, bytes_per_sec)
    ("dl_done", info, "nieuw" | "bestond al")
    ("dl_all_done", nieuw, overgeslagen)
    ("error", tekst)
    ("closed",)
"""

from __future__ import annotations

import itertools
import logging
import queue
import threading
from pathlib import Path

from . import APP_NAME
from .protocol import CAMERA_IP, Cancelled, FujiCamera, FujiError, ObjectInfo
from .sync import SyncListener, download_one, scan_card

logger = logging.getLogger("open_fujitransfer")

PRIO_DOWNLOAD = 0
PRIO_THUMB = 1


class _Forward(SyncListener):
    """Stuurt SyncListener-aanroepen door als berichten."""

    def __init__(self, events: queue.Queue):
        self.events = events

    def status(self, message):
        self.events.put(("status", message))

    def scan_progress(self, done, total):
        self.events.put(("scan", done, total))

    def file_progress(self, info, done, total, bytes_per_sec):
        self.events.put(("dl_progress", info, done, total, bytes_per_sec))


class CameraSession:
    def __init__(self, events: queue.Queue, host: str = CAMERA_IP, client_name: str = APP_NAME):
        self.events = events
        self.listener = _Forward(events)
        self.cam = FujiCamera(host=host, client_name=client_name, on_status=self.listener.status)
        self.tasks: queue.PriorityQueue = queue.PriorityQueue()
        self.counter = itertools.count()
        self.thumbs_requested: set[int] = set()
        self.cancel_download = threading.Event()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    # ---- aanroepen vanuit het venster (thread-safe) -----------------------

    def start(self) -> None:
        self.thread.start()

    def request_thumb(self, handle: int) -> None:
        if handle in self.thumbs_requested:
            return
        self.thumbs_requested.add(handle)
        # Negatieve teller: de laatst aangevraagde thumbnail komt als eerste
        self.tasks.put((PRIO_THUMB, -next(self.counter), "thumb", handle))

    def download(self, infos: list[ObjectInfo], out_dir: Path) -> None:
        self.cancel_download.clear()
        self.tasks.put((PRIO_DOWNLOAD, next(self.counter), "download", (infos, out_dir)))

    def stop_download(self) -> None:
        self.cancel_download.set()

    def close(self) -> None:
        self.stop.set()
        self.cancel_download.set()
        self.tasks.put((-1, 0, "close", None))

    # ---- de worker ---------------------------------------------------------

    def _run(self) -> None:
        try:
            self.cam.connect()
            self.cam.setup()
            total = len(self.cam.object_handles())
            self.events.put(("connected", self.cam.camera_name, total))
            self.events.put(("status", f"Kaart uitlezen ({total} bestanden) ..."))
            infos = scan_card(self.cam, self.listener, self.stop,
                              on_info=lambda i: self.events.put(("info", i)))
            self.events.put(("scan_done", infos))
            self.events.put(("status", f"{len(infos)} bestanden op de camera."))

            while not self.stop.is_set():
                _prio, _n, kind, payload = self.tasks.get()
                if kind == "close":
                    break
                if kind == "thumb":
                    self._thumb(payload)
                elif kind == "download":
                    self._download(*payload)
        except Cancelled:
            pass
        except FujiError as e:
            logger.exception("sessie gestopt")
            self.events.put(("error", str(e)))
        except Exception as e:  # onverwacht: laat het zien
            logger.exception("onverwachte fout")
            self.events.put(("error", f"Onverwachte fout: {e!r}"))
        finally:
            self.cam.close()
            self.events.put(("closed",))

    def _thumb(self, handle: int) -> None:
        try:
            data = self.cam.get_thumb(handle)
        except FujiError as e:
            if "verbroken" in str(e) or "antwoordt niet" in str(e):
                raise
            data = None
        self.events.put(("thumb", handle, data))

    def _download(self, infos: list[ObjectInfo], out_dir: Path) -> None:
        new = skipped = 0
        try:
            for index, info in enumerate(infos, start=1):
                if self.cancel_download.is_set():
                    raise Cancelled()
                self.events.put(("dl_start", info, index, len(infos)))
                outcome = download_one(self.cam, info.handle, out_dir, self.listener,
                                       self.cancel_download, info)
                new += outcome == "nieuw"
                skipped += outcome == "bestond al"
                self.events.put(("dl_done", info, outcome))
            self.events.put(("status", f"Klaar: {new} nieuw, {skipped} had je al."))
        except Cancelled:
            self.events.put(("status", f"Gestopt na {new} nieuwe bestanden."))
        self.events.put(("dl_all_done", new, skipped))
