"""
Bladervenster: alle foto's op de camera als raster met thumbnails.

Alles wordt op één Canvas getekend (geen widget per foto), zodat het ook met
duizenden foto's vlot blijft. Thumbnails worden pas opgehaald als een tegel in
beeld komt.

Klik = selecteren/deselecteren · Shift-klik = reeks · Cmd/Ctrl-A = alles
"""

from __future__ import annotations

import io
import queue
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from . import APP_NAME
from .protocol import ObjectInfo
from .session import CameraSession
from .sync import human_size, probably_have

try:  # Pillow maakt van de JPEG-thumbnails iets wat Tk kan tonen
    from PIL import Image, ImageTk
except ImportError:  # zonder Pillow: tegels zonder plaatje
    Image = ImageTk = None

IS_MAC = sys.platform == "darwin"

TILE_W, TILE_H = 168, 168       # tegel incl. marge
THUMB_W, THUMB_H = 150, 112     # ruimte voor het plaatje
PAD = 8

RED = "#c0362c"
PANEL = "#202124"
PAPER = "#f4f4ef"


class GalleryWindow:
    def __init__(self, master: tk.Misc, out_dir: Path, host: str):
        self.out_dir = out_dir
        self.win = tk.Toplevel(master)
        self.win.title(f"{APP_NAME} — op de camera")
        self.win.geometry("920x680")
        self.win.minsize(560, 420)
        self.win.protocol("WM_DELETE_WINDOW", self.close)

        self.events: queue.Queue = queue.Queue()
        self.session = CameraSession(self.events, host=host)

        self.items: list[ObjectInfo] = []          # in weergavevolgorde (nieuwste eerst)
        self.by_handle: dict[int, ObjectInfo] = {}
        self.selected: set[int] = set()
        self.on_mac: set[int] = set()
        self.done: set[int] = set()
        self.photos: dict[int, object] = {}        # handle -> PhotoImage (referentie bewaren!)
        self.last_clicked: int | None = None
        self.columns = 1
        self.positions: dict[int, int] = {}       # handle -> plek in het raster
        self.downloading = False
        self.closed = False
        self.scan_finished = False
        self.layout_pending = False

        self.status = tk.StringVar(value="Verbinden met de camera ...")
        self.sel_text = tk.StringVar(value="")
        self.filter = tk.StringVar(value="alles")

        self._build()
        self.session.start()
        self.win.after(50, self._poll)

    # ---- opbouw ------------------------------------------------------------

    def _build(self):
        bar = ttk.Frame(self.win, padding=(12, 10))
        bar.pack(fill="x")
        ttk.Button(bar, text="Alles", command=self.select_all).pack(side="left")
        ttk.Button(bar, text="Niets", command=self.select_none).pack(side="left", padx=(4, 0))
        ttk.Button(bar, text="Alleen nieuwe", command=self.select_new).pack(side="left", padx=(4, 12))
        ttk.Label(bar, text="Toon:").pack(side="left")
        for label, value in (("alles", "alles"), ("JPEG", "jpg"), ("RAF", "raf")):
            ttk.Radiobutton(bar, text=label, value=value, variable=self.filter,
                            command=self._refilter).pack(side="left", padx=(4, 0))
        self.dl_button = ttk.Button(bar, text="Download selectie", command=self.download)
        self.dl_button.pack(side="right")
        ttk.Label(bar, textvariable=self.sel_text).pack(side="right", padx=10)

        body = ttk.Frame(self.win)
        body.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(body, background=PAPER, highlightthickness=0)
        scroll = ttk.Scrollbar(body, orient="vertical", command=self._yview)
        self.canvas.configure(yscrollcommand=scroll.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        foot = ttk.Frame(self.win, padding=(12, 6))
        foot.pack(fill="x")
        ttk.Label(foot, textvariable=self.status).pack(side="left")
        self.bar = ttk.Progressbar(foot, mode="determinate", length=220)
        self.bar.pack(side="right")

        self.canvas.bind("<Configure>", lambda e: self._layout())
        self.canvas.bind("<Button-1>", self._click)
        self.canvas.bind("<Shift-Button-1>", lambda e: self._click(e, shift=True))
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.canvas.bind(seq, self._wheel)
        mod = "Command" if IS_MAC else "Control"
        self.win.bind(f"<{mod}-a>", lambda e: self.select_all())

    # ---- tekenen -----------------------------------------------------------

    def _visible_items(self) -> list[ObjectInfo]:
        f = self.filter.get()
        if f == "alles":
            return self.items
        return [i for i in self.items if i.filename.lower().endswith("." + f)]

    def _layout(self):
        """Alles opnieuw tekenen (bij resize, filter of nieuwe lijst)."""
        width = max(self.canvas.winfo_width(), TILE_W)
        self.columns = max(1, (width - PAD) // TILE_W)
        self.canvas.delete("all")
        items = self._visible_items()
        self.positions = {info.handle: n for n, info in enumerate(items)}
        for n, info in enumerate(items):
            self._draw_tile(n, info)
        rows = (len(items) + self.columns - 1) // self.columns
        self.canvas.configure(scrollregion=(0, 0, width, rows * TILE_H + PAD * 2))
        if not items:
            msg = "Kaart uitlezen ..." if not self.scan_finished else "Geen bestanden."
            self.canvas.create_text(width // 2, 80, text=msg, fill="#888", font=("Helvetica", 14))
        self._request_visible_thumbs()

    def _layout_during_scan(self):
        self.layout_pending = False
        if self.closed or self.scan_finished:
            return
        self.items.sort(key=lambda i: (i.capture_date, i.filename), reverse=True)
        self._layout()

    def _tile_origin(self, n: int) -> tuple[int, int]:
        row, col = divmod(n, self.columns)
        return PAD + col * TILE_W, PAD + row * TILE_H

    def _draw_tile(self, n: int, info: ObjectInfo):
        x, y = self._tile_origin(n)
        tag = f"h{info.handle}"
        sel = info.handle in self.selected
        self.canvas.create_rectangle(x, y, x + TILE_W - PAD, y + TILE_H - PAD,
                                     fill="#ffffff" if not sel else "#fbe9e7",
                                     outline=RED if sel else "#d8d8d0",
                                     width=3 if sel else 1, tags=(tag, "tile"))
        cx, cy = x + (TILE_W - PAD) // 2, y + 6 + THUMB_H // 2
        photo = self.photos.get(info.handle)
        if photo is not None:
            self.canvas.create_image(cx, cy, image=photo, tags=(tag,))
        else:
            self.canvas.create_rectangle(cx - THUMB_W // 2, cy - THUMB_H // 2,
                                         cx + THUMB_W // 2, cy + THUMB_H // 2,
                                         fill=PANEL, outline="", tags=(tag,))
            ext = info.filename.rsplit(".", 1)[-1]
            self.canvas.create_text(cx, cy, text=ext, fill="#777", font=("Helvetica", 12, "bold"),
                                    tags=(tag,))
        self.canvas.create_text(x + 8, y + THUMB_H + 16, anchor="w", text=info.filename,
                                font=("Helvetica", 11, "bold"), fill="#1b1c1e", tags=(tag,))
        sub = info.date_folder
        if info.handle in self.done or info.handle in self.on_mac:
            sub += "  ✓ op Mac"
        self.canvas.create_text(x + 8, y + THUMB_H + 32, anchor="w", text=sub,
                                font=("Helvetica", 10), fill="#2e7d32" if "✓" in sub else "#777",
                                tags=(tag,))
        if sel:  # vinkje rechtsboven
            self.canvas.create_oval(x + TILE_W - PAD - 26, y + 4, x + TILE_W - PAD - 4, y + 26,
                                    fill=RED, outline="", tags=(tag,))
            self.canvas.create_text(x + TILE_W - PAD - 15, y + 15, text="✓", fill="white",
                                    font=("Helvetica", 12, "bold"), tags=(tag,))

    def _redraw_tile(self, handle: int):
        n = self.positions.get(handle)
        if n is None:
            return  # niet zichtbaar met het huidige filter
        self.canvas.delete(f"h{handle}")
        self._draw_tile(n, self.by_handle[handle])

    # ---- scrollen en thumbnails op aanvraag --------------------------------

    def _yview(self, *args):
        self.canvas.yview(*args)
        self._request_visible_thumbs()

    def _wheel(self, event):
        if event.num == 4:
            delta = -1
        elif event.num == 5:
            delta = 1
        else:
            delta = -event.delta if IS_MAC else -event.delta // 120
        self.canvas.yview_scroll(int(delta), "units")
        self._request_visible_thumbs()

    def _request_visible_thumbs(self):
        if ImageTk is None or self.closed:
            return
        items = self._visible_items()
        if not items:
            return
        top = self.canvas.canvasy(0)
        bottom = self.canvas.canvasy(self.canvas.winfo_height())
        first = max(0, int(top // TILE_H) * self.columns)
        last = min(len(items), (int(bottom // TILE_H) + 2) * self.columns)
        # Van onder naar boven aanvragen: de sessie haalt de laatste aanvraag eerst,
        # dus de bovenste tegels komen als eerste binnen.
        for info in reversed(items[first:last]):
            self.session.request_thumb(info.handle)

    def _set_thumb(self, handle: int, data: bytes | None):
        if not data or Image is None:
            return
        try:
            img = Image.open(io.BytesIO(data))
            img.thumbnail((THUMB_W, THUMB_H))
            self.photos[handle] = ImageTk.PhotoImage(img, master=self.win)
            self._redraw_tile(handle)
        except Exception:
            pass  # kapotte of rare thumbnail: dan maar zonder

    # ---- selecteren --------------------------------------------------------

    def _handle_at(self, event) -> int | None:
        x, y = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        col, row = int((x - PAD) // TILE_W), int((y - PAD) // TILE_H)
        if col < 0 or col >= self.columns or row < 0:
            return None
        n = row * self.columns + col
        items = self._visible_items()
        return items[n].handle if n < len(items) else None

    def _click(self, event, shift=False):
        handle = self._handle_at(event)
        if handle is None or self.downloading:
            return
        if shift and self.last_clicked is not None:
            items = [i.handle for i in self._visible_items()]
            if self.last_clicked in self.positions:
                a, b = sorted((self.positions[self.last_clicked], self.positions[handle]))
                for h in items[a:b + 1]:
                    self.selected.add(h)
                    self._redraw_tile(h)
        else:
            self.selected.symmetric_difference_update({handle})
            self._redraw_tile(handle)
        self.last_clicked = handle
        self._update_selection_text()
        return "break"

    def select_all(self):
        self.selected = {i.handle for i in self._visible_items()}
        self._layout()
        self._update_selection_text()

    def select_none(self):
        self.selected.clear()
        self._layout()
        self._update_selection_text()

    def select_new(self):
        self.selected = {i.handle for i in self._visible_items()
                         if i.handle not in self.on_mac and i.handle not in self.done}
        self._layout()
        self._update_selection_text()

    def _refilter(self):
        self.canvas.yview_moveto(0)
        self._layout()

    def _update_selection_text(self):
        n = len(self.selected)
        self.sel_text.set(f"{n} geselecteerd" if n else "")
        if self.downloading:
            return
        self.dl_button.configure(text=f"Download {n}" if n else "Download selectie")

    # ---- downloaden --------------------------------------------------------

    def download(self):
        if self.downloading:
            self.session.stop_download()
            self.status.set("Stoppen na dit blok ...")
            return
        chosen = [i for i in self.items if i.handle in self.selected]
        if not chosen:
            messagebox.showinfo(APP_NAME, "Klik eerst op de foto's die je wilt hebben "
                                          "(of kies Alles / Alleen nieuwe).", parent=self.win)
            return
        self.downloading = True
        self.dl_button.configure(text="Stop")
        self.bar.configure(maximum=len(chosen), value=0)
        self.session.download(chosen, self.out_dir)

    # ---- berichten uit de sessie -------------------------------------------

    def _poll(self):
        if self.closed:
            return
        try:
            for _ in range(200):  # niet eindeloos in één tik, UI moet blijven reageren
                self._handle(self.events.get_nowait())
        except queue.Empty:
            pass
        self.win.after(50, self._poll)

    def _handle(self, msg):
        kind = msg[0]
        if kind == "status":
            self.status.set(msg[1])
        elif kind == "connected":
            _, name, total = msg
            self.win.title(f"{APP_NAME} — {name} ({total} bestanden)")
            self.bar.configure(maximum=max(total, 1), value=0)
        elif kind == "scan":
            _, done, total = msg
            self.bar.configure(value=done)
            self.status.set(f"Kaart uitlezen: {done} van {total} ...")
        elif kind == "info":
            # Tijdens het uitlezen al tonen, maar hooguit eens per seconde hertekenen
            info = msg[1]
            self.items.append(info)
            self.by_handle[info.handle] = info
            if not self.layout_pending:
                self.layout_pending = True
                self.win.after(1000, self._layout_during_scan)
        elif kind == "scan_done":
            self.scan_finished = True
            self.items = sorted(msg[1], key=lambda i: (i.capture_date, i.filename), reverse=True)
            self.by_handle = {i.handle: i for i in self.items}
            self.on_mac = {i.handle for i in self.items if probably_have(self.out_dir, i)}
            self.bar.configure(value=0)
            self._layout()
        elif kind == "thumb":
            self._set_thumb(msg[1], msg[2])
        elif kind == "dl_start":
            _, info, index, total = msg
            self.bar.configure(value=index - 1)
            self.status.set(f"Downloaden {index}/{total}: {info.filename}")
        elif kind == "dl_progress":
            _, info, done, total, speed = msg
            self.status.set(f"{info.filename}: {human_size(done)} van {human_size(total)}"
                            f" · {human_size(speed)}/s")
        elif kind == "dl_done":
            _, info, _outcome = msg
            self.done.add(info.handle)
            self.selected.discard(info.handle)
            self.bar.step(1)
            self._redraw_tile(info.handle)
            self._update_selection_text()
        elif kind == "dl_all_done":
            self.downloading = False
            self._update_selection_text()
            self.bar.configure(value=0)
            if msg[1]:
                self._open_folder()
        elif kind == "error":
            if not self.closed:
                messagebox.showerror(APP_NAME, msg[1] + "\n\nDetails staan in het logbestand "
                                     "(knop 'Log' in het hoofdvenster).", parent=self.win)
                self.status.set(msg[1].splitlines()[0])
        elif kind == "closed":
            self.downloading = False
            self.dl_button.state(["disabled"])
            if not self.closed:
                self.status.set(self.status.get() + "  (verbinding gesloten)")

    def _open_folder(self):
        try:
            subprocess.Popen(["open" if IS_MAC else "xdg-open", str(self.out_dir)])
        except OSError:
            pass

    def close(self):
        self.closed = True
        self.session.close()
        self.win.destroy()
