import base64
import hashlib
import sqlite3
import sys
from pathlib import Path

import pytest

from labserver import ed25519, release
from labserver.app import create_app
from labserver.config import Config
from test_api import ADMIN, AGENT, poll

SECRET = bytes(range(32))
PUBLIC = ed25519.public_key(SECRET).hex()
EXE = b"MZ" + b"e" * 500


def sign(data=EXE, version="1.3.0", secret=SECRET):
    sha = hashlib.sha256(data).hexdigest()
    return base64.b64encode(ed25519.sign(secret, release.message(version, sha))).decode()


def make(tmp_path, key=PUBLIC):
    cfg = Config(
        lab_name="Lab Test", timezone="Asia/Jakarta", host="127.0.0.1", port=0,
        db_path=str(tmp_path / "t.db"), admin_user="admin", admin_password="secret-pass",
        agent_token="agent-token-1234567890", poll_interval=5, online_timeout=20,
        command_ttl=120, event_retention_days=30, warn_seconds=300, update_public_key=key,
    )
    return create_app(cfg).test_client()


@pytest.fixture
def client(tmp_path):
    return make(tmp_path)


def upload(client, data=EXE, version="1.3.0", signature=None, headers=ADMIN):
    signature = sign(data, version) if signature is None else signature
    return client.post("/api/release", query_string={"version": version, "signature": signature},
                       data=data, headers={**headers, "Content-Type": "application/octet-stream"})


def test_upload_then_agent_gets_manifest_and_file(client):
    poll(client)
    assert client.post("/api/agent/release", json={"host": "PC-01", "sha": "0" * 64}, headers=AGENT) \
        .get_json() == {"release": None}
    assert upload(client).status_code == 200
    manifest = client.post("/api/agent/release", json={"host": "PC-01", "sha": "0" * 64, "version": "1.2.0"},
                           headers=AGENT).get_json()["release"]
    assert manifest["version"] == "1.3.0" and manifest["size"] == len(EXE)
    assert manifest["sha256"] == hashlib.sha256(EXE).hexdigest() and set(manifest) == {"version", "sha256", "size", "signature"}
    assert release.verify(PUBLIC, "1.3.0", manifest["sha256"], base64.b64decode(manifest["signature"]))
    assert client.get("/api/agent/release/file", headers=AGENT).data == EXE


def test_agent_endpoints_need_token(client):
    upload(client)
    assert client.post("/api/agent/release", json={"host": "PC-01"}).status_code == 403
    assert client.get("/api/agent/release/file").status_code == 403


def test_agent_release_blocked_via_tunnel(client):
    upload(client)
    assert client.get("/api/agent/release/file", headers={**AGENT, "Cf-Ray": "x"}).status_code == 404


def test_upload_requires_admin_and_csrf_header(client):
    assert client.post("/api/release", data=EXE).status_code == 401
    only_auth = {"Authorization": ADMIN["Authorization"]}
    assert upload(client, headers=only_auth).status_code == 400


def test_upload_rejects_wrong_signature_and_bad_input(client):
    assert upload(client, signature=sign(secret=bytes(range(1, 33)))).status_code == 400
    assert upload(client, version="9.9.9", signature=sign(version="1.3.0")).status_code == 400          # tanda tangan untuk versi lain
    assert upload(client, signature="bukan-base64").status_code == 400
    assert upload(client, version="bad version").status_code == 400
    script = b"#!/bin/sh\necho hi\n"
    assert upload(client, data=script, signature=sign(script)).status_code == 400
    assert upload(client, data=b"", signature=sign(b"")).status_code == 400
    assert client.get("/api/release", headers=ADMIN).get_json()["release"] is None


def test_upload_disabled_without_public_key(tmp_path):
    assert upload(make(tmp_path, key="")).status_code == 409


def test_oversize_rejected(client, monkeypatch):
    monkeypatch.setattr(release, "MAX_RELEASE_BYTES", 100)
    assert upload(client).status_code == 413


def test_new_upload_replaces_old_and_delete_stops_distribution(client, tmp_path):
    upload(client)
    other = b"MZ" + b"f" * 300
    assert upload(client, data=other, version="1.4.0").status_code == 200
    files = sorted(p.name for p in (tmp_path / "releases").iterdir())
    assert files == [f"labagent-{hashlib.sha256(other).hexdigest()}.exe"]
    assert client.get("/api/agent/release/file", headers=AGENT).data == other
    assert client.delete("/api/release", headers=ADMIN).status_code == 200
    assert client.get("/api/agent/release/file", headers=AGENT).status_code == 404
    assert list((tmp_path / "releases").iterdir()) == []


def test_status_counts_up_to_date_pcs(client):
    upload(client)
    sha = hashlib.sha256(EXE).hexdigest()
    poll(client, host="PC-01", exe_sha=sha, version="1.3.0")
    poll(client, host="PC-02", exe_sha="0" * 64, version="1.2.0")
    status = client.get("/api/release", headers=ADMIN).get_json()
    assert (status["pcs_total"], status["pcs_current"], status["signing_enabled"]) == (2, 1, True)
    pcs = {p["id"]: p for p in client.get("/api/pcs", headers=ADMIN).get_json()["pcs"]}
    assert pcs["PC-01"]["exe_sha"] == sha and pcs["PC-02"]["version"] == "1.2.0"
    assert client.get("/api/pcs", headers=ADMIN).get_json()["release"]["version"] == "1.3.0"


def test_helper_check_updates_known_pc_only(client):
    poll(client)
    client.post("/api/agent/release", json={"host": "PC-01", "sha": "a" * 64, "version": "1.3.0"}, headers=AGENT)
    client.post("/api/agent/release", json={"host": "PC-99", "sha": "a" * 64, "version": "1.3.0"}, headers=AGENT)
    pcs = {p["id"]: p for p in client.get("/api/pcs", headers=ADMIN).get_json()["pcs"]}
    assert set(pcs) == {"PC-01"} and pcs["PC-01"]["exe_sha"] == "a" * 64


def test_migration_adds_exe_sha_column(tmp_path):
    path = tmp_path / "t.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE pcs (id TEXT PRIMARY KEY, first_seen REAL NOT NULL, last_seen REAL NOT NULL, "
               "ip TEXT, user TEXT, app TEXT, title TEXT, idle INTEGER, uptime INTEGER, mem INTEGER, "
               "locked INTEGER, version TEXT, override TEXT)")
    db.commit(); db.close()
    client = make(tmp_path)
    assert poll(client, exe_sha="b" * 64).status_code == 200


def test_signature_format_matches_agent():
    agent_dir = Path(__file__).resolve().parents[2] / "agent"
    if not agent_dir.exists():
        pytest.skip("folder agent tidak ada")
    sys.path.insert(0, str(agent_dir))
    from labagent import ed25519 as agent_ed, updater
    assert updater.release_message("1.0", "a" * 64) == release.message("1.0", "a" * 64)
    assert (agent_dir / "labagent" / "ed25519.py").read_bytes() == Path(ed25519.__file__).read_bytes()
    assert updater.VERSION_RE.pattern == release.VERSION_RE.pattern
    assert updater.MAX_RELEASE_BYTES == 200 * 1024 * 1024
