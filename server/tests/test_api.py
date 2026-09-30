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
