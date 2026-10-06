"""Unggah berkas besar per potongan dan bisa dilanjutkan (dari dashboard).

Satu permintaan panjang gampang putus di jalur internet yang lambat (Cloudflare memutus permintaan yang terlalu lama).
Di sini browser mengirim potongan kecil berurutan; bila ada yang gagal, ia menanyakan berapa byte yang sudah diterima
server dan melanjutkan dari situ. Potongan sementara disimpan di `<folder db>/uploads/` dan dibuang setelah 24 jam.
"""
import json
import re
import secrets
import threading
from pathlib import Path

CHUNK_MAX_BYTES = 4 * 1024 * 1024
TTL_SECONDS = 24 * 3600
MAX_SESSIONS = 20
ID_RE = re.compile(r"^[0-9a-f]{16}$")


class Uploads:
    def __init__(self, root):
        self.root = Path(root)
        self._master = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    def _lock(self, uid):
        with self._master:
            return self._locks.setdefault(uid, threading.Lock())

    def part_path(self, uid) -> Path:
        return self.root / f"{uid}.part"

    def _meta_path(self, uid) -> Path:
        return self.root / f"{uid}.json"

    def create(self, meta: dict, now: float):
        """Sesi baru. None bila terlalu banyak sesi aktif."""
        self.root.mkdir(parents=True, exist_ok=True)
        self.prune(now)
        if len(list(self.root.glob("*.json"))) >= MAX_SESSIONS:
            return None
        uid = secrets.token_hex(8)
        self.part_path(uid).write_bytes(b"")
        self._meta_path(uid).write_text(json.dumps({**meta, "created": now}))
        return uid

    def meta(self, uid):
        if not isinstance(uid, str) or not ID_RE.match(uid):
            return None
        try:
            return json.loads(self._meta_path(uid).read_text())
        except (OSError, ValueError):
            return None

    def received(self, uid) -> int:
        try:
            return self.part_path(uid).stat().st_size
        except OSError:
            return 0

    def append(self, uid, offset: int, stream, size: int):
        """Menambah potongan di `offset`. Mengembalikan (status, diterima): ok | offset (tidak sesuai) | too_big."""
        with self._lock(uid):
            have = self.received(uid)
            if offset != have:
                return "offset", have
            written = 0
            with open(self.part_path(uid), "ab") as out:
                for chunk in iter(lambda: stream.read(1 << 20), b""):
                    written += len(chunk)
                    if have + written > size:
                        out.truncate(have)
                        return "too_big", have
                    out.write(chunk)
            return "ok", have + written

    def remove(self, uid):
        for path in (self.part_path(uid), self._meta_path(uid)):
            path.unlink(missing_ok=True)
        with self._master:
            self._locks.pop(uid, None)

    def prune(self, now: float):
        for meta_file in self.root.glob("*.json"):
            try:
                created = json.loads(meta_file.read_text()).get("created", 0)
            except (OSError, ValueError):
                created = 0
            if now - created > TTL_SECONDS:
                self.remove(meta_file.stem)
        for part in self.root.glob("*.part"):
            if not self._meta_path(part.stem).exists():
                part.unlink(missing_ok=True)
