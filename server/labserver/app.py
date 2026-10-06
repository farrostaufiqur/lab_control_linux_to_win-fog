import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path
from zoneinfo import ZoneInfo, available_timezones

from datetime import timedelta
from urllib.parse import quote

from flask import Flask, Response, abort, jsonify, redirect, render_template, request, send_from_directory, session
from flask.sessions import SecureCookieSessionInterface
from werkzeug.exceptions import HTTPException

from . import policy, release, shared, uploads
from .config import Config
from .db import Store

HOST_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
EVENT_TYPES = {"agent_start", "app", "lock", "unlock", "user_action", "command", "info"}
COMMANDS = {"shutdown", "restart"}
MAX_EVENTS_PER_POLL = 200
MAX_OVERRIDE_MINUTES = 60 * 24 * 7
MAX_NOTICE_CHARS = 300
MAX_NAME_CHARS = 40
SESSION_HOURS = 12
NO_LIMIT = 1 << 62      # Flask menganggap None = "pakai MAX_CONTENT_LENGTH", jadi batas tak terhingga dinyatakan angka besar
MAX_SHOT_BYTES = 512 * 1024
DEFAULT_SHOT_INTERVAL = 10
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

    # sesi login (cookie bertanda tangan); kunci dibuat sekali dan disimpan, jadi sesi bertahan saat restart
    secret = store.get_setting("session_secret", None)
    if not secret:
        secret = secrets.token_hex(32)
        store.set_setting("session_secret", secret)
    app.secret_key = secret
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_NAME="labsession",
                      PERMANENT_SESSION_LIFETIME=timedelta(hours=SESSION_HOURS))

    class LabSessionInterface(SecureCookieSessionInterface):
        def get_cookie_secure(self, app):   # Secure hanya bila pengunjung memakai HTTPS (tunnel); LAN memakai HTTP
            return request.is_secure or "https" in request.headers.get("Cf-Visitor", "") \
                or request.headers.get("X-Forwarded-Proto", "") == "https"
    app.session_interface = LabSessionInterface()

    def password_fingerprint():   # mengganti password di config.toml mengakhiri semua sesi lama
        return hmac.new(secret.encode(), cfg.admin_password.encode(), hashlib.sha256).hexdigest()[:32]

    def session_valid():
        value = session.get("lab")
        return isinstance(value, str) and hmac.compare_digest(value, password_fingerprint())

    shots_dir = Path(cfg.db_path).parent / "screenshots"
    upload_sessions = uploads.Uploads(Path(cfg.db_path).parent / "uploads")
    shared_files = shared.SharedFiles(Path(cfg.db_path).parent / "shared", store)
    shared_status: dict[str, dict] = {}       # laporan sinkron terakhir tiap PC (di memori saja)

    def shared_settings():
        saved = store.get_setting("shared", {}) or {}
        mode = saved.get("default_mode")
        return {"path": saved.get("path") if isinstance(saved.get("path"), str) else "",
                "default_mode": mode if mode in shared.MODES else "off"}

    def shared_mode_for(row):
        """Aturan efektif PC ini: miliknya sendiri, atau bawaan; mati bila folder tujuan belum diisi."""
        if not shared_settings()["path"]:
            return "off"
        return row["shared_mode"] if row["shared_mode"] in shared.MODES else shared_settings()["default_mode"]

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

    def csrf_header_required():
        # Header khusus memaksa preflight CORS, jadi situs lain tidak bisa memakai sesi/kredensial admin
        if request.method != "GET" and request.headers.get("X-Lab-Admin") != "1":
            abort(400, "header X-Lab-Admin wajib")

    def safe_next(value):
        return value if isinstance(value, str) and value.startswith("/") and not value.startswith(("//", "/\\")) else "/"

    def admin_only():
        if session_valid():
            csrf_header_required()
            return
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
            if request.path.startswith("/api/"):   # tanpa tantangan Basic, supaya browser tidak memunculkan prompt
                abort(Response(json.dumps({"error": "login diperlukan"}), 401, mimetype="application/json"))
            abort(redirect("/login?next=" + quote(request.full_path.rstrip("?"), safe="/")))
        csrf_header_required()

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
        status = data.get("shared")
        if isinstance(status, dict):
            shared_status[host] = {"ok": bool(status.get("ok")), "files": _int(status.get("files")) or 0,
                                   "error": _text(status.get("error"), 200) or "", "at": now}
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
            "shared": {"mode": shared_mode_for(pc_row), "path": shared_settings()["path"],
                       "interval": shared.SYNC_SECONDS},
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

    # folder bersama (agent) ----------------------------------------------
    def shared_agent(need):
        """Izin sinkron: token, hanya LAN, PC dikenal, dan aturan PC mengizinkan `need` (index | read | write)."""
        agent_only()
        if via_tunnel():
            abort(404)                          # folder bersama tidak pernah dilayani lewat Cloudflare
        host = request.args.get("host", "")
        row = store.get_pc(host) if HOST_RE.match(host) else None
        if row is None:
            abort(404)
        mode = shared_mode_for(row)
        allowed = {"index": ("receive", "send", "both"), "read": ("receive", "both"), "write": ("send", "both")}[need]
        if mode not in allowed:
            abort(403, "aturan folder bersama komputer ini tidak mengizinkan")
        return row

    def shared_path_arg():
        path = shared.clean_path(request.args.get("path", ""))
        if path is None:
            abort(400, "path file tidak valid")
        return path

    def shared_error(status):
        codes = {"disk_full": (413, "ruang disk server hampir habis"),
                 "conflict": (409, "file berubah di server; coba lagi"),
                 "name_clash": (409, "nama bentrok dengan file/folder lain (huruf besar-kecil dianggap sama)")}
        code, text = codes[status]
        abort(code, text)

    def mtime_arg():
        try:
            value = float(request.args.get("mtime", ""))
        except ValueError:
            return time.time()
        return min(value, time.time() + 60) if value > 0 else time.time()

    @app.get("/api/agent/shared/index")
    def agent_shared_index():
        shared_agent("index")
        return jsonify({"files": shared_files.index()})

    @app.get("/api/agent/shared/file")
    def agent_shared_get():
        shared_agent("read")
        target = shared_files.file_path(shared_path_arg())
        if target is None:
            abort(404)
        return send_from_directory(target.parent, target.name, mimetype="application/octet-stream", max_age=0)

    @app.put("/api/agent/shared/file")
    def agent_shared_put():
        shared_agent("write")
        path = shared_path_arg()
        request.max_content_length = NO_LIMIT        # folder bersama tanpa batas ukuran
        status, entry = shared_files.put(path, request.stream, mtime_arg(), request.args.get("expect"), time.time())
        if status != "ok":
            shared_error(status)
        return jsonify(ok=True, **entry)

    @app.delete("/api/agent/shared/file")
    def agent_shared_delete():
        shared_agent("write")
        status = shared_files.delete(shared_path_arg(), request.args.get("expect"))
        if status == "conflict":
            shared_error(status)
        return jsonify(ok=True)

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
            "pcs_total": len(pcs),
            "pcs_current": sum(1 for row in pcs if rel and row["exe_sha"] == rel["sha256"]),
        })

    @app.post("/api/release")
    def api_release_post():
        admin_only()
        version = clean_version(request.args.get("version"))
        request.max_content_length = release.MAX_RELEASE_BYTES
        return commit_release(request.stream, version)

    def clean_version(raw):
        version = str(raw or "").strip()
        if version and not release.VERSION_RE.match(version):
            abort(400, "versi: 1-32 karakter huruf, angka, titik, garis bawah, atau tanda hubung")
        return version

    def commit_release(stream, version):
        releases_dir.mkdir(parents=True, exist_ok=True)
        tmp = releases_dir / f"upload-{secrets.token_hex(6)}.tmp"
        try:
            try:
                sha, size = release.sha_of_stream(stream, tmp, release.MAX_RELEASE_BYTES)
            except ValueError:
                abort(413, "berkas terlalu besar")
            with open(tmp, "rb") as f:
                is_exe = f.read(2) == b"MZ"
            if not size or not is_exe:
                abort(400, "berkas bukan exe Windows")
            version = version or release.default_version(sha)
            release.store_file(releases_dir, sha, tmp)
        finally:
            tmp.unlink(missing_ok=True)
        info = {"version": version, "sha256": sha, "size": size, "uploaded": time.time()}
        store.set_setting("agent_release", info)
        release.prune_files(releases_dir, sha)
        store.add_event("*", "info", f"Admin mengunggah rilis agent {version} ({sha[:12]})")
        return jsonify(ok=True, release=info)

    # unggah per potongan (dashboard) -----------------------------------
    def upload_or_404(uid):
        meta = upload_sessions.meta(uid)
        if meta is None:
            abort(404, "sesi unggah tidak ada atau sudah kedaluwarsa")
        return meta

    @app.post("/api/uploads")
    def api_upload_create():
        admin_only()
        body = request.get_json(silent=True) or {}
        kind, size = body.get("kind"), body.get("size")
        if type(size) is not int or size <= 0:
            abort(400, "size wajib diisi (byte)")
        if kind == "shared":
            target, limit = shared.clean_path(body.get("path")), None
            if target is None:
                abort(400, "path file tidak valid")
            if shared_files.disk_free() - size < shared.MIN_FREE_BYTES:
                abort(413, "ruang disk server tidak cukup untuk berkas ini")
        elif kind == "release":
            target, limit = clean_version(body.get("version")), release.MAX_RELEASE_BYTES
        else:
            abort(400, "kind tidak dikenal")
        if limit is not None and size > limit:
            abort(413, f"berkas lebih dari {limit // 1048576} MB")
        uid = upload_sessions.create({"kind": kind, "target": target, "size": size}, time.time())
        if uid is None:
            abort(409, "terlalu banyak unggahan yang belum selesai; coba lagi nanti")
        return jsonify(id=uid, chunk=min(uploads.CHUNK_MAX_BYTES, 1024 * 1024))

    @app.get("/api/uploads/<uid>")
    def api_upload_status(uid):
        admin_only()
        meta = upload_or_404(uid)
        return jsonify(received=upload_sessions.received(uid), size=meta["size"])

    @app.put("/api/uploads/<uid>")
    def api_upload_chunk(uid):
        admin_only()
        meta = upload_or_404(uid)
        offset = _int(request.args.get("offset"), 0, meta["size"])
        if offset is None:
            abort(400, "offset tidak valid")
        request.max_content_length = uploads.CHUNK_MAX_BYTES
        status, received = upload_sessions.append(uid, offset, request.stream, meta["size"])
        if status == "too_big":
            abort(413, "potongan melebihi ukuran yang dijanjikan")
        # offset tidak sesuai (mis. potongan sebelumnya sebenarnya sudah masuk): beri tahu posisi sebenarnya
        return jsonify(received=received, resync=status == "offset"), (409 if status == "offset" else 200)

    @app.post("/api/uploads/<uid>/finish")
    def api_upload_finish(uid):
        admin_only()
        meta = upload_or_404(uid)
        if upload_sessions.received(uid) != meta["size"]:
            abort(409, f"belum lengkap: {upload_sessions.received(uid)} dari {meta['size']} byte")
        try:
            with open(upload_sessions.part_path(uid), "rb") as stream:
                if meta["kind"] == "shared":
                    status, entry = shared_files.put(meta["target"], stream, time.time(), None, time.time())
                    if status != "ok":
                        shared_error(status)
                    store.add_event("*", "info", f"Admin mengunggah ke folder bersama: {meta['target']}")
                    return jsonify(ok=True, **entry)
                return commit_release(stream, meta["target"])
        finally:
            upload_sessions.remove(uid)

    @app.delete("/api/uploads/<uid>")
    def api_upload_abort(uid):
        admin_only()
        upload_sessions.remove(uid) if upload_sessions.meta(uid) else None
        return jsonify(ok=True)

    @app.delete("/api/release")
    def api_release_delete():
        admin_only()
        store.set_setting("agent_release", None)
        release.prune_files(releases_dir, None)
        store.add_event("*", "info", "Admin menghentikan distribusi rilis agent")
        return jsonify(ok=True)

    # login ---------------------------------------------------------------
    def login_page(error="", status=200):
        return render_template("login.html", lab_name=cfg.lab_name, error=error,
                               next=safe_next(request.values.get("next"))), status

    @app.get("/login")
    def login_form():
        if session_valid():
            return redirect(safe_next(request.args.get("next")))
        return login_page()

    @app.post("/login")
    def login_submit():
        ip, now = client_ip(), time.time()
        try:
            check_login_block(ip, now)
        except HTTPException:             # diblokir (429): tampilkan di halaman login
            return login_page("Terlalu banyak percobaan login. Coba lagi nanti.", 429)
        username, password = request.form.get("username", ""), request.form.get("password", "")
        ok = (secrets.compare_digest(username.strip(), cfg.admin_user)
              and secrets.compare_digest(password, cfg.admin_password))
        record_login(ip, ok, now)
        if not ok:
            return login_page("Username atau password salah.", 401)
        session.clear()
        session["lab"] = password_fingerprint()
        session.permanent = True
        return redirect(safe_next(request.form.get("next")))

    @app.get("/logout")
    def logout():
        session.clear()
        return redirect("/login")

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

    @app.get("/pembagian")
    def page_shared():
        return page("shared.html", "shared")

    @app.get("/api/shared")
    def api_shared():
        admin_only()
        now, files = time.time(), shared_files.index()
        pcs = []
        for row in store.list_pcs():
            status = shared_status.get(row["id"])
            pcs.append({"id": row["id"], "name": row["alias"] or row["id"],
                        "online": now - row["last_seen"] <= cfg.online_timeout,
                        "mode": row["shared_mode"] if row["shared_mode"] in shared.MODES else None,
                        "effective": shared_mode_for(row),
                        "status": {**status, "age": now - status["at"]} if status else None})
        return jsonify({"settings": shared_settings(), "modes": shared.MODE_LABELS,
                        "files": [{"path": p, **e} for p, e in files.items()],
                        "total_size": sum(e["size"] for e in files.values()),
                        "disk_free": shared_files.disk_free(),
                        "pcs": pcs, "sync_seconds": shared.SYNC_SECONDS})

    @app.put("/api/shared/settings")
    def api_shared_settings():
        admin_only()
        body = request.get_json(silent=True) or {}
        path, mode = shared.clean_profile_path(body.get("path", "")), body.get("default_mode", "off")
        if path is None:
            abort(400, "folder tujuan: path relatif di profil user, mis. Desktop/shared (tanpa .. atau huruf drive)")
        if mode not in shared.MODES:
            abort(400, "aturan bawaan tidak dikenal")
        store.set_setting("shared", {"path": path, "default_mode": mode})
        store.add_event("*", "info", f"Admin mengatur folder bersama: {path or '(mati)'} · bawaan {shared.MODE_LABELS[mode]}")
        return jsonify(ok=True)

    @app.put("/api/pcs/<pc_id>/shared-mode")
    def api_pc_shared_mode(pc_id):
        admin_only()
        pc_or_404(pc_id)
        mode = (request.get_json(silent=True) or {}).get("mode")
        if mode not in (*shared.MODES, "default"):
            abort(400, "aturan tidak dikenal")
        store.set_shared_mode(pc_id, None if mode == "default" else mode)
        store.add_event(pc_id, "info", "Admin mengatur folder bersama: " + ("ikut bawaan" if mode == "default" else shared.MODE_LABELS[mode]))
        return jsonify(ok=True)

    @app.get("/api/shared/file")
    def api_shared_download():
        admin_only()
        path = shared_path_arg()
        target = shared_files.file_path(path)
        if target is None:
            abort(404)
        return send_from_directory(target.parent, target.name, as_attachment=True, download_name=target.name, max_age=0)

    @app.post("/api/shared/file")
    def api_shared_upload():
        admin_only()
        path = shared_path_arg()
        request.max_content_length = NO_LIMIT
        status, entry = shared_files.put(path, request.stream, time.time(), None, time.time())
        if status != "ok":
            shared_error(status)
        store.add_event("*", "info", f"Admin mengunggah ke folder bersama: {path}")
        return jsonify(ok=True, **entry)

    @app.delete("/api/shared/file")
    def api_shared_delete():
        admin_only()
        path = shared_path_arg()
        shared_files.delete(path, None)
        store.add_event("*", "info", f"Admin menghapus dari folder bersama: {path}")
        return jsonify(ok=True)

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

    @app.post("/api/all/command")
    def api_all_command():
        """Restart/matikan semua komputer yang sedang online. PC offline dilewati supaya tidak mati begitu menyala."""
        admin_only()
        action = (request.get_json(silent=True) or {}).get("action")
        if action not in COMMANDS:
            abort(400, "action tidak dikenal")
        now = time.time()
        targets = [row["id"] for row in store.list_pcs() if now - row["last_seen"] <= cfg.online_timeout]
        for pc_id in targets:
            store.queue_command(pc_id, action, now)
        store.add_event("*", "info", f"Admin meminta {action} ke semua komputer online ({len(targets)} komputer)")
        return jsonify(ok=True, count=len(targets))

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
