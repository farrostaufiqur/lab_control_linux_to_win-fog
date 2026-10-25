import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from labagent import helper, updater  # noqa: E402
from labagent.config import AgentConfig  # noqa: E402

SID = "S-1-5-21-1111111111-2222222222-3333333333-1001"
OLD_SHA = "0" * 64
NEW_EXE = b"MZ" + b"n" * 200


def release(data=NEW_EXE, version="1.3.0"):
    return {"version": version, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}


class Clock:
    def __init__(self): self.now = 5000.0
    def __call__(self): return self.now


class World:
    def __init__(self, tmp_path, manifest=None, exe=NEW_EXE):
        self.install, self.state = tmp_path / "install", tmp_path / "state"
        self.install.mkdir()
        self.cfg = AgentConfig(server_url="http://x", token="t", state_dir=str(self.state))
        self.manifest, self.exe = manifest, exe
        self.applied, self.launched, self.downloads = [], [], 0
        self.loaded = True
        self.fail_manifest = False
        self.rename = None
        self.rename_error = None
        self.renamed, self.reports, self.hostname = [], [], "PC-01"
        self.clock = Clock()
        self.jobs, self.job_runs, self.job_reports = [], [], []
        self.job_result = (0, "ok", False)
        self.job_report_failures = 0

    def ports(self):
        def fetch():
            if self.fail_manifest:
                raise OSError("offline")
            return {"release": self.manifest, "rename": self.rename}

        def rename_computer(name):
            self.renamed.append(name)
            return self.rename_error

        def download(dest):
            self.downloads += 1
            Path(dest).write_bytes(self.exe)

        def apply(sid, locked, taskmgr):
            self.applied.append((sid, locked, taskmgr))
            return None if not self.loaded else []
        def fetch_job():
            return self.jobs.pop(0) if self.jobs else None

        def run_job(job):
            self.job_runs.append(job)
            return self.job_result

        def report_job(job_id, code, output, timed_out):
            if self.job_report_failures:
                self.job_report_failures -= 1
                raise OSError("offline")
            self.job_reports.append((job_id, code, output, timed_out))
        return helper.Ports(own_sha=OLD_SHA, fetch_job=fetch_job, run_job=run_job, report_job=report_job, fetch_manifest=fetch, download=download,
                            apply_policy=apply, launch_update=self.launched.append, hostname=self.hostname,
                            rename_computer=rename_computer,
                            report_rename=lambda name, ok, error: self.reports.append((name, ok, error)))

    def helper(self):
        return helper.Helper(self.cfg, self.install, self.ports(), clock=self.clock, rand=lambda a, b: 0)


# --- permintaan kebijakan ------------------------------------------------------
def test_policy_request_roundtrip_and_validation(tmp_path):
    helper.write_policy_request(tmp_path, SID, True, False)
    assert helper.read_policy_request(tmp_path) == (SID, True, False)
    for bad in ('{"sid": "S-1-5-21-1-2-3-4\\\\..\\\\x", "locked": true, "block_taskmgr": false}',
                '{"sid": "%s", "locked": 1, "block_taskmgr": false}' % SID,
                '{"sid": "S-1-5-18", "locked": true, "block_taskmgr": true}',
                "bukan json", "[]"):
        (tmp_path / "policy.json").write_text(bad)
        assert helper.read_policy_request(tmp_path) is None
    (tmp_path / "policy.json").unlink()
    assert helper.read_policy_request(tmp_path) is None


def test_policy_applied_once_then_reapplied_periodically(tmp_path):
    w = World(tmp_path)
    h = w.helper()
    helper.write_policy_request(w.state, SID, True, False)
    h.tick(); h.tick()
    assert w.applied == [(SID, True, False)]
    w.clock.now += helper.POLICY_REAPPLY_SECONDS + 1
    h.tick()
    assert len(w.applied) == 2
    helper.write_policy_request(w.state, SID, False, True)
    h.tick()
    assert w.applied[-1] == (SID, False, True)


def test_policy_retried_until_profile_loaded(tmp_path):
    w = World(tmp_path)
    w.loaded = False
    h = w.helper()
    helper.write_policy_request(w.state, SID, True, False)
    h.tick(); h.tick()
    assert len(w.applied) == 2
    w.loaded = True
    h.tick(); h.tick()
    assert len(w.applied) == 3


def test_heartbeat_and_errors(tmp_path):
    w = World(tmp_path)
    h = w.helper()
    assert not helper.helper_alive(w.state, w.clock.now)
    h.tick()
    assert helper.helper_alive(w.state, w.clock.now)
    assert not helper.helper_alive(w.state, w.clock.now + helper.HEARTBEAT_FRESH_SECONDS + 1)
    assert (w.install / "helper.ok").read_text().strip() == OLD_SHA


# --- pembaruan -------------------------------------------------------------------
def test_update_downloads_verifies_and_launches(tmp_path):
    w = World(tmp_path, manifest=release())
    h = w.helper()
    h.tick()
    assert w.downloads == 1 and h.finished
    assert (w.install / "labagent.new").read_bytes() == NEW_EXE
    script = (w.install / "apply-update.cmd").read_text()
    assert hashlib.sha256(NEW_EXE).hexdigest() in script
    assert w.launched == [str(w.install / "apply-update.cmd")]


def test_no_update_when_same_sha_or_no_release(tmp_path):
    w = World(tmp_path, manifest={**release(), **{"sha256": OLD_SHA}})
    w.manifest = None
    h = w.helper(); h.tick()
    assert w.downloads == 0 and not h.finished
    w.manifest = release(data=b"MZ-old")
    w.manifest["sha256"] = OLD_SHA  # sha sama dengan exe terpasang -> tidak ada yang dilakukan
    w.clock.now += 100
    h.tick()
    assert w.downloads == 0


def test_tampered_download_rejected_then_retried_later(tmp_path):
    w = World(tmp_path, manifest=release(), exe=b"MZ" + b"z" * 200)  # panjang sama, isi beda
    h = w.helper(); h.tick()
    assert w.downloads == 1 and not h.finished
    assert not (w.install / "labagent.new").exists()
    h.tick()
    assert w.downloads == 1  # belum waktunya mencoba lagi
    w.exe = NEW_EXE
    w.clock.now += helper.UPDATE_RETRY_SECONDS + 1
    h.tick()
    assert h.finished


def test_non_exe_download_rejected(tmp_path):
    data = b"#!/bin/sh\n" + b"x" * 100
    w = World(tmp_path, manifest=release(data=data), exe=data)
    h = w.helper(); h.tick()
    assert not h.finished


def test_download_jitter_delays_first_attempt(tmp_path):
    w = World(tmp_path, manifest=release())
    h = helper.Helper(w.cfg, w.install, w.ports(), clock=w.clock, rand=lambda a, b: 90.0)
    h.tick()
    assert w.downloads == 0
    w.clock.now += 95
    h.tick()
    assert h.finished


def test_server_errors_do_not_crash(tmp_path):
    w = World(tmp_path, manifest=release())
    w.fail_manifest = True
    h = w.helper(); h.tick()
    assert not h.finished
    w.fail_manifest = False
    w.clock.now += helper.UPDATE_CHECK_SECONDS + 1
    h.tick()
    assert h.finished


def test_failed_update_marker_blacklists_release(tmp_path):
    w = World(tmp_path, manifest=release())
    sha = hashlib.sha256(NEW_EXE).hexdigest()
    (w.install / "update-failed.txt").write_text(sha + "\r\n")
    h = w.helper(); h.tick()
    assert w.downloads == 0 and not (w.install / "update-failed.txt").exists()
    assert json.loads((w.install / "updater.json").read_text())["bad"] == [sha]
    # ingat setelah helper dinyalakan ulang
    h2 = w.helper(); w.clock.now += 100; h2.tick()
    assert w.downloads == 0
    # rilis baru (sha lain) tetap diterima
    w.manifest = release(data=b"MZ" + b"q" * 50, version="1.4.0"); w.exe = b"MZ" + b"q" * 50
    w.clock.now += 100; h2.tick()
    assert h2.finished


# --- ganti nama komputer -----------------------------------------------------------
def test_rename_applied_and_reported_once(tmp_path):
    w = World(tmp_path)     # tidak butuh kunci pembaruan
    w.rename = {"name": "lab-01"}
    h = w.helper(); h.tick()
    assert w.renamed == ["LAB-01"] and w.reports == [("LAB-01", True, "")]
    w.clock.now += helper.UPDATE_CHECK_SECONDS + 1
    h.tick()                        # server belum sempat mencatat: tidak mengganti lagi, hanya lapor ulang
    assert w.renamed == ["LAB-01"] and len(w.reports) == 2


def test_rename_failure_reported(tmp_path):
    w = World(tmp_path)
    w.rename, w.rename_error = {"name": "LAB-02"}, "Access is denied"
    h = w.helper(); h.tick()
    assert w.reports == [("LAB-02", False, "Access is denied")]
    w.rename = None
    w.clock.now += helper.UPDATE_CHECK_SECONDS + 1
    h.tick()
    assert len(w.reports) == 1


def test_rename_to_current_name_only_reports(tmp_path):
    w = World(tmp_path)
    w.rename = {"name": "pc-01"}
    h = w.helper(); h.tick()
    assert w.renamed == [] and w.reports == [("PC-01", True, "")]


@pytest.mark.parametrize("bad", ["", "a b", "-x", "12345", "a" * 16, "x;calc", 7, None])
def test_rename_invalid_names_ignored(tmp_path, bad):
    w = World(tmp_path)
    w.rename = {"name": bad}
    h = w.helper(); h.tick()
    assert w.renamed == [] and w.reports == []


def test_rename_and_update_in_same_reply(tmp_path):
    w = World(tmp_path, manifest=release())
    w.rename = {"name": "LAB-03"}
    h = w.helper(); h.tick()
    assert w.renamed == ["LAB-03"] and h.finished


# --- catatan pembantu untuk dashboard ------------------------------------------------------
def test_failures_are_written_to_heartbeat_notes(tmp_path):
    w = World(tmp_path, manifest=release(), exe=b"MZ" + b"z" * 200)               # unduhan rusak (isi beda)
    h = w.helper(); h.tick()
    notes = helper.read_helper_notes(w.state)
    assert notes == []                                    # unduhan rusak ditolak oleh pemeriksaan SHA, bukan galat unduh
    w.exe = None                                          # unduhan melempar galat
    w.clock.now += helper.UPDATE_RETRY_SECONDS + 1
    h.tick()
    notes = helper.read_helper_notes(w.state)
    assert notes and "unduhan gagal" in notes[-1][1] and notes[-1][0] == w.clock.now


def test_notes_dedupe_repeated_errors_but_keep_new_ones(tmp_path):
    w = World(tmp_path)
    w.fail_manifest = True
    h = w.helper()
    for _ in range(4):
        h.tick()
        w.clock.now += helper.UPDATE_CHECK_SECONDS + 1
    assert len(helper.read_helper_notes(w.state)) == 1                           # galat sama dalam 10 menit: satu catatan
    w.clock.now += helper.NOTE_REPEAT_SECONDS
    h.tick()
    assert len(helper.read_helper_notes(w.state)) == 2                           # sudah lama: dicatat lagi
    w.fail_manifest, w.manifest = False, release()
    w.clock.now += helper.UPDATE_CHECK_SECONDS + 1
    h.tick()
    assert "memasang rilis" in helper.read_helper_notes(w.state)[-1][1]


def test_notes_are_capped_and_bad_heartbeat_is_ignored(tmp_path):
    w = World(tmp_path)
    h = w.helper()
    for i in range(25):
        h._note(f"catatan {i}")
    h._heartbeat(w.clock.now)
    notes = helper.read_helper_notes(w.state)
    assert len(notes) == helper.NOTES_KEPT and notes[-1][1] == "catatan 24"
    (w.state / "helper.json").write_text("bukan json")
    assert helper.read_helper_notes(w.state) == []
    assert helper.read_helper_notes(tmp_path / "tidak-ada") == []


# --- perintah shell dari admin -----------------------------------------------------
def finish_worker(h):
    if h._worker is not None:
        h._worker.join(5)


def test_job_is_run_and_reported(tmp_path):
    w = World(tmp_path)
    w.jobs.append({"id": 4, "shell": "cmd", "script": "hostname", "timeout": 30})
    h = w.helper()
    h.tick(); finish_worker(h)
    assert [j["id"] for j in w.job_runs] == [4]
    assert w.job_reports == [(4, 0, "ok", False)]


def test_invalid_job_is_ignored_with_a_note(tmp_path):
    w = World(tmp_path)
    w.jobs.append({"id": 4, "shell": "bash", "script": "ls", "timeout": 30})
    h = w.helper()
    h.tick(); finish_worker(h)
    assert w.job_runs == [] and any("tidak valid" in line for line in h.log)


def test_jobs_checked_only_every_few_seconds(tmp_path):
    w = World(tmp_path)
    h = w.helper()
    h.tick()
    w.jobs.append({"id": 1, "shell": "cmd", "script": "a", "timeout": 5})
    h.tick(); finish_worker(h)
    assert w.job_runs == []
    w.clock.now += helper.JOB_CHECK_SECONDS + 1
    h.tick(); finish_worker(h)
    assert len(w.job_runs) == 1


def test_job_report_retried_then_given_up(tmp_path, monkeypatch):
    monkeypatch.setattr(helper.time, "sleep", lambda s: None)
    w = World(tmp_path)
    w.jobs.append({"id": 4, "shell": "cmd", "script": "a", "timeout": 5})
    w.job_report_failures = 2
    h = w.helper()
    h.tick(); finish_worker(h)
    assert w.job_reports == [(4, 0, "ok", False)]


def test_job_runner_crash_is_reported_as_failure(tmp_path):
    w = World(tmp_path)
    w.jobs.append({"id": 4, "shell": "cmd", "script": "a", "timeout": 5})
    ports = w.ports()
    ports.run_job = lambda job: 1 / 0
    h = helper.Helper(w.cfg, w.install, ports, clock=w.clock, rand=lambda a, b: 0)
    h.tick(); finish_worker(h)
    assert w.job_reports[0][0] == 4 and w.job_reports[0][1] == -1 and "ZeroDivisionError" in w.job_reports[0][2]


def test_update_is_not_blocked_by_a_running_job(tmp_path):
    import threading
    w = World(tmp_path, manifest=release())
    gate = threading.Event()
    ports = w.ports()
    ports.run_job = lambda job: (gate.wait(5), (0, "", False))[1]
    w.jobs.append({"id": 4, "shell": "cmd", "script": "ping -t x", "timeout": 600})
    h = helper.Helper(w.cfg, w.install, ports, clock=w.clock, rand=lambda a, b: 0)
    h.tick()
    assert h.finished and w.downloads == 1      # pembaruan jalan walau perintah masih berlangsung
    gate.set(); finish_worker(h)
