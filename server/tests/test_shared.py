import hashlib

import pytest

from labserver import shared
from test_api import ADMIN, AGENT, TUNNEL, client, poll  # noqa: F401  (client = fixture)

DATA = b"isi file contoh"
SHA = hashlib.sha256(DATA).hexdigest()


def setup(client, path="Desktop/shared", default="both", pcs=("PC-01",)):
    for pc in pcs:
        poll(client, host=pc)
    assert client.put("/api/shared/settings", json={"path": path, "default_mode": default}, headers=ADMIN).status_code == 200


def put(client, path="a.txt", data=DATA, host="PC-01", headers=AGENT, **query):
    return client.put("/api/agent/shared/file", query_string={"host": host, "path": path, **query}, data=data, headers=headers)


def get(client, path="a.txt", host="PC-01", headers=AGENT):
    return client.get("/api/agent/shared/file", query_string={"host": host, "path": path}, headers=headers)


def index(client, host="PC-01", headers=AGENT):
    return client.get("/api/agent/shared/index", query_string={"host": host}, headers=headers)


def set_mode(client, pc, mode):
    assert client.put(f"/api/pcs/{pc}/shared-mode", json={"mode": mode}, headers=ADMIN).status_code == 200


# --- nama dan path ---------------------------------------------------------------------
@pytest.mark.parametrize("bad", ["", "/a", "a/", "../a", "a/../b", "a//b", "a\\b", "C:/a", "a:b", "con", "nul.txt", "a.", "a ",
                                 "x" * 130, "/".join("d" * 3 for _ in range(10)), "desktop.ini", "a/Thumbs.db", "~$a.docx",
                                 "b.labtmp", "a?b", "a\x00b", None, 5])
def test_clean_path_rejects_unsafe_names(bad):
    assert shared.clean_path(bad) is None


@pytest.mark.parametrize("good", ["a.txt", "tugas/siswa 1/jawaban.docx", "Laporan (1).pdf", "é.txt", "a/b/c/d/e/f/g/h.txt"])
def test_clean_path_accepts_normal_names(good):
    assert shared.clean_path(good) == good


def test_profile_path_rules():
    assert shared.clean_profile_path("Desktop\\shared\\") == "Desktop/shared"
    assert shared.clean_profile_path("  ") == "" and shared.clean_profile_path("/") == ""
    for bad in ("../x", "C:\\Windows", "C:/x", "a/../b", "a\x01", "nul", None):
        assert shared.clean_profile_path(bad) is None, bad


# --- pengaturan dan jawaban poll ----------------------------------------------------------
def test_poll_reports_folder_and_mode_per_pc(client):
    poll(client)
    assert poll(client).get_json()["shared"] == {"mode": "off", "path": "", "interval": 10}
    setup(client, default="receive", pcs=("PC-01", "PC-02", "PC-03"))
    set_mode(client, "PC-02", "both")
    set_mode(client, "PC-03", "send")
    modes = {h: poll(client, host=h).get_json()["shared"]["mode"] for h in ("PC-01", "PC-02", "PC-03")}
    assert modes == {"PC-01": "receive", "PC-02": "both", "PC-03": "send"}
    assert poll(client).get_json()["shared"]["path"] == "Desktop/shared"
    set_mode(client, "PC-02", "default")
    assert poll(client, host="PC-02").get_json()["shared"]["mode"] == "receive"
    set_mode(client, "PC-01", "off")
    assert poll(client).get_json()["shared"]["mode"] == "off"


def test_empty_folder_disables_everything_for_all_pcs(client):
    setup(client, default="both")
    set_mode(client, "PC-01", "both")
    client.put("/api/shared/settings", json={"path": "", "default_mode": "both"}, headers=ADMIN)
    assert poll(client).get_json()["shared"]["mode"] == "off"
    assert index(client).status_code == 403


def test_settings_validation_and_auth(client):
    poll(client)
    put_settings = lambda body, headers=ADMIN: client.put("/api/shared/settings", json=body, headers=headers)
    assert put_settings({"path": "../x"}).status_code == 400
    assert put_settings({"path": "C:\\x"}).status_code == 400
    assert put_settings({"path": "Desktop/s", "default_mode": "terserah"}).status_code == 400
    assert put_settings({"path": "Desktop/s"}, headers={}).status_code == 401
    assert client.put("/api/pcs/PC-01/shared-mode", json={"mode": "x"}, headers=ADMIN).status_code == 400
    assert client.put("/api/pcs/NOPE/shared-mode", json={"mode": "off"}, headers=ADMIN).status_code == 404


# --- izin agent -------------------------------------------------------------------------------
def test_agent_endpoints_need_token_and_are_lan_only(client):
    setup(client)
    assert index(client, headers={}).status_code == 403
    assert put(client, headers={}).status_code == 403
    for call in (lambda h: index(client, headers=h), lambda h: put(client, headers=h), lambda h: get(client, headers=h)):
        assert call({**AGENT, **TUNNEL}).status_code == 404            # Cloudflare: ditolak
    assert put(client).status_code == 200


def test_unknown_pc_gets_404(client):
    setup(client)
    assert index(client, host="PC-99").status_code == 404
    assert index(client, host="../x").status_code == 404


def test_modes_enforced_on_server(client):
    setup(client, pcs=("PC-01", "PC-02", "PC-03", "PC-04"))
    assert put(client, host="PC-01").status_code == 200                # bawaan both
    for pc, mode in (("PC-01", "both"), ("PC-02", "receive"), ("PC-03", "send"), ("PC-04", "off")):
        set_mode(client, pc, mode)
    results = {pc: (index(client, pc).status_code, get(client, host=pc).status_code, put(client, "b.txt", host=pc).status_code,
                    client.delete("/api/agent/shared/file", query_string={"host": pc, "path": "b.txt"}, headers=AGENT).status_code)
               for pc in ("PC-01", "PC-02", "PC-03", "PC-04")}
    assert results["PC-01"] == (200, 200, 200, 200)                    # dua arah
    assert results["PC-02"] == (200, 200, 403, 403)                    # terima saja: tidak boleh mengubah server
    assert results["PC-03"] == (200, 403, 200, 200)                    # kirim saja: tidak boleh mengunduh isi
    assert results["PC-04"] == (403, 403, 403, 403)                    # mati


# --- unggah, unduh, hapus ----------------------------------------------------------------------
def test_upload_download_index_delete_roundtrip(client, tmp_path):
    setup(client)
    reply = put(client, "tugas/a.txt", mtime=1700000000).get_json()
    assert reply["sha256"] == SHA and reply["size"] == len(DATA) and reply["mtime"] == 1700000000
    assert index(client).get_json()["files"] == {"tugas/a.txt": {"sha256": SHA, "size": len(DATA), "mtime": 1700000000.0}}
    assert get(client, "tugas/a.txt").data == DATA
    assert (tmp_path / "shared" / "files" / "tugas" / "a.txt").read_bytes() == DATA
    assert client.delete("/api/agent/shared/file", query_string={"host": "PC-01", "path": "tugas/a.txt"}, headers=AGENT).status_code == 200
    assert index(client).get_json()["files"] == {} and get(client, "tugas/a.txt").status_code == 404
    assert not (tmp_path / "shared" / "files" / "tugas").exists()      # folder kosong ikut dibersihkan
    assert client.delete("/api/agent/shared/file", query_string={"host": "PC-01", "path": "tugas/a.txt"}, headers=AGENT).status_code == 200


def test_expect_sha_protects_against_overwriting_other_pcs_changes(client):
    setup(client)
    put(client, "a.txt")
    other = b"versi dari komputer lain"
    assert put(client, "a.txt", data=other, expect=SHA).status_code == 200          # sesuai harapan
    assert put(client, "a.txt", data=b"timpa buta", expect=SHA).status_code == 409   # sudah berubah
    assert put(client, "baru.txt", expect="none").status_code == 200
    assert put(client, "baru.txt", data=b"x", expect="none").status_code == 409      # ternyata sudah ada
    assert get(client, "a.txt").data == other
    q = {"host": "PC-01", "path": "a.txt", "expect": SHA}
    assert client.delete("/api/agent/shared/file", query_string=q, headers=AGENT).status_code == 409
    assert client.delete("/api/agent/shared/file", query_string={**q, "expect": hashlib.sha256(other).hexdigest()}, headers=AGENT).status_code == 200


def test_bad_paths_rejected_on_agent_endpoints(client):
    setup(client)
    for bad in ("../x", "a/../../x", "/etc/passwd", "a\\b", "con", ""):
        assert put(client, bad).status_code == 400, bad
        assert get(client, bad).status_code == 400, bad
    assert index(client).get_json()["files"] == {}


def test_limits(client, monkeypatch):
    setup(client)
    monkeypatch.setattr(shared, "MAX_FILE_BYTES", 10)
    assert put(client, "besar.bin", data=b"x" * 11).status_code == 413
    monkeypatch.setattr(shared, "MAX_FILE_BYTES", 1000)
    monkeypatch.setattr(shared, "MAX_TOTAL_BYTES", 30)
    assert put(client, "a", data=b"x" * 20).status_code == 200
    assert put(client, "b", data=b"x" * 20).status_code == 413
    assert put(client, "a", data=b"x" * 30).status_code == 200                  # menimpa: hanya selisih yang dihitung
    monkeypatch.setattr(shared, "MAX_TOTAL_BYTES", 10 ** 6)
    monkeypatch.setattr(shared, "MAX_FILES", 1)
    assert put(client, "c", data=b"x").status_code == 413
    assert client.delete("/api/agent/shared/file", query_string={"host": "PC-01", "path": "a"}, headers=AGENT).status_code == 200
    assert put(client, "c", data=b"x").status_code == 200


def test_names_clashing_by_case_or_folder_are_refused(client):
    setup(client)
    put(client, "Tugas/A.txt")
    assert put(client, "tugas/a.txt").status_code == 409                # Windows menganggapnya sama
    assert put(client, "Tugas").status_code == 409                      # file vs folder
    put(client, "berkas")
    assert put(client, "berkas/dalam.txt").status_code == 409
    assert put(client, "Tugas/B.txt").status_code == 200


# --- dashboard -------------------------------------------------------------------------------------
def test_admin_manages_files_and_sees_status(client):
    setup(client, pcs=("PC-01", "PC-02"))
    up = client.post("/api/shared/file", query_string={"path": "materi/bab1.pdf"}, data=DATA, headers=ADMIN)
    assert up.status_code == 200
    poll(client, shared={"ok": True, "files": 3, "error": ""})
    poll(client, host="PC-02", shared={"ok": False, "files": 0, "error": "tidak bisa membuat folder"})
    data = client.get("/api/shared", headers=ADMIN).get_json()
    assert data["files"][0]["path"] == "materi/bab1.pdf" and data["total_size"] == len(DATA)
    pcs = {p["id"]: p for p in data["pcs"]}
    assert pcs["PC-01"]["status"]["ok"] is True and pcs["PC-01"]["status"]["files"] == 3
    assert pcs["PC-02"]["status"]["error"] == "tidak bisa membuat folder"
    assert pcs["PC-01"]["mode"] is None and pcs["PC-01"]["effective"] == "both"
    download = client.get("/api/shared/file", query_string={"path": "materi/bab1.pdf"}, headers=ADMIN)
    assert download.data == DATA and "attachment" in download.headers["Content-Disposition"]
    assert get(client, "materi/bab1.pdf").data == DATA                          # agent juga bisa mengambilnya
    assert client.delete("/api/shared/file", query_string={"path": "materi/bab1.pdf"}, headers=ADMIN).status_code == 200
    assert client.get("/api/shared", headers=ADMIN).get_json()["files"] == []


def test_dashboard_endpoints_need_login(client):
    for method, path in (("get", "/api/shared"), ("put", "/api/shared/settings"), ("post", "/api/shared/file?path=a"),
                         ("delete", "/api/shared/file?path=a"), ("get", "/api/shared/file?path=a")):
        assert getattr(client, method)(path).status_code == 401, path
    assert client.get("/pembagian").status_code == 302
    assert client.get("/pembagian", headers=ADMIN).status_code == 200


def test_shared_mode_survives_windows_rename(client):
    setup(client)
    set_mode(client, "PC-01", "send")
    client.put("/api/pcs/PC-01/windows-name", json={"name": "LAB-09"}, headers=ADMIN)
    client.post("/api/agent/rename-result", json={"host": "PC-01", "name": "LAB-09", "ok": True}, headers=AGENT)
    poll(client, host="LAB-09")
    pcs = {p["id"]: p for p in client.get("/api/shared", headers=ADMIN).get_json()["pcs"]}
    assert list(pcs) == ["LAB-09"] and pcs["LAB-09"]["mode"] == "send"


def test_path_rules_match_agent():
    import sys
    from pathlib import Path
    agent_dir = Path(__file__).resolve().parents[2] / "agent"
    if not agent_dir.exists():
        pytest.skip("folder agent tidak ada")
    sys.path.insert(0, str(agent_dir))
    from labagent import sharedsync
    samples = ["a.txt", "a/b/c.txt", "", "/a", "a/", "../a", "a//b", "a\\b", "C:/a", "con", "nul.txt", "a.", "a ", "x" * 130,
               "desktop.ini", "~$a", "b.labtmp", "é.txt", "a?b", "d/d/d/d/d/d/d/d/d", None, 5]
    for text in samples:
        assert sharedsync.clean_path(text) == shared.clean_path(text), text
        assert sharedsync.clean_profile_path(text) == shared.clean_profile_path(text), text
    assert (sharedsync.MAX_FILE_BYTES, sharedsync.MAX_FILES, sharedsync.MAX_DEPTH) == (shared.MAX_FILE_BYTES, shared.MAX_FILES, shared.MAX_DEPTH)
