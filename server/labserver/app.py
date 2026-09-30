import json
import os
import re
import secrets
import threading
import time
from pathlib import Path
from zoneinfo import ZoneInfo, available_timezones

from flask import Flask, Response, abort, jsonify, request, send_from_directory

from . import policy
from .config import Config
from .db import Store

HOST_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
EVENT_TYPES = {"agent_start", "app", "lock", "unlock", "user_action", "command", "info"}
COMMANDS = {"shutdown", "restart"}
MAX_EVENTS_PER_POLL = 200
MAX_OVERRIDE_MINUTES = 60 * 24 * 7
MAX_NOTICE_CHARS = 300
ALL_ZONES = frozenset(available_timezones())


def _text(value, limit):
    return None if value is None else str(value)[:limit]


def _int(value, low=0, high=10**9):
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return None


def create_app(cfg: Config) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.config["MAX_CONTENT_LENGTH"] = 256 * 1024
    store = Store(cfg.db_path)

    # helpers -------------------------------------------------------------
    def timezone_name():
        name = store.get_setting("timezone", cfg.timezone)
        return name if name in ALL_ZONES else cfg.timezone

    def get_tz():
        return ZoneInfo(timezone_name())

    def schedule():
        return store.get_setting("schedule", {"enabled": False, "rules": []})

    def global_override():
        return store.get_setting("global_override", None)

    def state_for(pc_row, now):
        sched = schedule()
        override = json.loads(pc_row["override"]) if pc_row is not None and pc_row["override"] else None
        state = policy.evaluate(now, override, global_override(), sched["enabled"], sched["rules"], get_tz())
        state["override"] = override
        return state

    failures: dict[str, tuple[int, float]] = {}   # ip -> (failed attempts, blocked_until)
    failures_lock = threading.Lock()
    loopback = {"127.0.0.1", "::1"}

    def via_tunnel():
        return "Cf-Connecting-Ip" in request.headers or "Cf-Ray" in request.headers

    def client_ip():
        # cloudflared connects from localhost; the real visitor is in this header
        forwarded = request.headers.get("Cf-Connecting-Ip")
        if forwarded and request.remote_addr in loopback:
            return forwarded
        return request.remote_addr or "?"

    def check_login_block(ip, now):
        with failures_lock:
            count, blocked_until = failures.get(ip, (0, 0.0))
            if blocked_until > now:
                retry = str(int(blocked_until - now) + 1)
                abort(Response("Terlalu banyak percobaan login. Coba lagi nanti.", 429, {"Retry-After": retry}))

    def record_login(ip, ok, now):
        with failures_lock:
            if ok:
                failures.pop(ip, None)
                return
            count, blocked_until = failures.get(ip, (0, 0.0))
            if blocked_until and blocked_until <= now:
                count = 0                      # block expired: start counting afresh
            count += 1
            blocked_until = now + cfg.login_block_seconds if count >= cfg.max_login_failures else 0.0
            failures[ip] = (count, blocked_until)
            if len(failures) > 1000:           # never let an attacker grow this without bound
                for key in [k for k, (_, until) in failures.items() if until <= now][:500]:
                    failures.pop(key, None)

    def admin_only():
        auth = request.authorization
        ip, now = client_ip(), time.time()
        check_login_block(ip, now)
        ok = (
            auth is not None
            and auth.type == "basic"
            and secrets.compare_digest(auth.username or "", cfg.admin_user)
            and secrets.compare_digest(auth.password or "", cfg.admin_password)
        )
        if auth is not None:
            record_login(ip, ok, now)
        if not ok:
            abort(Response("Login diperlukan", 401, {"WWW-Authenticate": 'Basic realm="Lab Control"'}))
        # A custom header forces a CORS preflight, so other websites cannot drive the API
        # with the admin's cached credentials.
        if request.method != "GET" and request.headers.get("X-Lab-Admin") != "1":
            abort(400, "header X-Lab-Admin wajib")

    def agent_only():
        if cfg.block_agent_via_tunnel and via_tunnel():
            abort(404)  # agents talk to the server over the LAN only
        token = request.headers.get("X-Agent-Token", "")
        if not secrets.compare_digest(token, cfg.agent_token):
            abort(403)

    def override_from_request(mode):
        body = request.get_json(silent=True) or {}
        minutes = body.get("minutes")
        if minutes is None:
            return {"mode": mode, "until": None}
        minutes = _int(minutes, 1, MAX_OVERRIDE_MINUTES)
        if minutes is None:
            abort(400, "minutes tidak valid")
        return {"mode": mode, "until": time.time() + minutes * 60}

    def pc_view(row, now):
        state = state_for(row, now)
        return {
            "id": row["id"],
            "online": now - row["last_seen"] <= cfg.online_timeout,
            "last_seen": row["last_seen"],
            "first_seen": row["first_seen"],
            "ip": row["ip"],
            "user": row["user"],
            "app": row["app"],
            "title": row["title"],
            "idle": row["idle"],
            "uptime": row["uptime"],
            "mem": row["mem"],
            "agent_locked": bool(row["locked"]),
            "version": row["version"],
            "locked": state["locked"],
            "source": state["source"],
            "changes_in": state["changes_in"],
            "override": state["override"],
        }

    @app.get("/healthz")
    def healthz():
        return jsonify(ok=True)  # lets install.ps1 check that the server is reachable from a PC

    # agent API -----------------------------------------------------------
    @app.post("/api/agent/poll")
    def agent_poll():
        agent_only()
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            abort(400)
        host = data.get("host", "")
        if not isinstance(host, str) or not HOST_RE.match(host):
            abort(400, "host tidak valid")
        now = time.time()

        report = {
            "ip": request.remote_addr,
            "user": _text(data.get("user"), 64),
            "app": _text(data.get("app"), 128),
            "title": _text(data.get("title"), 200),
            "idle": _int(data.get("idle")),
            "uptime": _int(data.get("uptime")),
            "mem": _int(data.get("mem"), 0, 100),
            "locked": 1 if data.get("locked") else 0,
            "version": _text(data.get("version"), 32),
        }
        events = []
        for raw in (data.get("events") or [])[:MAX_EVENTS_PER_POLL]:
            if not isinstance(raw, dict) or raw.get("type") not in EVENT_TYPES:
                continue
            ts = raw.get("ts")
            ts = float(ts) if isinstance(ts, (int, float)) else now
            events.append({
                "eid": _text(raw.get("eid"), 64) or secrets.token_hex(8),
                "ts": min(ts, now),
                "type": raw["type"],
                "detail": _text(raw.get("detail"), 300) or "",
            })

        before = store.record_report(host, report, events, now)
        if before is None:
            store.add_event(host, "info", "PC baru terdaftar", now)
        elif now - before["last_seen"] > cfg.online_timeout * 2:
            gap = int(now - before["last_seen"])
            store.add_event(host, "info", f"Kembali terhubung setelah terputus {gap} detik", now)
        store.prune(cfg.event_retention_days, now)

        pc_row = store.get_pc(host)
        state = state_for(pc_row, now)
        commands = store.take_commands(host, now - cfg.command_ttl, now)
        for action in commands:
            store.add_event(host, "command", f"Perintah dari admin: {action}", now)
        return jsonify({
            "locked": state["locked"],
            "message": policy.lock_message(state, get_tz(), now) if state["locked"] else "",
            "notice": store.get_notice(host, now) if state["locked"] else "",
            "changes_in": state["changes_in"],
            "commands": commands,
            "poll_interval": cfg.poll_interval,
            "warn_seconds": cfg.warn_seconds,
            "lab_name": cfg.lab_name,
        })

    # admin API -----------------------------------------------------------
    @app.get("/")
    def dashboard():
        admin_only()
        return send_from_directory(Path(__file__).parent / "static", "index.html")

    @app.get("/api/pcs")
    def api_pcs():
        admin_only()
        now = time.time()
        sched = schedule()
        return jsonify({
            "now": now,
            "lab_name": cfg.lab_name,
            "timezone": timezone_name(),
            "global_override": global_override(),
            "schedule_enabled": sched["enabled"],
            "pcs": [pc_view(row, now) for row in store.list_pcs()],
        })

    def pc_or_404(pc_id):
        row = store.get_pc(pc_id)
        if row is None:
            abort(404)
        return row

    @app.post("/api/pcs/<pc_id>/lock")
    def api_pc_lock(pc_id):
        admin_only()
        pc_or_404(pc_id)
        override = override_from_request("lock")
        store.set_override(pc_id, override)
        store.add_event(pc_id, "info", "Admin mengunci komputer" + _suffix(override))
        return jsonify(ok=True)

    @app.post("/api/pcs/<pc_id>/unlock")
    def api_pc_unlock(pc_id):
        admin_only()
        pc_or_404(pc_id)
        override = override_from_request("unlock")
        store.set_override(pc_id, override)
        store.add_event(pc_id, "info", "Admin membuka kunci komputer" + _suffix(override))
        return jsonify(ok=True)

    @app.post("/api/pcs/<pc_id>/clear")
    def api_pc_clear(pc_id):
        admin_only()
        pc_or_404(pc_id)
        store.set_override(pc_id, None)
        store.add_event(pc_id, "info", "Admin mengembalikan komputer ke jadwal")
        return jsonify(ok=True)

    @app.post("/api/pcs/<pc_id>/command")
    def api_pc_command(pc_id):
        admin_only()
        pc_or_404(pc_id)
        action = (request.get_json(silent=True) or {}).get("action")
        if action not in COMMANDS:
            abort(400, "action tidak dikenal")
        store.queue_command(pc_id, action, time.time())
        return jsonify(ok=True)

    @app.delete("/api/pcs/<pc_id>")
    def api_pc_delete(pc_id):
        admin_only()
        store.delete_pc(pc_id)
        return jsonify(ok=True)

    @app.post("/api/all/<action>")
    def api_all(action):
        admin_only()
        if action in ("lock", "unlock"):
            override = override_from_request(action)
            store.set_setting("global_override", override)
            for row in store.list_pcs():  # a manual per-PC override would otherwise win
                store.set_override(row["id"], None)
            detail = ("Admin mengunci semua komputer" if action == "lock"
                      else "Admin membuka kunci semua komputer") + _suffix(override)
        elif action == "clear":
            store.set_setting("global_override", None)
            for row in store.list_pcs():
                store.set_override(row["id"], None)
            detail = "Admin mengembalikan semua komputer ke jadwal"
        else:
            abort(404)
        store.add_event("*", "info", detail)
        return jsonify(ok=True)

    def exit_process():
        time.sleep(1)   # beri waktu agar respons terkirim; systemd (Restart=always) menyalakan ulang
        os._exit(0)

    app.config["EXIT_FN"] = exit_process

    @app.post("/api/server/restart")
    def api_server_restart():
        admin_only()
        if not os.environ.get("INVOCATION_ID"):
            abort(409, "server tidak berjalan di bawah systemd, restart manual")
        store.add_event("*", "info", "Admin me-restart server")
        threading.Thread(target=app.config["EXIT_FN"], daemon=True).start()
        return jsonify(ok=True)

    def notice_from_request():
        body = request.get_json(silent=True) or {}
        text = str(body.get("text") or "").strip()
        if not text or len(text) > MAX_NOTICE_CHARS:
            abort(400, f"pesan wajib diisi, maksimal {MAX_NOTICE_CHARS} karakter")
        minutes = body.get("minutes")
        if minutes is None:
            return text, None
        minutes = _int(minutes, 1, MAX_OVERRIDE_MINUTES)
        if minutes is None:
            abort(400, "minutes tidak valid")
        return text, time.time() + minutes * 60

    @app.post("/api/pcs/<pc_id>/message")
    def api_pc_message(pc_id):
        admin_only()
        pc_or_404(pc_id)
        text, until = notice_from_request()
        store.set_notice(pc_id, text, until, time.time())
        store.add_event(pc_id, "info", f"Admin mengirim pesan: {text}")
        return jsonify(ok=True)

    @app.delete("/api/pcs/<pc_id>/message")
    def api_pc_message_clear(pc_id):
        admin_only()
        pc_or_404(pc_id)
        store.clear_notice(pc_id)
        return jsonify(ok=True)

    @app.post("/api/all/message")
    def api_all_message():
        admin_only()
        text, until = notice_from_request()
        store.set_notice("*", text, until, time.time())
        store.add_event("*", "info", f"Admin mengirim pesan ke semua komputer: {text}")
        return jsonify(ok=True)

    @app.delete("/api/all/message")
    def api_all_message_clear():
        admin_only()
        store.clear_notice("*")
        return jsonify(ok=True)

    @app.get("/api/timezones")
    def api_timezones():
        admin_only()
        return jsonify({"current": timezone_name(), "zones": sorted(ALL_ZONES)})

    @app.put("/api/timezone")
    def api_timezone_put():
        admin_only()
        name = (request.get_json(silent=True) or {}).get("timezone")
        if name not in ALL_ZONES:
            abort(400, "zona waktu tidak dikenal")
        store.set_setting("timezone", name)
        store.add_event("*", "info", f"Admin mengubah zona waktu ke {name}")
        return jsonify(ok=True)

    @app.get("/api/schedule")
    def api_schedule_get():
        admin_only()
        return jsonify(schedule())

    @app.put("/api/schedule")
    def api_schedule_put():
        admin_only()
        body = request.get_json(silent=True) or {}
        try:
            rules = policy.validate_rules(body.get("rules", []))
        except (KeyError, TypeError, ValueError) as exc:
            abort(400, str(exc))
        store.set_setting("schedule", {"enabled": bool(body.get("enabled")), "rules": rules})
        return jsonify(ok=True)

    @app.get("/api/events")
    def api_events():
        admin_only()
        rows = store.list_events(
            request.args.get("pc") or None,
            _int(request.args.get("limit"), 1, 500) or 200,
            _int(request.args.get("before")),
        )
        return jsonify([dict(row) for row in rows])

    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    def plain_error(err):
        return jsonify(error=getattr(err, "description", "error")), err.code

    return app


def _suffix(override):
    if override["until"] is None:
        return " (tanpa batas waktu)"
    minutes = round((override["until"] - time.time()) / 60)
    return f" ({minutes} menit)"
