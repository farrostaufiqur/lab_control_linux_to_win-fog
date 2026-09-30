"""Rilis agent: server hanya menyimpan dan membagikan; keaslian dijamin tanda tangan admin (Ed25519).

Format pesan harus sama persis dengan agent/labagent/updater.py (dicek oleh tes).
"""
import base64
import binascii
import hashlib
import re
from pathlib import Path

from . import ed25519

VERSION_RE = re.compile(r"^[0-9A-Za-z._-]{1,32}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_RELEASE_BYTES = 200 * 1024 * 1024


def message(version: str, sha256: str) -> bytes:
    return f"labagent-release-v1\n{version}\n{sha256}".encode()


def decode_signature(text: str) -> bytes | None:
    try:
        signature = base64.b64decode(text.replace(" ", "+"), validate=True)  # '+' bisa berubah jadi spasi di query
    except (binascii.Error, ValueError):
        return None
    return signature if len(signature) == 64 else None


def verify(public_key_hex: str, version: str, sha256: str, signature: bytes) -> bool:
    try:
        public = bytes.fromhex(public_key_hex)
    except ValueError:
        return False
    return ed25519.verify(public, message(version, sha256), signature)


def manifest(release: dict) -> dict:
    return {k: release[k] for k in ("version", "sha256", "size", "signature")}


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


def sha_of_stream(stream, tmp_path: Path, limit: int) -> tuple[str, int]:
    """Salin aliran ke tmp_path sambil menghitung SHA-256; melempar ValueError bila melebihi limit."""
    digest, size = hashlib.sha256(), 0
    with open(tmp_path, "wb") as out:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            size += len(chunk)
            if size > limit:
                raise ValueError("berkas terlalu besar")
            digest.update(chunk)
            out.write(chunk)
    return digest.hexdigest(), size
