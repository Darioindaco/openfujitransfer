"""
Tests tegen de nep-camera. Draaien met:

    python3 -m unittest discover -s tests -v
"""

import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import fake_camera  # noqa: E402
from open_fujitransfer.protocol import ObjectInfo, parse_object_info  # noqa: E402
from open_fujitransfer.sync import SyncListener, SyncOptions, run_sync, select_files  # noqa: E402


class RecordingListener(SyncListener):
    def __init__(self):
        self.outcomes = {}
        self.statuses = []

    def status(self, message):
        self.statuses.append(message)

    def file_finished(self, info, outcome):
        self.outcomes[info.filename] = outcome


class AgainstFakeCamera(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.proc = subprocess.Popen([sys.executable, str(HERE / "fake_camera.py")],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", 55740), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        time.sleep(0.2)  # de testverbinding hierboven netjes laten afhandelen

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def sync(self, **kw):
        listener = RecordingListener()
        result = run_sync(SyncOptions(out_dir=self.out, host="127.0.0.1", **kw), listener)
        return result, listener

    def test_downloads_everything_byte_for_byte(self):
        result, _ = self.sync()
        self.assertEqual(result.new, 3)
        for name, date, data in fake_camera.FILES:
            folder = f"{date[0:4]}-{date[4:6]}-{date[6:8]}"
            self.assertEqual((self.out / folder / name).read_bytes(), data, name)
        self.assertEqual(list(self.out.rglob("*.part")), [])

    def test_second_run_skips_existing(self):
        self.sync()
        result, listener = self.sync()
        self.assertEqual((result.new, result.skipped), (0, 3))
        self.assertEqual(set(listener.outcomes.values()), {"bestond al"})

    def test_only_jpg_latest_one(self):
        result, listener = self.sync(only=("jpg",), latest=1)
        self.assertEqual(list(listener.outcomes), ["DSCF0002.JPG"])

    def test_list_only_downloads_nothing(self):
        result, _ = self.sync(list_only=True)
        self.assertEqual(len(result.files), 3)
        self.assertEqual(list(self.out.rglob("*.*")), [])


class Parsing(unittest.TestCase):
    def test_object_info(self):
        fake_camera.state[0xD227] = 1
        info = parse_object_info(1, fake_camera.object_info(0))
        self.assertEqual(info.filename, "DSCF0001.JPG")
        self.assertEqual(info.size, 2_500_000)
        self.assertEqual(info.date_folder, "2026-10-30")

    def test_select_files_newest_first(self):
        infos = [ObjectInfo(1, "A.JPG", 1, "20250101T000000", 0),
                 ObjectInfo(2, "B.RAF", 1, "20260101T000000", 0),
                 ObjectInfo(3, "C.JPG", 1, "20260201T000000", 0)]
        names = [i.filename for i in select_files(infos, SyncOptions(only=("jpg",)))]
        self.assertEqual(names, ["C.JPG", "A.JPG"])

    def test_unknown_date(self):
        self.assertEqual(ObjectInfo(1, "X.JPG", 1, "", 0).date_folder, "onbekend")


if __name__ == "__main__":
    unittest.main()
