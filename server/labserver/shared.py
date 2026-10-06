"""Folder bersama: satu folder pusat di server yang disinkronkan ke folder lokal agent, hanya lewat LAN.

Server menyimpan file di `<folder db>/shared/files/` dan daftarnya (SHA-256, ukuran, waktu ubah) di tabel
`shared_files`. Aturan sinkron (terima/kirim/dua arah) diputuskan agent; server menegakkan izin per komputer dan
memakai `expect` (SHA-256 yang diharapkan agent) supaya dua agent tidak saling menimpa tanpa sadar.
"""
import os
import re
import secrets
import shutil
import threading
from pathlib import Path

from .release import sha_of_stream

MODES = ("off", "receive", "send", "both")
MODE_LABELS = {"off": "Mati", "receive": "Terima saja", "send": "Kirim saja", "both": "Dua arah"}
SYNC_SECONDS = 10
MIN_FREE_BYTES = 2 * 1024 ** 3       # bukan batas file: unggahan ditolak hanya bila ruang disk server tinggal sedikit
MAX_DEPTH = 8
MAX_PATH_CHARS = 200
MAX_PART_CHARS = 120

_BAD_CHARS = re.compile(r'[<>:"|?*\\\x00-\x1f]')
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
_IGNORED_NAMES = {"desktop.ini", "thumbs.db"}


def _clean_parts(parts, allow_ignored=False):
    if not parts or len(parts) > MAX_DEPTH:
        return None
    for part in parts:
        if not part or part in (".", "..") or len(part) > MAX_PART_CHARS or _BAD_CHARS.search(part):
            return None
        if part.endswith((" ", ".")) or part.split(".")[0].lower() in _RESERVED:
            return None
    return parts


def clean_path(raw):
    """Path file relatif gaya POSIX yang aman di Linux dan Windows, atau None. Nama sementara/sistem ditolak."""
    if not isinstance(raw, str) or not raw or len(raw) > MAX_PATH_CHARS or raw.startswith("/") or raw.endswith("/"):
        return None
    parts = _clean_parts(raw.split("/"))
    if parts is None:
        return None
    name = parts[-1].lower()
    if name in _IGNORED_NAMES or name.endswith(".labtmp") or name.startswith("~$"):
        return None
    return "/".join(parts)


def clean_profile_path(raw):
    """Folder tujuan di profil user agent (mis. `Desktop/shared`); boleh memakai `\\`. '' = fitur dimatikan."""
    if not isinstance(raw, str):
        return None
    text = raw.strip().replace("\\", "/").strip("/")
    if not text:
        return ""
    if len(text) > MAX_PATH_CHARS or re.match(r"^[A-Za-z]:", text):
        return None
    parts = _clean_parts(text.split("/"))
    return "/".join(parts) if parts else None


class SharedFiles:
    def __init__(self, root: Path, store):
        self.root, self.store = Path(root), store
        self.files_dir, self.tmp_dir = self.root / "files", self.root / ".tmp"
        self._lock = threading.Lock()

    def index(self) -> dict:
        return self.store.shared_index()

    def disk_free(self) -> int:
        self.root.mkdir(parents=True, exist_ok=True)
        return shutil.disk_usage(self.root).free

    def total_size(self) -> int:
        return sum(entry["size"] for entry in self.index().values())

    def file_path(self, path: str) -> Path | None:
        target = self.files_dir / path
        return target if target.is_file() and self.files_dir.resolve() in target.resolve().parents else None

    @staticmethod
    def _clash(index: dict, path: str):
        low = path.lower()
        for other in index:
            o = other.lower()
            if other != path and (o == low or low.startswith(o + "/") or o.startswith(low + "/")):
                return other
        return None

    def put(self, path: str, stream, mtime: float, expect, now: float):
        """Mengembalikan (status, entry). status: ok | conflict | name_clash | disk_full. Tanpa batas ukuran/jumlah file."""
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.tmp_dir / f"up-{secrets.token_hex(6)}"
        try:
            sha, size = sha_of_stream(stream, tmp, None)
            if self.disk_free() - size < MIN_FREE_BYTES:
                return "disk_full", None
            with self._lock:
                index = self.store.shared_index()
                current = index.get(path)
                if expect is not None and (current["sha256"] if current else "none") != expect:
                    return "conflict", None
                if self._clash(index, path):
                    return "name_clash", None
                target = self.files_dir / path
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(tmp, target)
                self.store.shared_set(path, sha, size, mtime, now)
                return "ok", {"sha256": sha, "size": size, "mtime": mtime}
        finally:
            tmp.unlink(missing_ok=True)

    def delete(self, path: str, expect):
        """status: ok | missing | conflict."""
        with self._lock:
            current = self.store.shared_index().get(path)
            if current is None:
                return "missing"
            if expect is not None and current["sha256"] != expect:
                return "conflict"
            (self.files_dir / path).unlink(missing_ok=True)
            self.store.shared_remove(path)
            parent = (self.files_dir / path).parent
            while parent != self.files_dir and parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
                parent = parent.parent
            return "ok"
