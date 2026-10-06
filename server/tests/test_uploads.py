import hashlib
import json
import time

import pytest

from labserver import shared, uploads
from test_api import ADMIN, client, poll  # noqa: F401  (client = fixture)

EXE = b"MZ" + b"x" * 5000


def create(client, **body):
    return client.post("/api/uploads", json=body, headers=ADMIN)


def chunk(client, uid, offset, data):
    return client.put(f"/api/uploads/{uid}", query_string={"offset": offset}, data=data,
                      headers={**ADMIN, "Content-Type": "application/octet-stream"})


def send_all(client, uid, data, size=1000):
    for offset in range(0, len(data), size):
        assert chunk(client, uid, offset, data[offset:offset + size]).status_code == 200


def test_shared_file_in_chunks_matches_direct_upload(client):
    data = bytes(range(256)) * 40
    start = create(client, kind="shared", path="materi/bab1.bin", size=len(data)).get_json()
    assert len(start["id"]) == 16 and start["chunk"] == 1024 * 1024
    send_all(client, start["id"], data, 777)
    assert client.get(f"/api/uploads/{start['id']}", headers=ADMIN).get_json() == {"received": len(data), "size": len(data)}
    done = client.post(f"/api/uploads/{start['id']}/finish", headers=ADMIN)
    assert done.status_code == 200 and done.get_json()["sha256"] == hashlib.sha256(data).hexdigest()
    files = {f["path"]: f for f in client.get("/api/shared", headers=ADMIN).get_json()["files"]}
    assert files["materi/bab1.bin"]["size"] == len(data)
    assert client.get("/api/shared/file", query_string={"path": "materi/bab1.bin"}, headers=ADMIN).data == data
    assert client.get(f"/api/uploads/{start['id']}", headers=ADMIN).status_code == 404     # sesi dibersihkan


def test_resume_after_a_lost_response(client):
    data = b"A" * 3000
    uid = create(client, kind="shared", path="a.bin", size=3000).get_json()["id"]
    assert chunk(client, uid, 0, data[:1000]).get_json() == {"received": 1000, "resync": False}
    # balasan potongan kedua hilang di jalan; browser mengirim ulang dengan offset lama
    assert chunk(client, uid, 1000, data[1000:2000]).status_code == 200
    stale = chunk(client, uid, 1000, data[1000:2000])
    assert stale.status_code == 409 and stale.get_json() == {"received": 2000, "resync": True}
    assert chunk(client, uid, 2000, data[2000:]).get_json()["received"] == 3000          # lanjut dari posisi server
    assert client.post(f"/api/uploads/{uid}/finish", headers=ADMIN).status_code == 200


def test_gap_and_overflow_are_rejected_without_corrupting(client):
    uid = create(client, kind="shared", path="a.bin", size=2000).get_json()["id"]
    assert chunk(client, uid, 500, b"x" * 100).status_code == 409                          # melompat
    assert chunk(client, uid, 0, b"x" * 1500).status_code == 200
    assert chunk(client, uid, 1500, b"y" * 600).status_code == 413                         # melebihi ukuran janji
    assert client.get(f"/api/uploads/{uid}", headers=ADMIN).get_json()["received"] == 1500  # potongan buruk dibuang
    assert chunk(client, uid, 1500, b"y" * 500).status_code == 200
    assert client.post(f"/api/uploads/{uid}/finish", headers=ADMIN).status_code == 200


def test_finish_requires_complete_upload(client):
    uid = create(client, kind="shared", path="a.bin", size=100).get_json()["id"]
    chunk(client, uid, 0, b"x" * 40)
    assert client.post(f"/api/uploads/{uid}/finish", headers=ADMIN).status_code == 409
    assert client.get(f"/api/uploads/{uid}", headers=ADMIN).status_code == 200            # sesi masih ada untuk dilanjutkan


def test_release_in_chunks(client):
    uid = create(client, kind="release", version="1.9.0", size=len(EXE)).get_json()["id"]
    send_all(client, uid, EXE, 900)
    rel = client.post(f"/api/uploads/{uid}/finish", headers=ADMIN).get_json()["release"]
    assert rel["version"] == "1.9.0" and rel["sha256"] == hashlib.sha256(EXE).hexdigest()
    assert client.get("/api/release", headers=ADMIN).get_json()["release"]["size"] == len(EXE)
    uid = create(client, kind="release", size=10).get_json()["id"]                         # bukan exe
    chunk(client, uid, 0, b"#!/bin/sh\n\n")
    assert client.post(f"/api/uploads/{uid}/finish", headers=ADMIN).status_code in (400, 409)


def test_non_exe_release_rejected_at_finish(client):
    data = b"#!/bin/sh\necho hi\n"
    uid = create(client, kind="release", size=len(data)).get_json()["id"]
    chunk(client, uid, 0, data)
    assert client.post(f"/api/uploads/{uid}/finish", headers=ADMIN).status_code == 400
    assert client.get("/api/release", headers=ADMIN).get_json()["release"] is None


@pytest.mark.parametrize("body,status", [
    ({"kind": "shared", "path": "../x", "size": 10}, 400), ({"kind": "shared", "path": "a", "size": 0}, 400),
    ({"kind": "shared", "path": "a", "size": "10"}, 400), ({"kind": "shared", "path": "a"}, 400),
    ({"kind": "release", "size": 10, "version": "bad version"}, 400), ({"kind": "lain", "size": 10}, 400),
])
def test_create_validation(client, body, status):
    assert create(client, **body).status_code == status


def test_auth_unknown_ids_and_abort(client):
    assert client.post("/api/uploads", json={"kind": "shared", "path": "a", "size": 1}).status_code == 401
    uid = create(client, kind="shared", path="a", size=10).get_json()["id"]
    assert client.put(f"/api/uploads/{uid}?offset=0", data=b"x").status_code == 401
    assert client.post(f"/api/uploads/{uid}/finish").status_code == 401
    for bad in ("../../etc", "zzzzzzzzzzzzzzzz", "0" * 16, "x"):
        assert client.get(f"/api/uploads/{bad}", headers=ADMIN).status_code == 404
    assert client.delete(f"/api/uploads/{uid}", headers=ADMIN).status_code == 200
    assert client.get(f"/api/uploads/{uid}", headers=ADMIN).status_code == 404
    assert client.delete(f"/api/uploads/{uid}", headers=ADMIN).status_code == 200          # idempoten


def test_failed_commit_reports_the_reason(client, monkeypatch):
    poll(client)
    client.put("/api/shared/settings", json={"path": "Desktop/s", "default_mode": "both"}, headers=ADMIN)
    client.post("/api/shared/file", query_string={"path": "Tugas/A.txt"}, data=b"x", headers=ADMIN)
    uid = create(client, kind="shared", path="tugas/a.txt", size=3).get_json()["id"]       # bentrok huruf besar-kecil
    chunk(client, uid, 0, b"abc")
    assert client.post(f"/api/uploads/{uid}/finish", headers=ADMIN).status_code == 409


def test_session_limit_and_expiry(client, tmp_path, monkeypatch):
    monkeypatch.setattr(uploads, "MAX_SESSIONS", 2)
    a = create(client, kind="shared", path="a", size=5)
    create(client, kind="shared", path="b", size=5)
    assert create(client, kind="shared", path="c", size=5).status_code == 409
    root = tmp_path / "uploads"
    meta = root / f"{a.get_json()['id']}.json"
    data = json.loads(meta.read_text())
    data["created"] = time.time() - uploads.TTL_SECONDS - 10                              # kedaluwarsa
    meta.write_text(json.dumps(data))
    assert create(client, kind="shared", path="c", size=5).status_code == 200            # yang lama dibersihkan
    assert not (root / f"{a.get_json()['id']}.part").exists()


def test_orphan_parts_are_pruned(tmp_path):
    store = uploads.Uploads(tmp_path / "u")
    (tmp_path / "u").mkdir()
    (tmp_path / "u" / ("ab" * 8 + ".part")).write_bytes(b"sisa")
    store.prune(time.time())
    assert list((tmp_path / "u").iterdir()) == []


def test_shared_upload_has_no_size_ceiling_but_release_does(client):
    assert create(client, kind="shared", path="raksasa.bin", size=50 * 1024 ** 3).status_code in (200, 413)   # 413 hanya bila disk kurang
    from labserver import release
    assert create(client, kind="release", size=release.MAX_RELEASE_BYTES + 1).status_code == 413
