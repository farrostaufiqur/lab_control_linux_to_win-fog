"""Alat admin untuk rilis agent (jalankan di komputer admin, bukan di server).

  python tools/release.py keygen  rilis.key        # sekali saja; cetak kunci publik
  python tools/release.py sign    dist/labagent.exe --key rilis.key --version 1.2.0

`keygen` menulis kunci privat ke berkas (jangan di-commit, jangan disimpan di server) dan
mencetak kunci publik untuk `install.ps1 -UpdatePublicKey` / `update_public_key` di config
server. `sign` menulis `<exe>.release.json`; unggah exe dan berkas itu lewat dashboard.
"""
import argparse
import base64
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from labagent import ed25519, updater  # noqa: E402


def keygen(path: Path) -> None:
    if path.exists():
        raise SystemExit(f"{path} sudah ada; tidak ditimpa.")
    secret = os.urandom(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(secret.hex() + "\n")
    print(f"Kunci privat ditulis ke {path} (simpan aman, cadangkan, jangan ke server).")
    print("Kunci publik:", ed25519.public_key(secret).hex())


def sign(exe: Path, key_file: Path, version: str) -> Path:
    if not updater.VERSION_RE.match(version):
        raise SystemExit("versi hanya boleh huruf, angka, titik, garis (maks 32 karakter)")
    if not updater.is_windows_exe(exe):
        raise SystemExit(f"{exe} bukan exe Windows")
    secret = bytes.fromhex(key_file.read_text().strip())
    sha = hashlib.sha256(exe.read_bytes()).hexdigest()
    manifest = {
        "version": version, "sha256": sha, "size": exe.stat().st_size,
        "signature": base64.b64encode(ed25519.sign(secret, updater.release_message(version, sha))).decode(),
    }
    out = exe.with_name(exe.name + ".release.json")
    out.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"{out}\n  versi {version}\n  sha256 {sha}")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("keygen"); k.add_argument("keyfile", type=Path)
    s = sub.add_parser("sign"); s.add_argument("exe", type=Path)
    s.add_argument("--key", type=Path, required=True); s.add_argument("--version", required=True)
    args = parser.parse_args()
    keygen(args.keyfile) if args.cmd == "keygen" else sign(args.exe, args.key, args.version)


if __name__ == "__main__":
    main()
