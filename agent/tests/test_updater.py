import base64
import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from labagent import ed25519, updater  # noqa: E402

SECRET = bytes(range(32))
PUBLIC = ed25519.public_key(SECRET).hex()


def signed_manifest(data=b"MZ" + b"x" * 100, version="1.2.0", secret=SECRET):
    sha = hashlib.sha256(data).hexdigest()
    signature = ed25519.sign(secret, updater.release_message(version, sha))
    return {"version": version, "sha256": sha, "size": len(data),
            "signature": base64.b64encode(signature).decode()}


def test_manifest_valid_and_signature_checks_out():
    manifest = updater.parse_manifest(signed_manifest())
    assert manifest and updater.signature_valid(PUBLIC, manifest)


def test_signature_by_other_key_rejected():
    manifest = updater.parse_manifest(signed_manifest(secret=bytes(range(1, 33))))
    assert not updater.signature_valid(PUBLIC, manifest)


def test_signature_binds_version_and_hash():
    raw = signed_manifest()
    assert not updater.signature_valid(PUBLIC, updater.parse_manifest({**raw, "version": "9.9.9"}))
    other = hashlib.sha256(b"lain").hexdigest()
    assert not updater.signature_valid(PUBLIC, updater.parse_manifest({**raw, "sha256": other}))


@pytest.mark.parametrize("patch", [
    {"version": "a b"}, {"version": ""}, {"version": "x" * 40}, {"sha256": "abc"}, {"sha256": "Z" * 64},
    {"size": 0}, {"size": -1}, {"size": "10"}, {"size": updater.MAX_RELEASE_BYTES + 1},
    {"signature": "bukan base64!!"}, {"signature": base64.b64encode(b"pendek").decode()}, {"signature": None},
])
def test_manifest_rejects_malformed(patch):
    assert updater.parse_manifest({**signed_manifest(), **patch}) is None


def test_manifest_rejects_non_dict():
    assert updater.parse_manifest(None) is None and updater.parse_manifest([1]) is None


def test_bad_public_key_hex():
    manifest = updater.parse_manifest(signed_manifest())
    assert not updater.signature_valid("zz", manifest)
    assert not updater.signature_valid("ab" * 5, manifest)


def test_file_sha_and_mz(tmp_path):
    f = tmp_path / "a.exe"
    f.write_bytes(b"MZabc")
    assert updater.file_sha256(f) == hashlib.sha256(b"MZabc").hexdigest()
    assert updater.is_windows_exe(f)
    f.write_bytes(b"#!/bin/sh")
    assert not updater.is_windows_exe(f)


def test_apply_script_swaps_verifies_and_rolls_back():
    sha = "a" * 64
    script = updater.apply_script("C:\\Program Files\\LabAgent\\", sha)
    assert "\r\n" in script and script.startswith("@echo off")
    assert 'set "DIR=C:\\Program Files\\LabAgent"' in script
    assert f'set "NEWSHA={sha}"' in script
    assert "labagent.new" in script and "labagent.bak" in script
    assert "helper.ok" in script and "update-failed.txt" in script
    # urutan: pindah exe lama -> pasang baru -> jalankan -> cek -> (gagal) pulihkan
    order = [script.index(x) for x in (":swap", ":placed", "call :start_tasks", "findstr", ":rollback", ":done")]
    assert order == sorted(order)
    assert "LabAgentUpdate" in script


def test_release_tool_roundtrip(tmp_path):
    import json
    sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
    import release
    key = tmp_path / "k.key"
    release.keygen(key)
    assert oct(key.stat().st_mode & 0o777) == "0o600"
    exe = tmp_path / "labagent.exe"
    exe.write_bytes(b"MZ" + b"e" * 50)
    out = release.sign(exe, key, "2.0.0")
    manifest = updater.parse_manifest(json.loads(out.read_text()))
    public = ed25519.public_key(bytes.fromhex(key.read_text().strip())).hex()
    assert manifest and updater.signature_valid(public, manifest)
    with pytest.raises(SystemExit):
        release.keygen(key)
