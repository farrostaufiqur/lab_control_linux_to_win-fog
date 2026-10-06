import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from labagent import updater  # noqa: E402


def manifest_raw(data=b"MZ" + b"x" * 100, version="1.2.0"):
    return {"version": version, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}


def test_manifest_valid():
    raw = manifest_raw()
    assert updater.parse_manifest(raw) == raw
    assert updater.parse_manifest({**raw, "signature": "diabaikan"}) == raw     # bidang lain dibuang


@pytest.mark.parametrize("patch", [
    {"version": "a b"}, {"version": ""}, {"version": "x" * 40}, {"sha256": "abc"}, {"sha256": "Z" * 64},
    {"size": 0}, {"size": -1}, {"size": "10"}, {"size": updater.MAX_RELEASE_BYTES + 1},
])
def test_manifest_rejects_malformed(patch):
    assert updater.parse_manifest({**manifest_raw(), **patch}) is None


def test_manifest_rejects_non_dict():
    assert updater.parse_manifest(None) is None and updater.parse_manifest([1]) is None


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
