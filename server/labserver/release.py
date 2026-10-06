"""Rilis agent: admin mengunggah labagent.exe lewat dashboard; server menyimpan dan membagikannya.

Tidak ada tanda tangan: yang berhak memasang rilis adalah siapa pun yang bisa login sebagai admin.
"""
import hashlib
import re
from pathlib import Path

VERSION_RE = re.compile(r"^[0-9A-Za-z._-]{1,32}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_RELEASE_BYTES = 200 * 1024 * 1024


def default_version(sha256: str) -> str:
    return f"build-{sha256[:8]}"


def manifest(release: dict) -> dict:
    return {k: release[k] for k in ("version", "sha256", "size")}


def store_file(directory: Path, sha256: str, tmp_path: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"labagent-{sha256}.exe"
    tmp_path.replace(target)
    return target


def prune_files(directory: Path, keep_sha: str | None) -> None:
    for old in directory.glob("labagent-*.exe"):
        if keep_sha is None or old.name != f"labagent-{keep_sha}.exe":
            old.unlink(missing_ok=True)
    for stale in directory.glob("upload-*.tmp"):
        stale.unlink(missing_ok=True)


def sha_of_stream(stream, tmp_path: Path, limit: int | None) -> tuple[str, int]:
    """Salin aliran ke tmp_path sambil menghitung SHA-256; melempar ValueError bila melebihi limit (None = tanpa batas)."""
    digest, size = hashlib.sha256(), 0
    with open(tmp_path, "wb") as out:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            size += len(chunk)
            if limit is not None and size > limit:
                raise ValueError("berkas terlalu besar")
            digest.update(chunk)
            out.write(chunk)
    return digest.hexdigest(), size
