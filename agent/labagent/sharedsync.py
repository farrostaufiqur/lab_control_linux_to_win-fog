"""Sinkron folder bersama di sisi agent (logika murni; HTTP dan disk disuntikkan/ditest di Linux).

Server menyimpan satu folder pusat. Tiap komputer punya aturan sendiri:
  receive : mengikuti server. Hapus/ubah lokal tidak berpengaruh ke server (file dikembalikan).
  send    : perubahan dan hapus lokal dikirim ke server; perubahan di server tidak diterima.
  both    : dua arah; bila dua sisi mengubah file yang sama, waktu ubah terbaru menang.

Agar tahu "siapa yang berubah", agent menyimpan `base`: SHA-256 tiap file pada sinkron terakhir yang berhasil.
Dari L (lokal), S (server), B (base) diputuskan satu tindakan per file. Penghapusan tidak butuh catatan di server:
file yang ada di base tetapi hilang di satu sisi berarti dihapus di sisi itu.
"""
import hashlib
import ipaddress
import json
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .nethttp import agent_headers

MODES = ("receive", "send", "both")
MAX_FILE_BYTES = 100 * 1024 * 1024
MAX_FILES = 2000
MAX_DEPTH = 8
MAX_PATH_CHARS = 200
MAX_PART_CHARS = 120
SETTLE_SECONDS = 2.0                # file yang baru diubah dianggap masih ditulis: ditunda
DELETE_GUARD_MIN = 5                # penghapusan ke server yang melebihi ini DAN separuh file ditahan (jaga kecelakaan)
DELETE_GUARD_RATIO = 0.5
TMP_SUFFIX = ".labtmp"

_BAD_CHARS = re.compile(r'[<>:"|?*\\\x00-\x1f]')
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
_IGNORED_NAMES = {"desktop.ini", "thumbs.db"}


# --- nama dan folder (aturan sama dengan server/labserver/shared.py, dicek tes) --------------
def _clean_parts(parts):
    if not parts or len(parts) > MAX_DEPTH:
        return None
    for part in parts:
        if not part or part in (".", "..") or len(part) > MAX_PART_CHARS or _BAD_CHARS.search(part):
            return None
        if part.endswith((" ", ".")) or part.split(".")[0].lower() in _RESERVED:
            return None
    return parts


def clean_path(raw):
    if not isinstance(raw, str) or not raw or len(raw) > MAX_PATH_CHARS or raw.startswith("/") or raw.endswith("/"):
        return None
    parts = _clean_parts(raw.split("/"))
    if parts is None:
        return None
    name = parts[-1].lower()
    if name in _IGNORED_NAMES or name.endswith(TMP_SUFFIX) or name.startswith("~$"):
        return None
    return "/".join(parts)


def clean_profile_path(raw):
    if not isinstance(raw, str):
        return None
    text = raw.strip().replace("\\", "/").strip("/")
    if not text:
        return ""
    if len(text) > MAX_PATH_CHARS or re.match(r"^[A-Za-z]:", text):
        return None
    parts = _clean_parts(text.split("/"))
    return "/".join(parts) if parts else None


def resolve_folder(profile, relative):
    """Folder tujuan di dalam profil user, atau None (kosong, tidak aman, atau keluar dari profil lewat tautan)."""
    rel = clean_profile_path(relative)
    if not rel:
        return None
    profile = Path(profile)
    folder = profile / rel
    try:
        if profile.resolve() not in folder.resolve().parents:
            return None
    except OSError:
        return None
    return folder


def safe_target(folder: Path, path: str) -> Path:
    target = folder / path
    if folder.resolve() not in target.resolve().parents:
        raise ValueError("path keluar dari folder bersama")
    return target


def is_local_url(url, resolver=socket.getaddrinfo) -> bool:
    """True bila alamat server ada di jaringan lokal (IP privat/loopback/link-local). Hostname di-resolve."""
    host = urllib.parse.urlparse(url).hostname or ""
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            addresses = [ipaddress.ip_address(info[4][0]) for info in resolver(host, None)]
        except (OSError, ValueError):
            return False
    return bool(addresses) and all(a.is_private or a.is_loopback or a.is_link_local for a in addresses)


# --- keputusan ------------------------------------------------------------------------------------
def decide(mode, L, S, B, local_mtime=0.0, server_mtime=0.0):
    """Satu tindakan untuk satu file. L/S/B = SHA-256 lokal/server/base (None = tidak ada).

    Hasil: 'sync' (sudah sama, catat base), 'download', 'upload', 'delete_local', 'delete_server', atau 'noop'.
    """
    if L == S:
        return "sync"
    if mode == "receive":
        if S is not None:
            return "download"
        return "delete_local" if B is not None else "noop"          # server menghapus file yang pernah diterima
    if mode == "send":
        if L is not None:
            return "upload" if L != B else "noop"                    # hanya yang berubah di sini yang dikirim
        return "delete_server" if B is not None and S is not None else ("sync" if B is not None else "noop")
    # dua arah
    if L is not None and S is not None:
        if L == B:
            return "download"
        if S == B:
            return "upload"
        return "upload" if local_mtime > server_mtime else "download"   # sama-sama berubah: terbaru menang, seri = server
    if L is not None:                                                  # server tidak punya
        if B is None or L != B:
            return "upload"                                            # file baru, atau diubah selagi server menghapus
        return "delete_local"
    if B is None or S != B:
        return "download"                                              # baru di server, atau diubah selagi lokal menghapus
    return "delete_server"


# --- klien HTTP -------------------------------------------------------------------------------------
class Conflict(Exception):
    """Server menolak karena file sudah berubah (HTTP 409); dicoba lagi pada putaran berikutnya."""


class HttpSharedClient:
    def __init__(self, base_url, token, host, timeout=30.0, transfer_timeout=120.0):
        self.base, self.token, self.host = base_url.rstrip("/"), token, host
        self.timeout, self.transfer_timeout = timeout, transfer_timeout

    def _url(self, endpoint, **query):
        query = {k: v for k, v in {"host": self.host, **query}.items() if v is not None}
        return f"{self.base}/api/agent/shared/{endpoint}?{urllib.parse.urlencode(query)}"

    def _open(self, request, timeout):
        try:
            return urllib.request.urlopen(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            if exc.code == 409:
                raise Conflict() from exc
            raise

    def index(self):
        request = urllib.request.Request(self._url("index"), headers=agent_headers(self.token))
        with self._open(request, self.timeout) as response:
            return json.loads(response.read())["files"]

    def download(self, path):
        request = urllib.request.Request(self._url("file", path=path), headers=agent_headers(self.token))
        with self._open(request, self.transfer_timeout) as response:
            while chunk := response.read(1 << 16):
                yield chunk

    def upload(self, path, file_path, mtime, expect):
        size = os.path.getsize(file_path)
        headers = {**agent_headers(self.token, "application/octet-stream"), "Content-Length": str(size)}
        with open(file_path, "rb") as body:
            request = urllib.request.Request(self._url("file", path=path, mtime=f"{mtime:.3f}", expect=expect),
                                             data=body, headers=headers, method="PUT")
            with self._open(request, self.transfer_timeout) as response:
                return json.loads(response.read())

    def delete(self, path, expect):
        request = urllib.request.Request(self._url("file", path=path, expect=expect),
                                         headers=agent_headers(self.token), method="DELETE")
        with self._open(request, self.timeout) as response:
            response.read()


# --- sinkron ------------------------------------------------------------------------------------------
@dataclass
class Result:
    files: int = 0
    downloaded: int = 0
    uploaded: int = 0
    deleted_local: int = 0
    deleted_server: int = 0
    held_deletes: int = 0
    errors: list = field(default_factory=list)

    @property
    def changed(self):
        return self.downloaded + self.uploaded + self.deleted_local + self.deleted_server

    def summary(self):
        parts = [(self.downloaded, "diunduh"), (self.uploaded, "diunggah"),
                 (self.deleted_local, "dihapus lokal"), (self.deleted_server, "dihapus di server")]
        return ", ".join(f"{n} {label}" for n, label in parts if n)


def file_sha256(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_reparse(st) -> bool:
    return bool(getattr(st, "st_file_attributes", 0) & 0x400)      # junction/symlink Windows


class FolderSync:
    def __init__(self, state_file, clock=time.time):
        self.state_file, self._clock = Path(state_file), clock

    # status tersimpan ----------------------------------------------------------------------
    def _load(self):
        try:
            raw = json.loads(self.state_file.read_text(encoding="utf-8"))
            return str(raw["folder"]), dict(raw["base"]), {k: tuple(v) for k, v in raw["cache"].items()}
        except (OSError, ValueError, KeyError, TypeError):
            return "", {}, {}

    def _save(self, folder, base, cache):
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps({"folder": str(folder), "base": base, "cache": cache}), encoding="utf-8")
            os.replace(tmp, self.state_file)
        except OSError:
            pass

    # pemindaian lokal -------------------------------------------------------------------------
    def _scan(self, folder: Path, cache: dict, now: float):
        local, settling, new_cache, errors = {}, set(), {}, []
        stack = [(folder, "")]
        while stack:
            directory, prefix = stack.pop()
            try:
                entries = list(os.scandir(directory))
            except OSError as exc:
                errors.append(f"tidak bisa membaca {prefix or '.'}: {exc.strerror or exc}")
                continue
            for entry in entries:
                rel = prefix + entry.name
                try:
                    if entry.is_symlink():
                        continue
                    st = entry.stat(follow_symlinks=False)
                    if _is_reparse(st):
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        stack.append((Path(entry.path), rel + "/"))
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    path = clean_path(rel)
                    if path is None or st.st_size > MAX_FILE_BYTES:
                        continue
                    if len(local) + len(settling) >= MAX_FILES:
                        errors.append(f"lebih dari {MAX_FILES} file; sinkron dihentikan")
                        return {}, set(), cache, errors
                    if now - st.st_mtime < SETTLE_SECONDS:
                        settling.add(path)
                        if path in cache:
                            new_cache[path] = cache[path]
                        continue
                    key = cache.get(path)
                    if key and key[0] == st.st_size and key[1] == st.st_mtime_ns:
                        sha = key[2]
                    else:
                        sha = file_sha256(entry.path)
                    new_cache[path] = (st.st_size, st.st_mtime_ns, sha)
                    local[path] = {"sha": sha, "mtime": st.st_mtime}
                except OSError:
                    settling.add(clean_path(rel) or rel)        # terkunci aplikasi lain: coba lagi nanti
        return local, settling, new_cache, errors

    # aksi file -------------------------------------------------------------------------------------
    def _download(self, folder, path, entry, client, cache):
        dest = safe_target(folder, path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.is_dir():
            raise OSError("ada folder dengan nama yang sama")
        tmp = dest.with_name(dest.name + TMP_SUFFIX)
        digest, size = hashlib.sha256(), 0
        try:
            with open(tmp, "wb") as out:
                for chunk in client.download(path):
                    size += len(chunk)
                    if size > MAX_FILE_BYTES:
                        raise OSError("file terlalu besar")
                    digest.update(chunk)
                    out.write(chunk)
            if digest.hexdigest() != entry["sha256"]:
                raise OSError("unduhan rusak (SHA-256 berbeda)")
            os.utime(tmp, (entry["mtime"], entry["mtime"]))
            os.replace(tmp, dest)
        finally:
            tmp.unlink(missing_ok=True)
        st = dest.stat()
        cache[path] = (st.st_size, st.st_mtime_ns, entry["sha256"])

    def _remove_local(self, folder, path, cache):
        target = safe_target(folder, path)
        target.unlink(missing_ok=True)
        cache.pop(path, None)
        parent = target.parent
        while parent != folder and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent

    # satu putaran ----------------------------------------------------------------------------------
    def run(self, folder, mode, client) -> Result:
        folder, result = Path(folder), Result()
        if mode not in MODES:
            result.errors.append("aturan tidak dikenal")
            return result
        saved_folder, base, cache = self._load()
        existed = folder.is_dir()
        if not existed or saved_folder != str(folder):
            base, cache = {}, {}          # folder baru/hilang: jangan menganggap file yang tak ada sebagai "dihapus"
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            result.errors.append(f"tidak bisa membuat folder: {exc.strerror or exc}")
            return result

        local, settling, cache, scan_errors = self._scan(folder, cache, self._clock())
        result.errors.extend(scan_errors)
        if scan_errors and not local and not settling and any("lebih dari" in e for e in scan_errors):
            self._save(folder, base, {})
            return result
        server = {p: e for p, e in client.index().items() if clean_path(p) == p and e.get("size", 0) <= MAX_FILE_BYTES}

        plan = []
        for path in sorted((set(local) | set(server) | set(base)) - settling):
            L = local[path]["sha"] if path in local else None
            S = server[path]["sha256"] if path in server else None
            action = decide(mode, L, S, base.get(path),
                            local[path]["mtime"] if L else 0.0, server[path]["mtime"] if S else 0.0)
            plan.append((path, action, L, S))
        deletes = [p for p in plan if p[1] == "delete_server"]
        if len(deletes) > max(DELETE_GUARD_MIN, DELETE_GUARD_RATIO * len(base)):
            result.held_deletes = len(deletes)
            result.errors.append(f"{len(deletes)} penghapusan ke server ditahan (terlalu banyak sekaligus); "
                                 "periksa folder lalu coba lagi")
            plan = [p for p in plan if p[1] != "delete_server"]

        for path, action, L, S in plan:
            try:
                if action == "sync":
                    if L is None:
                        base.pop(path, None)
                    else:
                        base[path] = L
                elif action == "noop":
                    continue
                elif action == "download":
                    self._download(folder, path, server[path], client, cache)
                    base[path] = S
                    result.downloaded += 1
                elif action == "upload":
                    reply = client.upload(path, safe_target(folder, path), local[path]["mtime"], S or "none")
                    base[path] = reply.get("sha256", L)
                    result.uploaded += 1
                elif action == "delete_local":
                    self._remove_local(folder, path, cache)
                    base.pop(path, None)
                    result.deleted_local += 1
                elif action == "delete_server":
                    client.delete(path, S)
                    base.pop(path, None)
                    cache.pop(path, None)
                    result.deleted_server += 1
            except Conflict:
                continue                                    # file berubah di server: dinilai ulang putaran berikutnya
            except (OSError, ValueError, urllib.error.URLError) as exc:
                result.errors.append(f"{path}: {getattr(exc, 'strerror', None) or exc}")
        result.files = sum(1 for p in set(local) | set(server) if p not in settling)
        self._save(folder, base, {k: list(v) for k, v in cache.items()})
        return result
