import base64
import ipaddress
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path
from zoneinfo import ZoneInfo, available_timezones

from flask import Flask, Response, abort, jsonify, render_template, request, send_from_directory

from . import policy, release
from .config import Config
from .db import Store

HOST_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
EVENT_TYPES = {"agent_start", "app", "lock", "unlock", "user_action", "command", "info"}
COMMANDS = {"shutdown", "restart"}
MAX_EVENTS_PER_POLL = 200
MAX_OVERRIDE_MINUTES = 60 * 24 * 7
MAX_NOTICE_CHARS = 300
MAX_NAME_CHARS = 40
MAX_SHOT_BYTES = 1536 * 1024
DEFAULT_SHOT_INTERVAL = 5
MAX_SHOT_INTERVAL = 3600
MIN_SHOT_INTERVAL = 5
HOSTNAME_RE = re.compile(r"^(?!\d+$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,13}[A-Za-z0-9])?$")  # aturan nama NetBIOS 15 karakter
ALL_ZONES = frozenset(available_timezones())


def _text(value, limit):
    return None if value is None else str(value)[:limit]


def _int(value, low=0, high=10**9):
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return None


def create_app(cfg: Config) -> Flask:
    app = Flask(__name__)   # /static (aset tampilan) terbuka tanpa login; tidak ada data di sana
    app.config["MAX_CONTENT_LENGTH"] = 256 * 1024
    store = Store(cfg.db_path)

    shots_dir = Path(cfg.db_path).parent / "screenshots"

    # helpers -------------------------------------------------------------
    def shot_interval():
        return _int(store.get_setting("screenshot_interval", DEFAULT_SHOT_INTERVAL), 0, MAX_SHOT_INTERVAL) or 0

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

    def pc_view(row, now, renames=None):
        state = state_for(row, now)
        req = (renames if renames is not None else store.list_renames()).get(row["id"])
        return {
            "id": row["id"],
            "name": row["alias"] or row["id"],
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
            "exe_sha": row["exe_sha"],
            "shot": row["shot_ts"] if row["shot_ts"] and not row["locked"] and not state["locked"]
                    and now - row["last_seen"] <= cfg.online_timeout else None,
            "rename": {"name": req["new"], "state": req["state"], "error": req["error"]} if req else None,
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
            "ip": _lan_ip(data.get("lan_ip")) or client_ip(),
            "user": _text(data.get("user"), 64),
            "app": _text(data.get("app"), 128),
            "title": _text(data.get("title"), 200),
            "idle": _int(data.get("idle")),
            "uptime": _int(data.get("uptime")),
            "mem": _int(data.get("mem"), 0, 100),
            "locked": 1 if data.get("locked") else 0,
            "version": _text(data.get("version"), 32),
            "exe_sha": _text(data.get("exe_sha"), 64) or None,
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
            old = store.migrate_renamed(host)
            if old:
                store.add_event(host, "info", f"Nama Windows diganti dari {old}", now)
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
            "screenshot_interval": shot_interval(),
            "warn_seconds": cfg.warn_seconds,
            "lab_name": cfg.lab_name,
        })

    @app.post("/api/agent/screenshot")
    def agent_screenshot():
        agent_only()
        host = request.args.get("host", "")
        if not HOST_RE.match(host) or store.get_pc(host) is None:
            abort(404)
        request.max_content_length = MAX_SHOT_BYTES
        data = request.get_data()
        if not data.startswith(b"\xff\xd8\xff"):
            abort(400, "bukan JPEG")
        if not shot_interval():
            return jsonify(ok=True, stored=False)   # fitur dimatikan admin: buang
        shots_dir.mkdir(parents=True, exist_ok=True)
        tmp = shots_dir / f"{host}.{secrets.token_hex(4)}.tmp"
        tmp.write_bytes(data)
        os.replace(tmp, shots_dir / f"{host}.jpg")
        store.set_shot(host, time.time())
        return jsonify(ok=True, stored=True)

    # rilis agent ---------------------------------------------------------
    releases_dir = Path(cfg.db_path).parent / "releases"

    def current_release():
        rel = store.get_setting("agent_release", None)
        if not rel or not (releases_dir / f"labagent-{rel['sha256']}.exe").is_file():
            return None
        return rel

    @app.post("/api/agent/release")
    def agent_release():
        agent_only()
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            abort(400)
        host, sha = data.get("host", ""), data.get("sha", "")
        if isinstance(host, str) and HOST_RE.match(host) and isinstance(sha, str) and release.SHA_RE.match(sha):
            store.set_build(host, _text(data.get("version"), 32), sha)
        rel = current_release()
        req = store.list_renames().get(host) if isinstance(host, str) else None
        rename = {"name": req["new"]} if req and req["state"] == "pending" else None
        return jsonify({"release": release.manifest(rel) if rel else None, "rename": rename})

    @app.post("/api/agent/rename-result")
    def agent_rename_result():
        agent_only()
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            abort(400)
        host, name = data.get("host", ""), data.get("name", "")
        if not (isinstance(host, str) and HOST_RE.match(host) and isinstance(name, str) and HOSTNAME_RE.match(name)):
            abort(400)
        req = store.rename_result(host, name.upper(), bool(data.get("ok")), _text(data.get("error"), 200) or "")
        if req is None:
            return jsonify(ok=True)
        if data.get("ok"):
            store.add_event(host, "info", f"Nama Windows diganti menjadi {req['new']}; berlaku setelah restart")
            if req["restart"]:
                store.queue_command(host, "restart", time.time())
        else:
            store.add_event(host, "info", f"Gagal mengganti nama Windows: {_text(data.get('error'), 200)}")
        return jsonify(ok=True)

    @app.get("/api/agent/release/file")
    def agent_release_file():
        agent_only()
        rel = current_release()
        if rel is None:
            abort(404)
        return send_from_directory(releases_dir, f"labagent-{rel['sha256']}.exe", mimetype="application/octet-stream")

    # admin API -----------------------------------------------------------
    @app.get("/api/release")
    def api_release_get():
        admin_only()
        pcs = store.list_pcs()
        rel = current_release()
        return jsonify({
            "release": rel,
            "signing_enabled": bool(cfg.update_public_key),
            "pcs_total": len(pcs),
            "pcs_current": sum(1 for row in pcs if rel and row["exe_sha"] == rel["sha256"]),
        })

    @app.post("/api/release")
    def api_release_post():
        admin_only()
        if not cfg.update_public_key:
            abort(409, "update_public_key belum diatur di config.toml server")
        version = request.args.get("version", "")
        signature = release.decode_signature(request.args.get("signature", ""))
        if not release.VERSION_RE.match(version) or signature is None:
            abort(400, "version atau signature tidak valid")
        request.max_content_length = release.MAX_RELEASE_BYTES
        releases_dir.mkdir(parents=True, exist_ok=True)
        tmp = releases_dir / f"upload-{secrets.token_hex(6)}.tmp"
        try:
            try:
                sha, size = release.sha_of_stream(request.stream, tmp, release.MAX_RELEASE_BYTES)
            except ValueError:
                abort(413, "berkas terlalu besar")
            with open(tmp, "rb") as f:
                is_exe = f.read(2) == b"MZ"
            if not size or not is_exe:
                abort(400, "berkas bukan exe Windows")
            if not release.verify(cfg.update_public_key, version, sha, signature):
                abort(400, "tanda tangan tidak cocok dengan berkas, versi, atau kunci publik server")
            release.store_file(releases_dir, sha, tmp)
        finally:
            tmp.unlink(missing_ok=True)
        info = {"version": version, "sha256": sha, "size": size,
                "signature": base64.b64encode(signature).decode(), "uploaded": time.time()}
        store.set_setting("agent_release", info)
        release.prune_files(releases_dir, sha)
        store.add_event("*", "info", f"Admin mengunggah rilis agent {version} ({sha[:12]})")
        return jsonify(ok=True, release=info)

    @app.delete("/api/release")
    def api_release_delete():
        admin_only()
        store.set_setting("agent_release", None)
        release.prune_files(releases_dir, None)
        store.add_event("*", "info", "Admin menghentikan distribusi rilis agent")
        return jsonify(ok=True)

    # halaman -------------------------------------------------------------
    def page(template, active, **extra):
        admin_only()
        return render_template(template, lab_name=cfg.lab_name, active=active, **extra)

    @app.get("/")
    def page_dashboard():
        return page("dashboard.html", "dashboard")

    @app.get("/pc/<pc_id>")
    def page_pc(pc_id):
        return page("pc.html", "dashboard", pc_id=pc_id)

    @app.get("/jadwal")
    def page_schedule():
        return page("jadwal.html", "jadwal")

    @app.get("/log")
    def page_log():
        return page("log.html", "log")

    @app.get("/pembaruan")
    def page_release():
        return page("pembaruan.html", "pembaruan")

    @app.get("/pengaturan")
    def page_settings():
        return page("pengaturan.html", "pengaturan")

    @app.get("/api/settings")
    def api_settings_get():
        admin_only()
        return jsonify({"screenshot_interval": shot_interval(),
                        "agent_via_tunnel": not cfg.block_agent_via_tunnel,
                        "restartable": bool(os.environ.get("INVOCATION_ID"))})

    @app.put("/api/settings")
    def api_settings_put():
        admin_only()
        seconds = (request.get_json(silent=True) or {}).get("screenshot_interval")
        if type(seconds) is not int or not (seconds == 0 or MIN_SHOT_INTERVAL <= seconds <= MAX_SHOT_INTERVAL):
            abort(400, f"screenshot_interval: 0 (mati) atau {MIN_SHOT_INTERVAL}-{MAX_SHOT_INTERVAL} detik")
        store.set_setting("screenshot_interval", seconds)
        if not seconds:                       # dimatikan: jangan simpan gambar lama
            store.clear_shots()
            for old in shots_dir.glob("*.jpg"):
                old.unlink(missing_ok=True)
        store.add_event("*", "info", f"Admin mengatur screenshot tiap {seconds} detik" if seconds else "Admin mematikan screenshot")
        return jsonify(ok=True)

    @app.get("/api/pcs/<pc_id>/screenshot")
    def api_pc_screenshot(pc_id):
        admin_only()
        row = pc_or_404(pc_id)
        if not row["shot_ts"] or not HOST_RE.match(pc_id):
            abort(404)
        response = send_from_directory(shots_dir, f"{pc_id}.jpg", mimetype="image/jpeg", max_age=0)
        response.headers["Cache-Control"] = "private, max-age=300"   # URL memuat ?t=<waktu>, jadi aman di-cache
        return response

    @app.get("/api/pcs")
    def api_pcs():
        admin_only()
        now = time.time()
        sched = schedule()
        renames = store.list_renames()
        return jsonify({
            "now": now,
            "lab_name": cfg.lab_name,
            "timezone": timezone_name(),
            "global_override": global_override(),
            "schedule_enabled": sched["enabled"],
            "release": current_release(),
            "pcs": [pc_view(row, now, renames) for row in store.list_pcs()],
        })

    def pc_or_404(pc_id):
        row = store.get_pc(pc_id)
        if row is None:
            abort(404)
        return row

    @app.put("/api/pcs/<pc_id>/name")
    def api_pc_rename(pc_id):
        admin_only()
        pc_or_404(pc_id)
        name = str((request.get_json(silent=True) or {}).get("name") or "").strip()
        if len(name) > MAX_NAME_CHARS or any(ord(c) < 32 for c in name):
            abort(400, f"nama maksimal {MAX_NAME_CHARS} karakter tanpa karakter kontrol")
        store.set_alias(pc_id, name or None)
        store.add_event(pc_id, "info", f"Admin mengganti nama tampilan menjadi {name}" if name else "Admin menghapus nama tampilan")
        return jsonify(ok=True, name=name or pc_id)

    @app.put("/api/pcs/<pc_id>/windows-name")
    def api_pc_windows_name(pc_id):
        admin_only()
        pc_or_404(pc_id)
        body = request.get_json(silent=True) or {}
        name = str(body.get("name") or "").strip()
        if not HOSTNAME_RE.match(name):
            abort(400, "nama Windows: 1-15 huruf/angka/tanda hubung, tidak boleh diawali atau diakhiri tanda hubung, tidak boleh hanya angka")
        name = name.upper()
        taken = {row["id"].upper() for row in store.list_pcs() if row["id"] != pc_id}
        taken |= {r["new"] for old, r in store.list_renames().items() if old != pc_id}
        if name in taken:
            abort(409, f"nama {name} sudah dipakai PC lain")
        if name == pc_id.upper():
            abort(400, "nama baru sama dengan nama sekarang")
        store.request_rename(pc_id, name, bool(body.get("restart")), time.time())
        store.add_event(pc_id, "info", f"Admin meminta nama Windows diganti menjadi {name}" + (" (lalu restart)" if body.get("restart") else ""))
        return jsonify(ok=True, name=name)

    @app.delete("/api/pcs/<pc_id>/windows-name")
    def api_pc_windows_name_cancel(pc_id):
        admin_only()
        pc_or_404(pc_id)
        store.cancel_rename(pc_id)
        return jsonify(ok=True)

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
        if HOST_RE.match(pc_id):
            (shots_dir / f"{pc_id}.jpg").unlink(missing_ok=True)
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
    @app.errorhandler(409)
    @app.errorhandler(413)
    def plain_error(err):
        return jsonify(error=getattr(err, "description", "error")), err.code

    return app


def _lan_ip(value):
    """Alamat LAN yang dilaporkan agent (lewat tunnel, alamat koneksi bukan alamat PC)."""
    try:
        ip = ipaddress.ip_address(str(value))
    except ValueError:
        return None
    return None if ip.is_unspecified or ip.is_loopback else str(ip)


def _suffix(override):
    if override["until"] is None:
        return " (tanpa batas waktu)"
    minutes = round((override["until"] - time.time()) / 60)
    return f" ({minutes} menit)"
