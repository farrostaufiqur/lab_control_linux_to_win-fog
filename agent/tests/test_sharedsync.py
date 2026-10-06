import hashlib
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from labagent import sharedsync  # noqa: E402
from labagent.sharedsync import Conflict, FolderSync, decide  # noqa: E402

PAST = 1_700_000_000.0           # mtime file lama (tidak dianggap sedang ditulis)
NOW = PAST + 10_000.0


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FakeServer:
    """Meniru server: indeks + isi file + aturan expect (409 bila SHA berbeda)."""

    def __init__(self):
        self.files = {}          # path -> {"data", "mtime"}
        self.calls = []

    def put(self, path, data, mtime=PAST):
        self.files[path] = {"data": data, "mtime": mtime}

    def client(self):
        return FakeClient(self)


class FakeClient:
    def __init__(self, server):
        self.s = server

    def index(self):
        return {p: {"sha256": sha(f["data"]), "size": len(f["data"]), "mtime": f["mtime"]} for p, f in self.s.files.items()}

    def download(self, path):
        self.s.calls.append(("download", path))
        data = self.s.files[path]["data"]
        yield data[: len(data) // 2]
        yield data[len(data) // 2:]

    def _check(self, path, expect):
        current = sha(self.s.files[path]["data"]) if path in self.s.files else "none"
        if expect is not None and current != expect:
            raise Conflict()

    def upload(self, path, file_path, mtime, expect):
        self.s.calls.append(("upload", path))
        self._check(path, expect)
        data = Path(file_path).read_bytes()
        self.s.files[path] = {"data": data, "mtime": mtime}
        return {"sha256": sha(data)}

    def delete(self, path, expect):
        self.s.calls.append(("delete", path))
        self._check(path, expect)
        self.s.files.pop(path, None)


class PC:
    """Satu komputer: folder lokal + status sinkron sendiri."""

    def __init__(self, tmp_path, server, mode, name="pc"):
        self.root = tmp_path / name
        self.folder = self.root / "Desktop" / "shared"
        self.server, self.mode = server, mode
        self.state = self.root / "shared.json"

    def sync(self):
        return FolderSync(self.state, clock=lambda: NOW).run(self.folder, self.mode, self.server.client())

    def write(self, rel, data, mtime=PAST):
        path = self.folder / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        os.utime(path, (mtime, mtime))

    def read(self, rel):
        path = self.folder / rel
        return path.read_bytes() if path.is_file() else None

    def delete(self, rel):
        (self.folder / rel).unlink()

    def files(self):
        return sorted(p.relative_to(self.folder).as_posix() for p in self.folder.rglob("*") if p.is_file())


@pytest.fixture
def server():
    return FakeServer()


# --- tabel keputusan -------------------------------------------------------------------------------
@pytest.mark.parametrize("mode,L,S,B,expected", [
    ("receive", "a", "a", None, "sync"), ("receive", None, None, "a", "sync"),
    ("receive", "x", "a", "a", "download"), ("receive", None, "a", "a", "download"), ("receive", None, "a", None, "download"),
    ("receive", "a", None, "a", "delete_local"), ("receive", "a", None, None, "noop"),
    ("send", "x", "a", "a", "upload"), ("send", "x", None, None, "upload"), ("send", "a", "b", "a", "noop"),
    ("send", None, "a", "a", "delete_server"), ("send", None, "a", None, "noop"), ("send", "a", None, "a", "noop"),
    ("both", "x", "a", "a", "upload"), ("both", "x", "a", "x", "download"), ("both", "a", None, None, "upload"),
    ("both", "a", None, "a", "delete_local"), ("both", "b", None, "a", "upload"), ("both", None, "a", None, "download"),
    ("both", None, "a", "a", "delete_server"), ("both", None, "b", "a", "download"),
])
def test_decide_table(mode, L, S, B, expected):
    assert decide(mode, L, S, B) == expected


def test_decide_conflict_newest_wins_and_tie_goes_to_server():
    assert decide("both", "l", "s", "b", local_mtime=200, server_mtime=100) == "upload"
    assert decide("both", "l", "s", "b", local_mtime=100, server_mtime=200) == "download"
    assert decide("both", "l", "s", "b", local_mtime=100, server_mtime=100) == "download"
    assert decide("both", "l", "s", None, local_mtime=200, server_mtime=100) == "upload"      # sama-sama baru


# --- terima saja ------------------------------------------------------------------------------------
def test_receive_downloads_creates_folder_and_keeps_mtime(tmp_path, server):
    server.put("materi/bab1.txt", b"isi bab satu", mtime=PAST + 5)
    pc = PC(tmp_path, server, "receive")
    assert not pc.folder.exists()
    result = pc.sync()
    assert pc.folder.is_dir() and pc.read("materi/bab1.txt") == b"isi bab satu"
    assert (result.downloaded, result.uploaded, result.errors) == (1, 0, [])
    assert (pc.folder / "materi" / "bab1.txt").stat().st_mtime == PAST + 5
    assert not list(pc.folder.rglob("*.labtmp"))


def test_receive_ignores_local_changes_and_deletions(tmp_path, server):
    server.put("a.txt", b"asli")
    pc = PC(tmp_path, server, "receive")
    pc.sync()
    pc.write("a.txt", b"diubah siswa", mtime=PAST + 50)
    pc.write("punya-siswa.txt", b"pribadi")
    pc.sync()
    assert pc.read("a.txt") == b"asli"                           # perubahan lokal dikembalikan
    assert pc.read("punya-siswa.txt") == b"pribadi"              # file yang tak dikenal server dibiarkan
    pc.delete("a.txt")
    pc.sync()
    assert pc.read("a.txt") == b"asli"                           # menghapus tidak berefek: dipulihkan
    assert server.files["a.txt"]["data"] == b"asli" and "punya-siswa.txt" not in server.files
    assert [c for c in server.calls if c[0] in ("upload", "delete")] == []


def test_receive_follows_server_updates_and_deletes(tmp_path, server):
    server.put("a.txt", b"v1")
    server.put("b.txt", b"b")
    pc = PC(tmp_path, server, "receive")
    pc.sync()
    server.put("a.txt", b"v2", mtime=PAST + 9)
    del server.files["b.txt"]
    pc.sync()
    assert pc.read("a.txt") == b"v2" and pc.read("b.txt") is None


# --- kirim saja --------------------------------------------------------------------------------------
def test_send_uploads_new_and_changed_files_and_propagates_deletes(tmp_path, server):
    pc = PC(tmp_path, server, "send")
    pc.write("tugas/a.txt", b"jawaban")
    assert pc.sync().uploaded == 1 and server.files["tugas/a.txt"]["data"] == b"jawaban"
    assert server.files["tugas/a.txt"]["mtime"] == PAST
    pc.write("tugas/a.txt", b"jawaban revisi", mtime=PAST + 30)
    pc.sync()
    assert server.files["tugas/a.txt"]["data"] == b"jawaban revisi"
    pc.delete("tugas/a.txt")
    result = pc.sync()
    assert result.deleted_server == 1 and "tugas/a.txt" not in server.files


def test_send_never_receives_or_deletes_what_it_did_not_send(tmp_path, server):
    server.put("dari-guru.txt", b"materi")
    pc = PC(tmp_path, server, "send")
    pc.write("saya.txt", b"x")
    pc.sync()
    assert pc.read("dari-guru.txt") is None and "dari-guru.txt" in server.files
    server.put("saya.txt", b"diubah komputer lain", mtime=PAST + 99)
    pc.sync()
    assert server.files["saya.txt"]["data"] == b"diubah komputer lain"        # tidak menimpa, tidak menerima
    assert pc.read("saya.txt") == b"x"


# --- dua arah ----------------------------------------------------------------------------------------
def test_both_syncs_in_both_directions_and_propagates_deletes(tmp_path, server):
    server.put("dari-server.txt", b"s")
    pc = PC(tmp_path, server, "both")
    pc.write("dari-pc.txt", b"p")
    pc.sync()
    assert pc.read("dari-server.txt") == b"s" and server.files["dari-pc.txt"]["data"] == b"p"
    pc.write("dari-server.txt", b"s2", mtime=PAST + 10)                         # ubah di PC -> naik
    server.put("dari-pc.txt", b"p2", mtime=PAST + 10)                           # ubah di server -> turun
    pc.sync()
    assert server.files["dari-server.txt"]["data"] == b"s2" and pc.read("dari-pc.txt") == b"p2"
    pc.delete("dari-server.txt")                                                # hapus di PC -> hapus di server
    del server.files["dari-pc.txt"]                                             # hapus di server -> hapus di PC
    pc.sync()
    assert "dari-server.txt" not in server.files and pc.read("dari-pc.txt") is None


def test_both_conflict_newest_wins_and_change_beats_delete(tmp_path, server):
    server.put("a.txt", b"awal")
    server.put("b.txt", b"awal")
    server.put("c.txt", b"awal")
    pc = PC(tmp_path, server, "both")
    pc.sync()
    pc.write("a.txt", b"pc lebih baru", mtime=PAST + 200)
    server.put("a.txt", b"server lebih lama", mtime=PAST + 100)
    pc.write("b.txt", b"pc lebih lama", mtime=PAST + 100)
    server.put("b.txt", b"server lebih baru", mtime=PAST + 200)
    pc.delete("c.txt")
    server.put("c.txt", b"server mengubah", mtime=PAST + 5)                     # diubah di server selagi dihapus di PC
    pc.sync()
    assert server.files["a.txt"]["data"] == b"pc lebih baru"
    assert pc.read("b.txt") == b"server lebih baru"
    assert pc.read("c.txt") == b"server mengubah"


def test_three_computers_with_different_rules_share_one_server(tmp_path, server):
    """Contoh dari permintaan: A dua arah, B menerima saja, C mengirim saja."""
    a, b, c = (PC(tmp_path, server, m, n) for m, n in (("both", "A"), ("receive", "B"), ("send", "C")))
    a.write("dari-a.txt", b"A")
    c.write("dari-c.txt", b"C")
    for pc in (a, c, a, b):
        pc.sync()
    assert sorted(server.files) == ["dari-a.txt", "dari-c.txt"]
    assert a.files() == ["dari-a.txt", "dari-c.txt"] and b.files() == ["dari-a.txt", "dari-c.txt"]
    assert c.files() == ["dari-c.txt"]                                           # C tidak menerima apa pun
    b.delete("dari-a.txt")                                                       # B menerima saja: tidak berefek
    b.sync()
    assert b.read("dari-a.txt") == b"A" and "dari-a.txt" in server.files
    c.delete("dari-c.txt")                                                       # C mengirim: menghapus di server
    c.sync()
    a.sync(); b.sync()
    assert "dari-c.txt" not in server.files and a.files() == ["dari-a.txt"] and b.files() == ["dari-a.txt"]
    a.delete("dari-a.txt")                                                       # A dua arah: menghapus untuk semua
    a.sync(); b.sync()
    assert server.files == {} and b.files() == []


# --- keselamatan ----------------------------------------------------------------------------------------
def test_missing_folder_is_restored_not_treated_as_mass_delete(tmp_path, server):
    for i in range(8):
        server.put(f"f{i}.txt", b"x%d" % i)
    pc = PC(tmp_path, server, "both")
    pc.sync()
    import shutil
    shutil.rmtree(pc.folder)                                                     # seluruh folder lenyap
    result = pc.sync()
    assert len(server.files) == 8 and len(pc.files()) == 8 and result.deleted_server == 0


def test_mass_delete_to_server_is_held(tmp_path, server):
    for i in range(10):
        server.put(f"f{i}.txt", b"x%d" % i)
    pc = PC(tmp_path, server, "both")
    pc.sync()
    for i in range(10):
        pc.delete(f"f{i}.txt")
    result = pc.sync()
    assert result.held_deletes == 10 and len(server.files) == 10 and any("ditahan" in e for e in result.errors)
    pc2 = PC(tmp_path, server, "both", "pc2")
    pc2.sync()
    pc2.delete("f0.txt"); pc2.delete("f1.txt")                                   # sedikit: diteruskan
    assert pc2.sync().deleted_server == 2 and len(server.files) == 8


def test_server_side_mass_delete_is_followed_by_receivers(tmp_path, server):
    for i in range(10):
        server.put(f"f{i}.txt", b"x")
    pc = PC(tmp_path, server, "receive")
    pc.sync()
    server.files.clear()                                                         # admin mengosongkan folder pusat
    assert pc.sync().deleted_local == 10 and pc.files() == []


def test_recently_modified_files_are_not_touched_yet(tmp_path, server):
    pc = PC(tmp_path, server, "send")
    pc.write("baru.txt", b"masih ditulis", mtime=NOW - 0.5)
    pc.write("lama.txt", b"selesai")
    pc.sync()
    assert list(server.files) == ["lama.txt"]
    FolderSync(pc.state, clock=lambda: NOW + 10).run(pc.folder, "send", server.client())
    assert sorted(server.files) == ["baru.txt", "lama.txt"]


def test_temp_and_system_files_never_sync(tmp_path, server):
    pc = PC(tmp_path, server, "send")
    for name in ("desktop.ini", "Thumbs.db", "~$dok.docx", "unduhan.labtmp", "bagus.txt"):
        pc.write(name, b"x")
    pc.sync()
    assert list(server.files) == ["bagus.txt"]


def test_unsafe_paths_from_server_are_ignored(tmp_path, server):
    for bad in ("../luar.txt", "a/../../luar.txt", "/etc/passwd", "a\\b.txt", "con.txt", "x" * 150):
        server.put(bad, b"jahat")
    server.put("baik.txt", b"ok")
    pc = PC(tmp_path, server, "receive")
    result = pc.sync()
    assert pc.files() == ["baik.txt"] and result.errors == []
    assert not (tmp_path / "luar.txt").exists() and not (pc.root / "luar.txt").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="symlink butuh hak khusus di Windows")
def test_symlinks_are_not_followed(tmp_path, server):
    secret = tmp_path / "rahasia"
    secret.mkdir()
    (secret / "kunci.txt").write_text("jangan dikirim")
    pc = PC(tmp_path, server, "send")
    pc.folder.mkdir(parents=True)
    (pc.folder / "tautan").symlink_to(secret, target_is_directory=True)
    (pc.folder / "tautan-file.txt").symlink_to(secret / "kunci.txt")
    pc.sync()
    assert server.files == {}


def test_corrupt_download_is_discarded(tmp_path, server):
    server.put("a.txt", b"isi")

    class Corrupt(FakeClient):
        def download(self, path):
            yield b"rusak!"
    pc = PC(tmp_path, server, "receive")
    result = FolderSync(pc.state, clock=lambda: NOW).run(pc.folder, "receive", Corrupt(server))
    assert pc.files() == [] and not list(pc.folder.rglob("*.labtmp")) and any("SHA-256" in e for e in result.errors)
    assert pc.sync().downloaded == 1 and pc.read("a.txt") == b"isi"             # putaran berikut berhasil


def test_conflict_from_server_is_retried_next_round(tmp_path, server):
    pc = PC(tmp_path, server, "send")
    pc.write("a.txt", b"v1")

    class Racing(FakeClient):
        def upload(self, path, file_path, mtime, expect):
            self.s.put(path, b"orang lain", mtime=PAST + 1)                      # berubah tepat sebelum unggah
            return super().upload(path, file_path, mtime, expect)
    result = FolderSync(pc.state, clock=lambda: NOW).run(pc.folder, "send", Racing(server))
    assert result.uploaded == 0 and result.errors == []
    pc.write("a.txt", b"v2", mtime=PAST + 7)                                      # edit lagi, lalu server normal
    assert pc.sync().uploaded == 1 and server.files["a.txt"]["data"] == b"v2"


def test_state_survives_restart_and_folder_change_resets_it(tmp_path, server):
    server.put("a.txt", b"isi")
    pc = PC(tmp_path, server, "both")
    pc.sync()
    assert pc.sync().changed == 0                                                 # instans baru, status dibaca dari berkas
    pc.delete("a.txt")
    other = PC(tmp_path, server, "both", "lain")
    other.state = pc.state                                                        # folder berbeda dengan status yang sama
    other.sync()
    assert "a.txt" in server.files and other.read("a.txt") == b"isi"             # tidak dianggap dihapus


def test_no_file_count_or_size_limits_on_the_agent(tmp_path, server):
    pc = PC(tmp_path, server, "send")
    for i in range(60):
        pc.write(f"f{i}.txt", b"x%d" % i)
    pc.write("besar.bin", b"B" * (5 * 1024 * 1024))
    result = pc.sync()
    assert result.errors == [] and len(server.files) == 61
    assert not hasattr(sharedsync, "MAX_FILE_BYTES") and not hasattr(sharedsync, "MAX_FILES")


@pytest.mark.skipif(sys.platform == "win32", reason="chmod tidak berarti di Windows")
def test_unreadable_subfolder_never_deletes_its_files_on_the_server(tmp_path, server):
    import os as _os
    if _os.geteuid() == 0:
        pytest.skip("root bisa membaca semuanya")
    server.put("rahasia/a.txt", b"1"); server.put("rahasia/b.txt", b"2"); server.put("biasa.txt", b"3")
    pc = PC(tmp_path, server, "both")
    pc.sync()
    (pc.folder / "rahasia").chmod(0)
    try:
        result = pc.sync()
    finally:
        (pc.folder / "rahasia").chmod(0o755)
    assert sorted(server.files) == ["biasa.txt", "rahasia/a.txt", "rahasia/b.txt"] and result.deleted_server == 0
    assert result.errors and "tidak bisa membaca" in result.errors[0]


def test_invalid_mode_and_uncreatable_folder(tmp_path, server):
    assert PC(tmp_path, server, "terserah").sync().errors == ["aturan tidak dikenal"]
    blocker = tmp_path / "blok"
    blocker.write_text("saya file")
    result = FolderSync(tmp_path / "s.json").run(blocker / "x", "receive", server.client())
    assert result.errors and "folder" in result.errors[0]


# --- nama, folder, alamat -------------------------------------------------------------------------------------
def test_resolve_folder_stays_inside_profile(tmp_path):
    assert sharedsync.resolve_folder(tmp_path, "Desktop/shared") == tmp_path / "Desktop" / "shared"
    for bad in ("", "../x", "C:/x", "a/../b", None):
        assert sharedsync.resolve_folder(tmp_path, bad) is None, bad
    if sys.platform != "win32":
        (tmp_path / "keluar").symlink_to("/tmp")
        assert sharedsync.resolve_folder(tmp_path, "keluar/shared") is None


def test_is_local_url():
    class Resolver:
        def __init__(self, *ips): self.ips = ips
        def __call__(self, host, port): return [(0, 0, 0, "", (ip, 0)) for ip in self.ips]
    local = sharedsync.is_local_url
    assert local("http://192.168.1.17:8080") and local("http://10.0.0.5") and local("http://127.0.0.1:8080")
    assert not local("http://8.8.8.8") and not local("https://1.1.1.1/x")
    assert local("http://lab-server:8080", Resolver("192.168.1.17"))
    assert not local("https://lab.fog.tf", Resolver("104.21.1.1", "172.67.1.1"))
    assert not local("https://campur.example", Resolver("192.168.1.2", "8.8.4.4"))
    assert not local("https://tidak-ada.example", lambda *a: (_ for _ in ()).throw(OSError("dns")))
    assert not local("bukan url")


@pytest.mark.parametrize("mode", ["receive", "both"])
def test_deleting_the_whole_shared_folder_only_recreates_and_redownloads(tmp_path, server, mode):
    """Folder bersama dihapus di satu komputer: dianggap belum punya folder; dibuat ulang dan file diunduh lagi.
    Server dan komputer lain tidak kehilangan apa pun."""
    import shutil
    for i in range(4):
        server.put(f"materi/bab{i}.txt", b"isi %d" % i)
    server.put("tugas/a.txt", b"tugas")
    pc, other = PC(tmp_path, server, mode), PC(tmp_path, server, "both", "lain")
    pc.sync(); other.sync()
    shutil.rmtree(pc.folder)
    result = pc.sync()
    assert (result.deleted_server, result.deleted_local, result.held_deletes) == (0, 0, 0) and result.errors == []
    assert pc.files() == sorted(server.files) and len(server.files) == 5      # dibuat ulang dan diunduh lagi
    other.sync()
    assert other.files() == sorted(server.files)                             # komputer lain tidak terpengaruh
    assert [c for c in server.calls if c[0] == "delete"] == []


def test_deleting_the_shared_folder_in_send_mode_deletes_nothing(tmp_path, server):
    import shutil
    pc = PC(tmp_path, server, "send")
    for i in range(6):
        pc.write(f"f{i}.txt", b"x%d" % i)
    pc.sync()
    assert len(server.files) == 6
    shutil.rmtree(pc.folder)
    result = pc.sync()
    assert pc.folder.is_dir() and len(server.files) == 6 and result.deleted_server == 0     # kirim saja: tidak mengunduh, tidak menghapus
    pc.write("baru.txt", b"lagi")
    pc.sync()
    assert "baru.txt" in server.files and len(server.files) == 7


def test_deleting_one_subfolder_is_still_a_normal_delete(tmp_path, server):
    """Hanya folder bersama itu sendiri yang diperlakukan khusus; menghapus subfolder tetap menghapus filenya."""
    import shutil
    server.put("a/1.txt", b"1"); server.put("a/2.txt", b"2"); server.put("b/3.txt", b"3")
    pc = PC(tmp_path, server, "both")
    pc.sync()
    shutil.rmtree(pc.folder / "a")
    pc.sync()
    assert sorted(server.files) == ["b/3.txt"]
