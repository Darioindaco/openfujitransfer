"""
De Mac-app (Tkinter, zit standaard in Python — geen extra dependencies).

Het werk (netwerk, downloaden) gebeurt in een aparte thread. Die stuurt
berichten via een queue; het venster leest die queue elke 50 ms uit. Zo
blijft de UI soepel, en raakt alleen de hoofdthread Tkinter aan.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import APP_NAME, __version__
from .protocol import CAMERA_IP, Cancelled, FujiError
from .sync import DEFAULT_OUT, SyncListener, SyncOptions, human_size, run_sync

IS_MAC = sys.platform == "darwin"

if IS_MAC:
    SETTINGS_FILE = Path("~/Library/Application Support/Open FujiTransfer/settings.json").expanduser()
else:
    SETTINGS_FILE = Path("~/.config/open-fujitransfer/settings.json").expanduser()

FILE_TYPES = {"alles": (), "jpg": ("jpg",), "raf": ("raf",)}


# ---------------------------------------------------------------------------
# Instellingen onthouden tussen sessies
# ---------------------------------------------------------------------------

def load_settings() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_settings(data: dict) -> None:
    try:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(data, indent=2))
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Brug tussen sync-thread en venster
# ---------------------------------------------------------------------------

class QueueListener(SyncListener):
    def __init__(self, q: queue.Queue):
        self.q = q

    def status(self, message):
        self.q.put(("status", message))

    def files_found(self, files):
        self.q.put(("files", files))

    def file_started(self, info, index, total):
        self.q.put(("started", info, index, total))

    def file_progress(self, info, done, total, bytes_per_sec):
        self.q.put(("progress", info, done, total, bytes_per_sec))

    def file_finished(self, info, outcome):
        self.q.put(("finished", info, outcome))


# ---------------------------------------------------------------------------
# Het venster
# ---------------------------------------------------------------------------

class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.q: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None
        self.cancel = threading.Event()
        self.rows: dict[int, str] = {}       # object-handle -> treeview-id

        settings = load_settings()
        self.out_dir = tk.StringVar(value=settings.get("out_dir", str(DEFAULT_OUT)))
        self.file_type = tk.StringVar(value=settings.get("file_type", "alles"))
        self.use_latest = tk.BooleanVar(value=settings.get("use_latest", False))
        self.latest_n = tk.StringVar(value=str(settings.get("latest_n", 20)))
        self.status_text = tk.StringVar(value="Klaar om te verbinden.")
        self.overall_text = tk.StringVar(value="")
        self.file_text = tk.StringVar(value="")

        root.title(APP_NAME)
        root.minsize(560, 600)
        root.geometry("620x700")
        self._style()
        self._build()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(50, self.poll_queue)

    # ---- opmaak ------------------------------------------------------------

    def _style(self):
        style = ttk.Style(self.root)
        if not IS_MAC:
            style.theme_use("clam")
        style.configure("Title.TLabel", font=("Helvetica", 22, "bold"))
        style.configure("Sub.TLabel", foreground="#666666")
        style.configure("Step.TLabel", font=("Helvetica", 13))
        style.configure("Num.TLabel", font=("Helvetica", 13, "bold"), foreground="#b0262c")
        style.configure("Status.TLabel", font=("Helvetica", 13, "bold"))
        style.configure("Horizontal.TProgressbar", background="#b0262c")
        style.configure("Big.TButton", font=("Helvetica", 14, "bold"), padding=(18, 8))

    def _build(self):
        outer = ttk.Frame(self.root, padding=20)
        outer.pack(fill="both", expand=True)

        # Kop
        head = ttk.Frame(outer)
        head.pack(fill="x")
        ttk.Label(head, text=APP_NAME, style="Title.TLabel").pack(side="left")
        ttk.Label(head, text=f"v{__version__}", style="Sub.TLabel").pack(side="right", anchor="s")
        ttk.Label(outer, text="Foto's van je Fujifilm via WiFi naar je Mac — open source.",
                  style="Sub.TLabel").pack(anchor="w", pady=(2, 14))

        # Stappen
        steps = ttk.LabelFrame(outer, text=" Zo verbind je ", padding=(14, 8))
        steps.pack(fill="x")
        for n, text in enumerate([
            "Camera: ▶ (afspelen) → MENU → Draadloze communicatie",
            "Mac: verbind met het WiFi-netwerk FUJIFILM-X-T3-…",
            "Klik op Foto's ophalen en druk zo nodig OK op de camera",
        ], start=1):
            row = ttk.Frame(steps)
            row.pack(fill="x", pady=2)
            ttk.Label(row, text=f"{n}", style="Num.TLabel", width=2).pack(side="left")
            ttk.Label(row, text=text, style="Step.TLabel").pack(side="left")

        # Instellingen
        opts = ttk.LabelFrame(outer, text=" Instellingen ", padding=(14, 8))
        opts.pack(fill="x", pady=(12, 0))

        r1 = ttk.Frame(opts)
        r1.pack(fill="x", pady=3)
        ttk.Label(r1, text="Opslaan in:", width=12).pack(side="left")
        ttk.Entry(r1, textvariable=self.out_dir).pack(side="left", fill="x", expand=True)
        ttk.Button(r1, text="Wijzig…", command=self.choose_folder).pack(side="left", padx=(6, 0))

        r2 = ttk.Frame(opts)
        r2.pack(fill="x", pady=3)
        ttk.Label(r2, text="Bestanden:", width=12).pack(side="left")
        for label, value in (("Alles", "alles"), ("Alleen JPEG", "jpg"), ("Alleen RAF", "raf")):
            ttk.Radiobutton(r2, text=label, value=value, variable=self.file_type).pack(side="left", padx=(0, 10))

        r3 = ttk.Frame(opts)
        r3.pack(fill="x", pady=3)
        ttk.Label(r3, text="", width=12).pack(side="left")
        ttk.Checkbutton(r3, text="Alleen de nieuwste", variable=self.use_latest).pack(side="left")
        ttk.Spinbox(r3, from_=1, to=9999, width=6, textvariable=self.latest_n).pack(side="left", padx=6)
        ttk.Label(r3, text="bestanden").pack(side="left")

        # Knoppen
        buttons = ttk.Frame(outer)
        buttons.pack(fill="x", pady=(16, 8))
        self.go_button = ttk.Button(buttons, text="Foto's ophalen", style="Big.TButton",
                                    command=self.start_download)
        self.go_button.pack(side="left")
        self.list_button = ttk.Button(buttons, text="Wat staat erop?", command=self.start_list)
        self.list_button.pack(side="left", padx=8)
        ttk.Button(buttons, text="Open map", command=self.open_folder).pack(side="right")

        # Voortgang
        ttk.Label(outer, textvariable=self.status_text, style="Status.TLabel",
                  wraplength=560).pack(anchor="w", pady=(4, 4))
        self.overall_bar = ttk.Progressbar(outer, mode="determinate")
        self.overall_bar.pack(fill="x")
        ttk.Label(outer, textvariable=self.overall_text, style="Sub.TLabel").pack(anchor="w")
        self.file_bar = ttk.Progressbar(outer, mode="determinate")
        self.file_bar.pack(fill="x", pady=(6, 0))
        ttk.Label(outer, textvariable=self.file_text, style="Sub.TLabel").pack(anchor="w")

        # Bestandenlijst
        table = ttk.Frame(outer)
        table.pack(fill="both", expand=True, pady=(10, 0))
        cols = ("bestand", "datum", "grootte", "status")
        self.tree = ttk.Treeview(table, columns=cols, show="headings", height=8)
        for col, width, anchor in (("bestand", 170, "w"), ("datum", 110, "w"),
                                   ("grootte", 90, "e"), ("status", 120, "w")):
            self.tree.heading(col, text=col.capitalize())
            self.tree.column(col, width=width, anchor=anchor, stretch=(col == "bestand"))
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.tag_configure("nieuw", foreground="#2e7d32")
        self.tree.tag_configure("bestond al", foreground="#888888")
        self.tree.tag_configure("bezig", foreground="#b0262c")
        self.tree.tag_configure("mislukt", foreground="#c62828")

    # ---- acties ------------------------------------------------------------

    def choose_folder(self):
        folder = filedialog.askdirectory(initialdir=self.out_dir.get() or str(Path.home()))
        if folder:
            self.out_dir.set(folder)

    def open_folder(self):
        path = Path(self.out_dir.get()).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        opener = "open" if IS_MAC else "xdg-open"
        try:
            subprocess.Popen([opener, str(path)])
        except OSError:
            pass

    def _options(self, list_only: bool) -> SyncOptions:
        latest = None
        if self.use_latest.get():
            try:
                latest = max(1, int(self.latest_n.get()))
            except ValueError:
                latest = None
        return SyncOptions(
            out_dir=Path(self.out_dir.get()).expanduser(),
            only=FILE_TYPES.get(self.file_type.get(), ()),
            latest=latest,
            list_only=list_only,
            # Alleen voor testen met tests/fake_camera.py:
            host=os.environ.get("OPEN_FUJITRANSFER_HOST", CAMERA_IP),
        )

    def _save(self):
        save_settings({
            "out_dir": self.out_dir.get(),
            "file_type": self.file_type.get(),
            "use_latest": self.use_latest.get(),
            "latest_n": self.latest_n.get(),
        })

    def start_download(self):
        if self.worker and self.worker.is_alive():
            self.cancel.set()
            self.status_text.set("Stoppen …")
            return
        self._start(list_only=False)

    def start_list(self):
        if not (self.worker and self.worker.is_alive()):
            self._start(list_only=True)

    def _start(self, list_only: bool):
        self._save()
        self.cancel = threading.Event()
        self.tree.delete(*self.tree.get_children())
        self.rows.clear()
        self.overall_bar["value"] = 0
        self.file_bar["value"] = 0
        self.overall_text.set("")
        self.file_text.set("")
        self.go_button.configure(text="Stop")
        self.list_button.state(["disabled"])

        opts = self._options(list_only)
        listener = QueueListener(self.q)

        def work():
            try:
                result = run_sync(opts, listener, self.cancel)
                self.q.put(("done", result, list_only))
            except Cancelled:
                self.q.put(("error", "Gestopt."))
            except FujiError as e:
                self.q.put(("error", str(e)))
            except Exception as e:  # onverwacht: laat het zien in plaats van stil te falen
                self.q.put(("error", f"Onverwachte fout: {e!r}"))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    # ---- berichten uit de sync-thread verwerken ---------------------------

    def poll_queue(self):
        try:
            while True:
                self.handle(self.q.get_nowait())
        except queue.Empty:
            pass
        self.root.after(50, self.poll_queue)

    def handle(self, msg):
        kind = msg[0]
        if kind == "status":
            self.status_text.set(msg[1])
        elif kind == "files":
            for info in msg[1]:
                self.rows[info.handle] = self.tree.insert("", "end", values=(
                    info.filename, info.date_folder, human_size(info.size), "wacht"))
            self.overall_bar["maximum"] = max(len(msg[1]), 1)
        elif kind == "started":
            _, info, index, total = msg
            self.overall_text.set(f"Bestand {index} van {total}")
            self.overall_bar["value"] = index - 1
            self._set_row(info, "bezig …", "bezig")
            self.tree.see(self.rows.get(info.handle, ""))
            self.file_bar["value"] = 0
        elif kind == "progress":
            _, info, done, total, speed = msg
            self.file_bar["maximum"] = max(total, 1)
            self.file_bar["value"] = done
            self.file_text.set(f"{info.filename} — {human_size(done)} van {human_size(total)}"
                               f" · {human_size(speed)}/s")
        elif kind == "finished":
            _, info, outcome = msg
            self._set_row(info, outcome, outcome)
            self.overall_bar["value"] = self.overall_bar["value"] + 1
        elif kind == "done":
            _, result, list_only = msg
            self._reset_buttons()
            if not list_only:
                self.overall_bar["value"] = self.overall_bar["maximum"]
                self.file_text.set("")
        elif kind == "error":
            self._reset_buttons()
            self.status_text.set(msg[1])
            if msg[1] != "Gestopt.":
                messagebox.showerror(APP_NAME, msg[1])

    def _set_row(self, info, text, tag):
        row = self.rows.get(info.handle)
        if row:
            values = list(self.tree.item(row, "values"))
            values[3] = text
            self.tree.item(row, values=values, tags=(tag,))

    def _reset_buttons(self):
        self.go_button.configure(text="Foto's ophalen")
        self.list_button.state(["!disabled"])

    def on_close(self):
        self._save()
        self.cancel.set()
        self.root.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
