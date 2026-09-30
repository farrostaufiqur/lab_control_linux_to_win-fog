import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from labagent.killswitch import DriveWatcher, cleanup_script, name_hash, watch_forever  # noqa: E402

SECRET = "234hv9fd2f2d23hsdfgd6.txt"


class FakeDrives:
    def __init__(self):
        self.drives = {}        # root -> identitas volume
        self.files = {}         # root -> daftar nama, atau None bila belum terbaca
        self.reads = []

    def list_drives(self): return dict(self.drives)

    def list_names(self, root):
        self.reads.append(root)
        return self.files.get(root)

    def watcher(self):
        return DriveWatcher(name_hash(SECRET), self.list_drives, self.list_names)


def test_name_hash_abaikan_huruf_besar_dan_spasi():
    assert name_hash(" 234HV9fd2f2d23hsdfgd6.TXT ") == name_hash(SECRET)
    assert name_hash("lain.txt") != name_hash(SECRET)


def test_drive_dengan_file_rahasia_memicu():
    d = FakeDrives()
    d.drives["E:\\"], d.files["E:\\"] = 1, ["foto.jpg", SECRET]
    assert d.watcher().scan() == "E:\\"


def test_drive_tanpa_file_rahasia_tidak_memicu():
    d = FakeDrives()
    d.drives["E:\\"], d.files["E:\\"] = 1, ["foto.jpg", "tugas.docx"]
    assert d.watcher().scan() is None


def test_drive_dibaca_sekali_selama_terpasang():
    d = FakeDrives()
    d.drives["E:\\"], d.files["E:\\"] = 1, ["foto.jpg"]
    w = d.watcher()
    for _ in range(5):
        assert w.scan() is None
    assert d.reads == ["E:\\"]


def test_dibaca_lagi_setelah_dicabut_dan_dipasang_ulang():
    d = FakeDrives()
    d.drives["E:\\"], d.files["E:\\"] = 1, ["foto.jpg"]
    w = d.watcher()
    w.scan()
    d.drives.clear()
    w.scan()
    d.drives["E:\\"], d.files["E:\\"] = 1, ["foto.jpg", SECRET]
    assert w.scan() == "E:\\"
    assert len(d.reads) == 2


def test_flashdisk_lain_di_huruf_drive_yang_sama_dibaca_ulang():
    d = FakeDrives()
    d.drives["E:\\"], d.files["E:\\"] = 1, ["foto.jpg"]
    w = d.watcher()
    w.scan()
    d.drives["E:\\"], d.files["E:\\"] = 2, [SECRET]   # dicabut dan diganti antara dua pemindaian
    assert w.scan() == "E:\\"


def test_media_belum_terbaca_dicoba_lagi():
    d = FakeDrives()
    d.drives["E:\\"], d.files["E:\\"] = 1, None
    w = d.watcher()
    assert w.scan() is None
    d.files["E:\\"] = [SECRET]
    assert w.scan() == "E:\\"


def test_watch_forever_memanggil_on_trigger_sekali_lalu_berhenti():
    d = FakeDrives()
    d.drives["E:\\"], d.files["E:\\"] = 1, [SECRET]
    fired = []
    watch_forever(d.watcher(), fired.append, threading.Event(), interval=0)
    assert fired == ["E:\\"]


def test_watch_forever_tahan_error_drive():
    calls = {"n": 0}

    def list_drives():
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("drive bermasalah")
        return {"E:\\": 1}

    fired = []
    watcher = DriveWatcher(name_hash(SECRET), list_drives, lambda root: [SECRET])
    watch_forever(watcher, fired.append, threading.Event(), interval=0)
    assert fired == ["E:\\"]


def test_cleanup_script_menghapus_semua_folder_dan_dirinya_sendiri():
    script = cleanup_script(r"C:\Program Files\LabAgent", r"C:\ProgramData\LabAgent")
    assert 'rmdir /s /q "C:\\Program Files\\LabAgent"' in script
    assert 'rmdir /s /q "C:\\ProgramData\\LabAgent"' in script
    assert 'del "%~f0"' in script
    assert "goto done0" in script and "goto done1" in script
