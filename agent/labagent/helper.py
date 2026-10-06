"""Proses pembantu (SYSTEM, `labagent.exe --helper`): logika murni, semua efek samping disuntikkan.

Tiga tugas yang tidak bisa dilakukan agent level-user:
1. menulis kebijakan registry Ctrl+Alt+Del ke profil user atas permintaan agent (`policy.json`);
2. mengunduh rilis baru yang ditandatangani dan menukar exe di folder pemasangan;
3. mengganti nama komputer Windows atas permintaan admin (dari server, lewat jawaban cek rilis).

`policy.json` ada di folder yang bisa ditulis user biasa, jadi isinya divalidasi ketat dan hanya
empat nilai registry yang pernah ditulis. Semua berkas yang menentukan exe (unduhan, skrip, daftar
rilis gagal) ada di folder pemasangan yang hanya bisa ditulis admin.
"""
import json
import os
import random
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import VERSION, updater

SID_RE = re.compile(r"^S-1-(5-21|12-1)(-\d{1,12}){3,4}$")
POLICY_FILE = "policy.json"
HEARTBEAT_FILE = "helper.json"
TICK_SECONDS = 2.0
POLICY_REAPPLY_SECONDS = 30.0
UPDATE_CHECK_SECONDS = 60.0
UPDATE_RETRY_SECONDS = 600.0
DOWNLOAD_JITTER_SECONDS = 120.0
HEARTBEAT_FRESH_SECONDS = 15.0
HOSTNAME_RE = re.compile(r"^(?!\d+$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,13}[A-Za-z0-9])?$")  # sama dengan server


# --- permintaan kebijakan (agent menulis, helper membaca) ----------------------
def write_policy_request(state_dir, sid: str, locked: bool, block_taskmgr: bool) -> None:
    path = Path(state_dir) / POLICY_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"sid": sid, "locked": bool(locked), "block_taskmgr": bool(block_taskmgr)}))
    os.replace(tmp, path)


def read_policy_request(state_dir) -> tuple[str, bool, bool] | None:
    try:
        raw = json.loads((Path(state_dir) / POLICY_FILE).read_text())
        sid, locked, taskmgr = raw["sid"], raw["locked"], raw["block_taskmgr"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not (isinstance(sid, str) and SID_RE.match(sid) and isinstance(locked, bool) and isinstance(taskmgr, bool)):
        return None
    return sid, locked, taskmgr


def helper_alive(state_dir, now: float) -> bool:
    try:
        beat = json.loads((Path(state_dir) / HEARTBEAT_FILE).read_text())
        return now - float(beat["ts"]) <= HEARTBEAT_FRESH_SECONDS
    except (OSError, ValueError, KeyError, TypeError):
        return False


def read_helper_errors(state_dir) -> list[str]:
    try:
        errors = json.loads((Path(state_dir) / HEARTBEAT_FILE).read_text()).get("errors", [])
        return [str(e) for e in errors][:8]
    except (OSError, ValueError, AttributeError):
        return []


# --- helper ---------------------------------------------------------------------
@dataclass
class Ports:
    """Efek samping; di Windows diisi winapi, di tes diisi tiruan."""
    own_sha: str
    fetch_manifest: Callable[[], object]                       # -> dict {"release": ...}; melempar saat gagal
    download: Callable[[str], None]                            # tulis rilis ke path
    apply_policy: Callable[[str, bool, bool], "list[str] | None"]  # None = profil belum dimuat
    launch_update: Callable[[str], None]                       # jalankan skrip pembaruan terlepas dari proses ini
    hostname: str = ""                                         # nama komputer saat helper dimulai
    rename_computer: "Callable[[str], str | None] | None" = None   # -> None bila berhasil, selain itu teks galat
    report_rename: "Callable[[str, bool, str], None] | None" = None  # (nama, berhasil, galat) ke server


class Helper:
    def __init__(self, cfg, install_dir, ports: Ports, clock=time.time, rand=random.uniform):
        self.cfg, self.ports, self._clock, self._rand = cfg, ports, clock, rand
        self.install_dir = Path(install_dir)
        self.state_dir = Path(cfg.state_dir)
        self.log: list[str] = []          # catatan singkat, dipakai tes dan heartbeat
        self.finished = False             # True setelah skrip pembaruan diluncurkan
        self._policy_key = None
        self._policy_at = 0.0
        self._errors: list[str] = []
        self._next_check = 0.0
        self._pending: tuple[str, float] | None = None
        self._renamed_to: str | None = None
        self._bad = self._load_bad()
        self._write_ok()

    # berkas di folder pemasangan (hanya admin yang bisa menulis) --------------
    def _load_bad(self) -> set[str]:
        bad_file = self.install_dir / "updater.json"
        try:
            bad = set(json.loads(bad_file.read_text()).get("bad", []))
        except (OSError, ValueError, AttributeError):
            bad = set()
        failed = self.install_dir / "update-failed.txt"
        try:
            sha = failed.read_text().strip().lower()
            if updater.SHA_RE.match(sha):
                bad.add(sha)
                self.log.append(f"pembaruan {sha[:12]} gagal dan dibatalkan")
            failed.unlink()
            bad_file.write_text(json.dumps({"bad": sorted(bad)}))
        except OSError:
            pass
        return bad

    def _write_ok(self) -> None:
        try:
            (self.install_dir / "helper.ok").write_text(self.ports.own_sha + "\n")
        except OSError:
            pass

    # putaran utama --------------------------------------------------------------
    def tick(self) -> None:
        now = self._clock()
        try:
            self._policy_tick(now)
        except Exception as exc:
            self._errors = [f"helper: {exc!r}"]
        self._heartbeat(now)
        if not self.finished and now >= self._next_check:
            try:
                self._check_tick(now)
            except Exception as exc:
                self.log.append(f"cek server gagal: {exc!r}")
                self._next_check = now + UPDATE_CHECK_SECONDS

    def run_forever(self, stop: threading.Event) -> None:
        while not stop.is_set() and not self.finished:
            self.tick()
            stop.wait(TICK_SECONDS)

    def _heartbeat(self, now: float) -> None:
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.state_dir / (HEARTBEAT_FILE + ".tmp")
            tmp.write_text(json.dumps({"ts": now, "sha": self.ports.own_sha, "version": VERSION,
                                       "errors": self._errors}))
            os.replace(tmp, self.state_dir / HEARTBEAT_FILE)
        except OSError:
            pass

    # kebijakan ---------------------------------------------------------------------
    def _policy_tick(self, now: float) -> None:
        request = read_policy_request(self.state_dir)
        if request is None:
            return
        if request == self._policy_key and now - self._policy_at < POLICY_REAPPLY_SECONDS:
            return
        errors = self.ports.apply_policy(*request)
        if errors is None:      # profil belum dimuat: coba lagi di tick berikutnya
            self._policy_key = None
            return
        self._policy_key, self._policy_at, self._errors = request, now, errors

    # pembaruan -----------------------------------------------------------------------
    def _check_tick(self, now: float) -> None:
        self._next_check = now + UPDATE_CHECK_SECONDS
        reply = self.ports.fetch_manifest()
        if not isinstance(reply, dict):
            return
        self._rename_tick(reply.get("rename"))
        self._update_tick(now, reply.get("release"))

    # ganti nama komputer -----------------------------------------------------------
    def _rename_tick(self, request) -> None:
        name = request.get("name") if isinstance(request, dict) else None
        if not isinstance(name, str) or self.ports.rename_computer is None or self.ports.report_rename is None:
            return
        if not HOSTNAME_RE.match(name):
            self.log.append("permintaan ganti nama tidak valid; diabaikan")
            return
        if name.lower() in (self.ports.hostname.lower(), (self._renamed_to or "").lower()):
            error = None       # sudah bernama itu (mis. laporan sebelumnya hilang): lapor ulang saja
        else:
            error = self.ports.rename_computer(name.upper())
            self.log.append(f"nama komputer -> {name.upper()}" if error is None else f"ganti nama gagal: {error}")
        if error is None:
            self._renamed_to = name
        self.ports.report_rename(name.upper(), error is None, error or "")

    def _update_tick(self, now: float, raw) -> None:
        if raw is None:
            self._pending = None
            return
        manifest = updater.parse_manifest(raw)
        if manifest is None:
            self.log.append("manifest rilis tidak valid")
            return
        sha = manifest["sha256"]
        if sha == self.ports.own_sha or sha in self._bad:
            self._pending = None
            return
        if self._pending is None or self._pending[0] != sha:
            self._pending = (sha, now + self._rand(0, DOWNLOAD_JITTER_SECONDS))  # sebar unduhan antar PC
        if now < self._pending[1]:
            return
        self._install(manifest, now)

    def _install(self, manifest: dict, now: float) -> None:
        new_path = self.install_dir / "labagent.new"
        try:
            self.ports.download(str(new_path))
            ok = (new_path.stat().st_size == manifest["size"]
                  and updater.file_sha256(new_path) == manifest["sha256"]
                  and updater.is_windows_exe(new_path))
        except Exception as exc:
            self.log.append(f"unduhan gagal: {exc!r}")
            ok = False
        if not ok:
            new_path.unlink(missing_ok=True)
            self._next_check = now + UPDATE_RETRY_SECONDS
            return
        script = self.install_dir / "apply-update.cmd"
        script.write_text(updater.apply_script(str(self.install_dir), manifest["sha256"]), encoding="ascii")
        self.log.append(f"memasang rilis {manifest['version']}")
        self.ports.launch_update(str(script))
        self.finished = True
