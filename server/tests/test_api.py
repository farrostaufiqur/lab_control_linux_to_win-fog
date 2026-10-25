import base64
import time

import pytest

from labserver.app import create_app
from labserver.config import Config

ADMIN = {
    "Authorization": "Basic " + base64.b64encode(b"admin:secret-pass").decode(),
    "X-Lab-Admin": "1",
}
AGENT = {"X-Agent-Token": "agent-token-1234567890"}


@pytest.fixture
def client(tmp_path):
    cfg = Config(
        lab_name="Lab Test", timezone="Asia/Jakarta", host="127.0.0.1", port=0,
        db_path=str(tmp_path / "t.db"), admin_user="admin", admin_password="secret-pass",
        agent_token="agent-token-1234567890", poll_interval=5, online_timeout=20,
        command_ttl=120, event_retention_days=30, warn_seconds=300,
    )
    return create_app(cfg).test_client()


def poll(client, host="PC-01", **extra):
    body = {"host": host, "user": "siswa", "app": "chrome.exe", "title": "Google", "locked": False}
    body.update(extra)
    return client.post("/api/agent/poll", json=body, headers=AGENT)


def test_agent_needs_token(client):
    assert client.post("/api/agent/poll", json={"host": "PC-01"}).status_code == 403
    bad = {"X-Agent-Token": "nope"}
    assert client.post("/api/agent/poll", json={"host": "PC-01"}, headers=bad).status_code == 403


def test_admin_needs_login_and_csrf_header(client):
    assert client.get("/api/pcs").status_code == 401
    no_header = {"Authorization": ADMIN["Authorization"]}
    assert client.get("/api/pcs", headers=no_header).status_code == 200
    assert client.post("/api/all/lock", headers=no_header).status_code == 400
    assert client.post("/api/all/lock", headers=ADMIN).status_code == 200


def test_new_pc_registers_and_is_unlocked_when_schedule_off(client):
    reply = poll(client).get_json()
    assert reply["locked"] is False and reply["lab_name"] == "Lab Test"
    pcs = client.get("/api/pcs", headers=ADMIN).get_json()["pcs"]
    assert [p["id"] for p in pcs] == ["PC-01"]
    assert pcs[0]["online"] and pcs[0]["user"] == "siswa"


def test_invalid_host_rejected(client):
    assert poll(client, host="../etc/passwd").status_code == 400


def test_lock_and_unlock_single_pc_with_timer(client):
    poll(client)
    assert client.post("/api/pcs/PC-01/lock", json={"minutes": 30}, headers=ADMIN).status_code == 200
    reply = poll(client).get_json()
    assert reply["locked"] is True
    assert 1790 <= reply["changes_in"] <= 1800
    assert "dikunci oleh admin" in reply["message"]

    client.post("/api/pcs/PC-01/clear", headers=ADMIN)
    assert poll(client).get_json()["locked"] is False


def test_lock_all_then_other_pc_is_locked_and_unlock_all_resets(client):
    poll(client, "PC-01")
    poll(client, "PC-02")
    client.post("/api/all/lock", json={}, headers=ADMIN)
    assert poll(client, "PC-01").get_json()["locked"]
    assert poll(client, "PC-02").get_json()["locked"]
    client.post("/api/all/unlock", json={"minutes": 60}, headers=ADMIN)
    assert not poll(client, "PC-01").get_json()["locked"]


def test_schedule_enabled_without_windows_locks_and_disabling_unlocks(client):
    poll(client)
    assert client.put("/api/schedule", json={"enabled": True, "rules": []}, headers=ADMIN).status_code == 200
    reply = poll(client).get_json()
    assert reply["locked"] is True and "di luar jadwal" in reply["message"].lower()
    client.put("/api/schedule", json={"enabled": False, "rules": []}, headers=ADMIN)
    assert poll(client).get_json()["locked"] is False


def test_schedule_rejects_bad_rules_and_roundtrips_good_ones(client):
    assert client.put("/api/schedule", json={"rules": [{"days": [9]}]}, headers=ADMIN).status_code == 400
    rules = [
        {"type": "weekly", "days": [0, 1], "start": "08:00", "end": "16:00"},
        {"type": "daily", "start": "18:00", "end": "20:00"},
        {"type": "once", "date": "2026-12-25", "start": "09:00", "end": "12:00"},
    ]
    client.put("/api/schedule", json={"enabled": True, "rules": rules}, headers=ADMIN)
    assert client.get("/api/schedule", headers=ADMIN).get_json() == {"enabled": True, "rules": rules}
    legacy = [{"days": [0], "start": "08:00", "end": "09:00"}]  # old format without "type"
    assert client.put("/api/schedule", json={"enabled": True, "rules": legacy}, headers=ADMIN).status_code == 200
    stored = client.get("/api/schedule", headers=ADMIN).get_json()["rules"]
    assert stored == [{"type": "weekly", "days": [0], "start": "08:00", "end": "09:00"}]
    for bad in ({"type": "once", "date": "2026-13-40"}, {"type": "monthly"}, {"type": "once"}):
        body = {"rules": [{"start": "08:00", "end": "09:00", **bad}]}
        assert client.put("/api/schedule", json=body, headers=ADMIN).status_code == 400


def test_commands_are_delivered_once(client):
    poll(client)
    assert client.post("/api/pcs/PC-01/command", json={"action": "restart"}, headers=ADMIN).status_code == 200
    assert client.post("/api/pcs/PC-01/command", json={"action": "rm -rf"}, headers=ADMIN).status_code == 400
    assert poll(client).get_json()["commands"] == ["restart"]
    assert poll(client).get_json()["commands"] == []


def test_events_are_stored_deduplicated_and_filtered(client):
    events = [
        {"eid": "a1", "ts": time.time() - 5, "type": "app", "detail": "chrome.exe | Google"},
        {"eid": "a2", "ts": time.time() - 4, "type": "lock", "detail": "terkunci"},
        {"eid": "a3", "ts": time.time(), "type": "bogus", "detail": "ignored"},
    ]
    poll(client, events=events)
    poll(client, events=events)  # agent retry after a lost response
    poll(client, "PC-02", events=[{"eid": "b1", "ts": time.time(), "type": "app", "detail": "x"}])
    rows = client.get("/api/events?pc=PC-01", headers=ADMIN).get_json()
    details = [r["detail"] for r in rows]
    assert details.count("chrome.exe | Google") == 1
    assert "ignored" not in details
    assert all(r["pc"] == "PC-01" for r in rows)


def test_future_event_timestamps_are_clamped(client):
    poll(client, events=[{"eid": "f", "ts": time.time() + 99999, "type": "app", "detail": "x"}])
    rows = client.get("/api/events?pc=PC-01", headers=ADMIN).get_json()
    assert max(r["ts"] for r in rows) <= time.time() + 1


def test_delete_pc(client):
    poll(client)
    client.delete("/api/pcs/PC-01", headers=ADMIN)
    assert client.get("/api/pcs", headers=ADMIN).get_json()["pcs"] == []
    assert client.post("/api/pcs/PC-01/lock", json={}, headers=ADMIN).status_code == 404


# --- access through Cloudflare Tunnel ----------------------------------------
TUNNEL = {"Cf-Connecting-Ip": "203.0.113.9", "Cf-Ray": "abc123"}


def test_agent_api_is_refused_when_request_comes_through_tunnel(client):
    body = {"host": "PC-01"}
    assert client.post("/api/agent/poll", json=body, headers={**AGENT, **TUNNEL}).status_code == 404
    assert client.post("/api/agent/poll", json=body, headers=AGENT).status_code == 200  # LAN still works


def test_dashboard_works_through_tunnel(client):
    assert client.get("/api/pcs", headers={**ADMIN, **TUNNEL}).status_code == 200


def test_healthz_is_public(client):
    assert client.get("/healthz").get_json() == {"ok": True}


def bad_login(client, extra=None):
    headers = {"Authorization": "Basic " + base64.b64encode(b"admin:wrong").decode(), **(extra or {})}
    return client.get("/api/pcs", headers=headers).status_code


def test_repeated_wrong_passwords_block_that_visitor_only(client):
    attacker = {"Cf-Connecting-Ip": "198.51.100.7"}
    assert [bad_login(client, attacker) for _ in range(5)] == [401] * 5
    assert bad_login(client, attacker) == 429
    good_but_blocked = {**ADMIN, **attacker}
    assert client.get("/api/pcs", headers=good_but_blocked).status_code == 429
    # another visitor, and the LAN, are unaffected
    assert client.get("/api/pcs", headers={**ADMIN, "Cf-Connecting-Ip": "192.0.2.1"}).status_code == 200
    assert client.get("/api/pcs", headers=ADMIN).status_code == 200


def test_successful_login_resets_failure_count(client):
    for _ in range(4):
        bad_login(client)
    assert client.get("/api/pcs", headers=ADMIN).status_code == 200
    for _ in range(4):
        assert bad_login(client) == 401


def test_timezone_is_selectable_and_changes_schedule(client):
    zones = client.get("/api/timezones", headers=ADMIN).get_json()
    assert zones["current"] == "Asia/Jakarta" and "Asia/Makassar" in zones["zones"]
    rules = [{"type": "daily", "start": "00:00", "end": "23:59"}]
    client.put("/api/schedule", json={"enabled": True, "rules": rules}, headers=ADMIN)
    assert client.put("/api/timezone", json={"timezone": "Not/AZone"}, headers=ADMIN).status_code == 400
    assert client.put("/api/timezone", json={"timezone": "../etc/passwd"}, headers=ADMIN).status_code == 400
    assert client.put("/api/timezone", json={"timezone": "Asia/Jayapura"}, headers=ADMIN).status_code == 200
    data = client.get("/api/pcs", headers=ADMIN).get_json()
    assert data["timezone"] == "Asia/Jayapura"


def test_message_shows_only_while_locked_and_can_expire_or_be_cleared(client):
    poll(client)
    poll(client, host="PC-02")
    client.post("/api/all/lock", json={}, headers=ADMIN)
    assert poll(client).get_json()["notice"] == ""

    assert client.post("/api/pcs/PC-01/message", json={"text": ""}, headers=ADMIN).status_code == 400
    assert client.post("/api/pcs/PC-01/message", json={"text": "x" * 301}, headers=ADMIN).status_code == 400
    assert client.post("/api/pcs/NOPE/message", json={"text": "hai"}, headers=ADMIN).status_code == 404

    client.post("/api/pcs/PC-01/message", json={"text": "Ke ruang guru"}, headers=ADMIN)
    assert poll(client).get_json()["notice"] == "Ke ruang guru"
    assert poll(client, host="PC-02").get_json()["notice"] == ""

    client.post("/api/all/message", json={"text": "Lab tutup lebih awal", "minutes": 30}, headers=ADMIN)
    assert poll(client).get_json()["notice"] == "Ke ruang guru"          # per-PC beats the global one
    assert poll(client, host="PC-02").get_json()["notice"] == "Lab tutup lebih awal"

    client.delete("/api/pcs/PC-01/message", headers=ADMIN)
    assert poll(client).get_json()["notice"] == "Lab tutup lebih awal"
    client.delete("/api/all/message", headers=ADMIN)
    assert poll(client).get_json()["notice"] == ""

    client.post("/api/all/unlock", json={}, headers=ADMIN)
    client.post("/api/pcs/PC-01/message", json={"text": "tidak terlihat"}, headers=ADMIN)
    assert poll(client).get_json()["notice"] == ""                        # unlocked: nothing to show it on


def test_expired_message_is_not_delivered(client, monkeypatch):
    poll(client)
    client.post("/api/all/lock", json={}, headers=ADMIN)
    client.post("/api/pcs/PC-01/message", json={"text": "sebentar", "minutes": 1}, headers=ADMIN)
    assert poll(client).get_json()["notice"] == "sebentar"
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 120)
    assert poll(client).get_json()["notice"] == ""


def test_shutdown_targets_only_the_chosen_pc(client):
    poll(client)
    poll(client, host="PC-02")
    client.post("/api/pcs/PC-02/command", json={"action": "shutdown"}, headers=ADMIN)
    assert poll(client).get_json()["commands"] == []
    assert poll(client, host="PC-02").get_json()["commands"] == ["shutdown"]


def test_server_restart_needs_admin_and_systemd(client, monkeypatch):
    called = []
    client.application.config["EXIT_FN"] = lambda: called.append(1)
    assert client.post("/api/server/restart").status_code == 401
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    assert client.post("/api/server/restart", headers=ADMIN).status_code == 409
    assert called == []
    monkeypatch.setenv("INVOCATION_ID", "abc")
    assert client.post("/api/server/restart", headers=ADMIN).status_code == 200
    import time
    time.sleep(0.05)
    assert called == [1]


def test_rename_pc_display_name(client):
    poll(client)
    assert client.put("/api/pcs/PC-01/name", json={"name": "  Meja 1  "}, headers=ADMIN).get_json()["name"] == "Meja 1"
    poll(client)  # laporan berikutnya tidak menghapus nama
    pc = client.get("/api/pcs", headers=ADMIN).get_json()["pcs"][0]
    assert pc["id"] == "PC-01" and pc["name"] == "Meja 1"
    client.put("/api/pcs/PC-01/name", json={"name": ""}, headers=ADMIN)
    assert client.get("/api/pcs", headers=ADMIN).get_json()["pcs"][0]["name"] == "PC-01"


def test_rename_validation(client):
    poll(client)
    assert client.put("/api/pcs/PC-01/name", json={"name": "x" * 41}, headers=ADMIN).status_code == 400
    assert client.put("/api/pcs/PC-01/name", json={"name": "a\nb"}, headers=ADMIN).status_code == 400
    assert client.put("/api/pcs/NOPE/name", json={"name": "a"}, headers=ADMIN).status_code == 404
    assert client.put("/api/pcs/PC-01/name", json={"name": "a"}).status_code == 401


def rename_check(client, host="PC-01"):
    return client.post("/api/agent/release", json={"host": host, "sha": "0" * 64}, headers=AGENT).get_json()["rename"]


def rename_result(client, name, ok=True, host="PC-01", error=""):
    return client.post("/api/agent/rename-result", json={"host": host, "name": name, "ok": ok, "error": error}, headers=AGENT)


def test_windows_rename_full_flow_keeps_settings_and_history(client):
    poll(client)
    client.put("/api/pcs/PC-01/name", json={"name": "Meja 1"}, headers=ADMIN)
    client.post("/api/pcs/PC-01/lock", json={}, headers=ADMIN)
    assert rename_check(client) is None
    assert client.put("/api/pcs/PC-01/windows-name", json={"name": "lab-01", "restart": True}, headers=ADMIN).get_json()["name"] == "LAB-01"
    assert rename_check(client) == {"name": "LAB-01"}
    pc = client.get("/api/pcs", headers=ADMIN).get_json()["pcs"][0]
    assert pc["rename"] == {"name": "LAB-01", "state": "pending", "error": None}

    assert rename_result(client, "LAB-01").status_code == 200
    assert rename_check(client) is None                      # tidak ditawarkan lagi
    assert rename_result(client, "LAB-01").status_code == 200  # hasil ganda diabaikan
    assert poll(client).get_json()["commands"] == ["restart"]  # restart diminta sekali saja
    assert poll(client).get_json()["commands"] == []

    reply = poll(client, host="LAB-01").get_json()            # setelah restart PC melapor dengan nama baru
    assert reply["locked"] is True                            # override kunci ikut pindah
    pcs = client.get("/api/pcs", headers=ADMIN).get_json()["pcs"]
    assert [p["id"] for p in pcs] == ["LAB-01"] and pcs[0]["name"] == "Meja 1" and pcs[0]["rename"] is None
    details = [e["detail"] for e in client.get("/api/events?pc=LAB-01", headers=ADMIN).get_json()]
    assert any("Nama Windows diganti dari PC-01" in d for d in details)
    assert any("Admin meminta nama Windows" in d for d in details)  # log lama ikut pindah


def test_windows_rename_failure_is_not_retried(client):
    poll(client)
    client.put("/api/pcs/PC-01/windows-name", json={"name": "LAB-02"}, headers=ADMIN)
    rename_result(client, "LAB-02", ok=False, error="Access is denied")
    assert rename_check(client) is None
    pc = client.get("/api/pcs", headers=ADMIN).get_json()["pcs"][0]
    assert pc["rename"]["state"] == "failed" and pc["rename"]["error"] == "Access is denied"
    client.put("/api/pcs/PC-01/windows-name", json={"name": "LAB-02"}, headers=ADMIN)   # admin mencoba lagi
    assert rename_check(client) == {"name": "LAB-02"}


def test_windows_rename_validation_and_cancel(client):
    poll(client); poll(client, host="PC-02")
    put = lambda name, pc="PC-01": client.put(f"/api/pcs/{pc}/windows-name", json={"name": name}, headers=ADMIN)
    for bad in ("", "a" * 16, "-abc", "abc-", "12345", "a b", "a_b", "a.b", "PC-01"):
        assert put(bad).status_code == 400, bad
    assert put("pc-02").status_code == 409                    # sudah dipakai PC lain (tanpa peduli huruf besar)
    assert put("LAB-05").status_code == 200
    assert put("LAB-05", "PC-02").status_code == 409          # sudah diminta PC lain
    assert client.put("/api/pcs/NOPE/windows-name", json={"name": "X"}, headers=ADMIN).status_code == 404
    assert client.put("/api/pcs/PC-01/windows-name", json={"name": "X"}).status_code == 401
    assert client.delete("/api/pcs/PC-01/windows-name", headers=ADMIN).status_code == 200
    assert rename_check(client) is None


def test_rename_result_needs_token_and_matching_request(client):
    poll(client)
    assert client.post("/api/agent/rename-result", json={"host": "PC-01", "name": "X1", "ok": True}).status_code == 403
    client.put("/api/pcs/PC-01/windows-name", json={"name": "LAB-03"}, headers=ADMIN)
    rename_result(client, "LAB-99")                            # nama tidak cocok: diabaikan
    assert rename_check(client) == {"name": "LAB-03"}
    assert rename_result(client, "bad name").status_code == 400


# --- halaman dashboard -------------------------------------------------------------
PAGES = ["/", "/pc/PC-01", "/jadwal", "/log", "/pembaruan", "/pengaturan"]


@pytest.mark.parametrize("path", PAGES)
def test_pages_need_login_and_render_with_sidebar(client, path):
    redirect = client.get(path)
    assert redirect.status_code == 302 and redirect.headers["Location"].startswith("/login?next=")
    page = client.get(path, headers=ADMIN)
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "Lab Test" in html and all(f'href="{link}"' in html for link in ("/jadwal", "/log", "/pembaruan", "/pengaturan"))


def test_static_assets_are_served_without_login(client):
    for path in ("/static/lab.js", "/static/lab.css", "/static/vendor/src/bootstrap/css/bootstrap.min.css"):
        assert client.get(path).status_code == 200, path


def test_pc_page_escapes_id_in_script(client):
    html = client.get("/pc/%3Cscript%3Ealert(1)", headers=ADMIN).get_data(as_text=True)
    assert "<script>alert(1)" not in html and "\\u003cscript" in html


# --- screenshot berkala ---------------------------------------------------------------
JPEG = b"\xff\xd8\xff\xe0" + b"x" * 500


def shot(client, host="PC-01", data=JPEG, headers=AGENT):
    return client.post(f"/api/agent/screenshot?host={host}", data=data, headers={**headers, "Content-Type": "image/jpeg"})


def pc(client, pc_id="PC-01"):
    return next(p for p in client.get("/api/pcs", headers=ADMIN).get_json()["pcs"] if p["id"] == pc_id)


def test_poll_tells_agent_the_screenshot_interval(client):
    assert poll(client).get_json()["screenshot_interval"] == 10
    assert client.put("/api/settings", json={"screenshot_interval": 60}, headers=ADMIN).status_code == 200
    assert poll(client).get_json()["screenshot_interval"] == 60
    for bad in (3, -1, "x", None):
        assert client.put("/api/settings", json={"screenshot_interval": bad}, headers=ADMIN).status_code == 400, bad


def test_screenshot_upload_and_view(client):
    poll(client)
    assert pc(client)["shot"] is None
    assert client.get("/api/pcs/PC-01/screenshot", headers=ADMIN).status_code == 404
    assert shot(client).get_json()["stored"] is True
    assert pc(client)["shot"] is not None
    assert client.get("/api/pcs/PC-01/screenshot").status_code == 401
    image = client.get("/api/pcs/PC-01/screenshot", headers=ADMIN)
    assert image.status_code == 200 and image.data == JPEG and image.mimetype == "image/jpeg"


def test_screenshot_upload_is_validated(client):
    poll(client)
    assert shot(client, headers={}).status_code == 403
    assert shot(client, host="PC-99").status_code == 404           # PC belum terdaftar
    assert shot(client, host="../x").status_code == 404
    assert shot(client, data=b"<html>bukan gambar").status_code == 400
    assert shot(client, data=JPEG + b"x" * (1536 * 1024)).status_code == 413
    assert pc(client)["shot"] is None


def test_screenshot_hidden_while_locked(client):
    poll(client)
    shot(client)
    client.post("/api/pcs/PC-01/lock", json={}, headers=ADMIN)
    assert pc(client)["shot"] is None
    client.post("/api/pcs/PC-01/unlock", json={}, headers=ADMIN)
    poll(client, locked=True)                                      # agent masih menampilkan layar kunci
    assert pc(client)["shot"] is None
    poll(client, locked=False)
    assert pc(client)["shot"] is not None


def test_turning_screenshots_off_deletes_them(client, tmp_path):
    poll(client)
    shot(client)
    assert (tmp_path / "screenshots" / "PC-01.jpg").is_file()
    client.put("/api/settings", json={"screenshot_interval": 0}, headers=ADMIN)
    assert not (tmp_path / "screenshots" / "PC-01.jpg").exists() and pc(client)["shot"] is None
    assert shot(client).get_json()["stored"] is False              # agent lama yang masih mengirim: dibuang
    assert pc(client)["shot"] is None and poll(client).get_json()["screenshot_interval"] == 0


def test_deleting_pc_removes_its_screenshot(client, tmp_path):
    poll(client)
    shot(client)
    client.delete("/api/pcs/PC-01", headers=ADMIN)
    assert not (tmp_path / "screenshots" / "PC-01.jpg").exists()


# --- alamat IP dan agent lewat tunnel -------------------------------------------------
def test_agent_reported_lan_ip_is_shown(client):
    poll(client, lan_ip="192.168.1.77")
    assert pc(client)["ip"] == "192.168.1.77"
    for bad in ("bukan-ip", "127.0.0.1", "0.0.0.0", "<b>", None):
        poll(client, lan_ip=bad)
        assert pc(client)["ip"] == "127.0.0.1", bad                # alamat koneksi (klien tes)


def test_agent_allowed_through_tunnel_when_configured(tmp_path):
    cfg = Config(
        lab_name="Lab Test", timezone="Asia/Jakarta", host="127.0.0.1", port=0,
        db_path=str(tmp_path / "t.db"), admin_user="admin", admin_password="secret-pass",
        agent_token="agent-token-1234567890", poll_interval=5, online_timeout=20,
        command_ttl=120, event_retention_days=30, warn_seconds=300, block_agent_via_tunnel=False,
    )
    client = create_app(cfg).test_client()
    assert poll_with(client, {**AGENT, **TUNNEL}).status_code == 200
    assert poll_with(client, TUNNEL).status_code == 403            # token tetap wajib
    assert pc(client)["ip"] == TUNNEL["Cf-Connecting-Ip"]          # tanpa lan_ip: alamat publik pengirim
    assert client.get("/api/settings", headers=ADMIN).get_json()["agent_via_tunnel"] is True


def poll_with(client, headers):
    return client.post("/api/agent/poll", json={"host": "PC-01"}, headers=headers,
                       environ_base={"REMOTE_ADDR": "127.0.0.1"})


# --- halaman login (form + sesi, bukan prompt Basic) ---------------------------------
def login(client, password="secret-pass", user="admin", **extra):
    return client.post("/login", data={"username": user, "password": password, **extra})


def test_login_page_is_public_and_has_no_basic_challenge(client):
    page = client.get("/login")
    assert page.status_code == 200 and 'name="password"' in page.get_data(as_text=True)
    assert "WWW-Authenticate" not in page.headers
    assert "WWW-Authenticate" not in client.get("/api/pcs").headers     # tidak memunculkan prompt browser
    assert "WWW-Authenticate" not in client.get("/").headers


def test_login_sets_session_that_works_for_pages_and_api(client):
    assert login(client).status_code == 302
    assert client.get("/").status_code == 200
    assert client.get("/api/pcs").status_code == 200
    cookie = client.get_cookie("labsession")
    assert cookie is not None and cookie.http_only and cookie.same_site == "Lax"
    assert client.post("/api/all/clear", headers={"X-Lab-Admin": "1"}).status_code == 200
    assert client.post("/api/all/clear").status_code == 400            # sesi pun tetap butuh header anti-CSRF


def test_wrong_login_rejected_and_rate_limited(client):
    for _ in range(5):
        assert login(client, password="salah").status_code == 401
    assert login(client, password="salah").status_code == 429
    assert login(client).status_code == 429                              # IP diblokir, walau password benar
    assert client.get("/api/pcs").status_code in (401, 429)


def test_login_redirects_to_next_but_never_off_site(client):
    assert login(client, next="/pc/PC-01").headers["Location"] == "/pc/PC-01"
    for evil in ("https://evil.example", "//evil.example", "/\\evil.example", "pc"):
        assert login(client, next=evil).headers["Location"] == "/", evil


def test_unauthenticated_page_redirect_keeps_target(client):
    assert client.get("/pc/PC-01").headers["Location"] == "/login?next=/pc/PC-01"


def test_logout_ends_session(client):
    login(client)
    assert client.get("/logout").status_code == 302
    assert client.get("/api/pcs").status_code == 401


def test_session_dies_when_admin_password_changes(tmp_path):
    def make(password):
        cfg = Config(
            lab_name="Lab Test", timezone="Asia/Jakarta", host="127.0.0.1", port=0,
            db_path=str(tmp_path / "t.db"), admin_user="admin", admin_password=password,
            agent_token="agent-token-1234567890", poll_interval=5, online_timeout=20,
            command_ttl=120, event_retention_days=30, warn_seconds=300,
        )
        return create_app(cfg).test_client()
    first = make("secret-pass")
    login(first)
    assert first.get("/api/pcs").status_code == 200
    restarted = make("secret-pass")                       # kunci sesi tersimpan: restart tidak mengeluarkan siapa pun
    restarted.set_cookie("labsession", first.get_cookie("labsession").value)
    assert restarted.get("/api/pcs").status_code == 200
    changed = make("password-baru-123")
    changed.set_cookie("labsession", first.get_cookie("labsession").value)
    assert changed.get("/api/pcs").status_code == 401


def test_session_cookie_secure_only_over_https(client):
    login(client)
    assert not client.get_cookie("labsession").secure
    other = client.application.test_client()
    other.post("/login", data={"username": "admin", "password": "secret-pass"}, headers={"X-Forwarded-Proto": "https"})
    assert other.get_cookie("labsession", ).secure


def test_basic_auth_still_works_for_scripts(client):
    assert client.get("/api/pcs", headers=ADMIN).status_code == 200


# --- restart/matikan semua komputer -------------------------------------------------------
def test_all_command_only_reaches_online_pcs(client, monkeypatch):
    poll(client, host="PC-01")
    poll(client, host="PC-02")
    import labserver.app as app_module
    real = time.time
    monkeypatch.setattr(app_module.time, "time", lambda: real() + 3600)     # semua PC tampak offline
    poll_after = lambda host: client.post("/api/agent/poll", json={"host": host, "user": "u"}, headers=AGENT)
    poll_after("PC-02")                                                     # hanya PC-02 yang online
    reply = client.post("/api/all/command", json={"action": "restart"}, headers=ADMIN).get_json()
    assert reply == {"ok": True, "count": 1}
    assert poll_after("PC-02").get_json()["commands"] == ["restart"]
    assert poll_after("PC-01").get_json()["commands"] == []


def test_all_command_validation_and_auth(client):
    poll(client)
    assert client.post("/api/all/command", json={"action": "format"}, headers=ADMIN).status_code == 400
    assert client.post("/api/all/command", json={}, headers=ADMIN).status_code == 400
    assert client.post("/api/all/command", json={"action": "shutdown"}).status_code == 401
    assert client.post("/api/all/command", json={"action": "shutdown"}, headers=ADMIN).get_json()["count"] == 1
    assert poll(client).get_json()["commands"] == ["shutdown"]
    details = [e["detail"] for e in client.get("/api/events", headers=ADMIN).get_json()]
    assert any("shutdown ke semua komputer online (1" in d for d in details)


# --- perintah shell ke PC ------------------------------------------------------------
def next_job(client, host="PC-01"):
    return client.post("/api/agent/jobs/next", json={"host": host}, headers=AGENT).get_json()["job"]


def test_exec_flow_from_queue_to_result(client):
    poll(client)
    made = client.post("/api/pcs/PC-01/exec", json={"shell": "powershell", "script": "Get-Date", "timeout": 30}, headers=ADMIN)
    assert made.status_code == 200
    job_id = made.get_json()["id"]
    job = next_job(client)
    assert job == {"id": job_id, "shell": "powershell", "script": "Get-Date", "timeout": 30}
    assert next_job(client) is None                       # sudah diserahkan: tidak diulang
    done = client.post("/api/agent/jobs/result", headers=AGENT,
                       json={"host": "PC-01", "id": job_id, "exit_code": 0, "output": "Senin", "timed_out": False})
    assert done.status_code == 200
    row = client.get("/api/jobs", headers=ADMIN).get_json()[0]
    assert (row["state"], row["exit_code"], row["output"]) == ("done", 0, "Senin")
    details = [e["detail"] for e in client.get("/api/events?pc=PC-01", headers=ADMIN).get_json()]
    assert any("menjalankan perintah" in d for d in details) and any("selesai" in d for d in details)


def test_exec_failure_and_timeout_states(client):
    poll(client)
    for exit_code, timed_out, state in ((3, False, "failed"), (None, True, "timeout")):
        job_id = client.post("/api/pcs/PC-01/exec", json={"script": "x"}, headers=ADMIN).get_json()["id"]
        assert next_job(client)["id"] == job_id
        client.post("/api/agent/jobs/result", headers=AGENT,
                    json={"host": "PC-01", "id": job_id, "exit_code": exit_code, "output": "", "timed_out": timed_out})
        assert client.get("/api/jobs?pc=PC-01&limit=1", headers=ADMIN).get_json()[0]["state"] == state


def test_exec_validation_and_auth(client):
    poll(client)
    assert client.post("/api/pcs/PC-01/exec", json={"script": "dir"}).status_code == 401
    for bad in ({"script": ""}, {"script": "  "}, {"script": "x", "shell": "bash"}, {"script": "x" * 4001},
                {"script": "x", "timeout": "abc"}):
        assert client.post("/api/pcs/PC-01/exec", json=bad, headers=ADMIN).status_code == 400
    assert client.post("/api/pcs/NOPE/exec", json={"script": "dir"}, headers=ADMIN).status_code == 404
    capped = client.post("/api/pcs/PC-01/exec", json={"script": "dir", "timeout": 99999}, headers=ADMIN).get_json()["id"]
    assert next_job(client)["timeout"] == 900 and capped
    assert client.post("/api/agent/jobs/next", json={"host": "PC-01"}).status_code == 403
    assert client.post("/api/agent/jobs/result", json={"host": "PC-01", "id": 1}).status_code == 403


def test_exec_result_only_from_owner_and_only_once(client):
    poll(client); poll(client, host="PC-02")
    job_id = client.post("/api/pcs/PC-01/exec", json={"script": "dir"}, headers=ADMIN).get_json()["id"]
    assert next_job(client, "PC-02") is None                # perintah PC-01 tidak bocor ke PC lain
    next_job(client)
    client.post("/api/agent/jobs/result", headers=AGENT, json={"host": "PC-02", "id": job_id, "exit_code": 0, "output": "palsu"})
    assert client.get("/api/jobs", headers=ADMIN).get_json()[0]["state"] == "running"
    ok = {"host": "PC-01", "id": job_id, "exit_code": 0, "output": "asli"}
    client.post("/api/agent/jobs/result", headers=AGENT, json=ok)
    client.post("/api/agent/jobs/result", headers=AGENT, json={**ok, "output": "timpa", "exit_code": 9})
    row = client.get("/api/jobs", headers=ADMIN).get_json()[0]
    assert (row["output"], row["exit_code"]) == ("asli", 0)


def test_exec_runs_one_at_a_time_in_order(client):
    poll(client)
    first = client.post("/api/pcs/PC-01/exec", json={"script": "a"}, headers=ADMIN).get_json()["id"]
    second = client.post("/api/pcs/PC-01/exec", json={"script": "b"}, headers=ADMIN).get_json()["id"]
    assert next_job(client)["id"] == first
    assert next_job(client) is None                         # menunggu yang pertama melapor
    client.post("/api/agent/jobs/result", headers=AGENT, json={"host": "PC-01", "id": first, "exit_code": 0, "output": ""})
    assert next_job(client)["id"] == second


def test_exec_cancel_pending_only(client):
    poll(client)
    pending = client.post("/api/pcs/PC-01/exec", json={"script": "a"}, headers=ADMIN).get_json()["id"]
    assert client.post(f"/api/jobs/{pending}/cancel", headers=ADMIN).status_code == 200
    assert next_job(client) is None
    assert client.post(f"/api/jobs/{pending}/cancel", headers=ADMIN).status_code == 409


def test_exec_expires_when_pc_was_offline_too_long(client, monkeypatch):
    from labserver import app as app_module
    poll(client)
    client.post("/api/pcs/PC-01/exec", json={"script": "a"}, headers=ADMIN)
    real = time.time
    monkeypatch.setattr(app_module.time, "time", lambda: real() + app_module.JOB_PENDING_TTL + 5)
    assert next_job(client) is None
    assert client.get("/api/jobs", headers=ADMIN).get_json()[0]["state"] == "expired"


def test_exec_all_targets_online_pcs_only(client):
    poll(client); poll(client, host="PC-02")
    assert client.post("/api/all/exec", json={"script": "hostname"}, headers=ADMIN).get_json()["count"] == 2
    assert next_job(client, "PC-01")["script"] == "hostname" and next_job(client, "PC-02")["script"] == "hostname"


def test_exec_output_is_capped_and_unknown_host_gets_nothing(client):
    poll(client)
    job_id = client.post("/api/pcs/PC-01/exec", json={"script": "a"}, headers=ADMIN).get_json()["id"]
    next_job(client)
    client.post("/api/agent/jobs/result", headers=AGENT,
                json={"host": "PC-01", "id": job_id, "exit_code": 0, "output": "x" * 100000})
    assert len(client.get("/api/jobs", headers=ADMIN).get_json()[0]["output"]) == 16000
    assert next_job(client, "TIDAK-ADA") is None


def test_exec_history_follows_pc_deletion(client):
    poll(client)
    client.post("/api/pcs/PC-01/exec", json={"script": "a"}, headers=ADMIN)
    client.delete("/api/pcs/PC-01", headers=ADMIN)
    assert client.get("/api/jobs", headers=ADMIN).get_json() == []


def test_commands_page_renders(client):
    assert client.get("/perintah", headers=ADMIN).status_code == 200
