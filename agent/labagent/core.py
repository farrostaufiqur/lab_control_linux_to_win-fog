"""Platform-independent agent logic: talks to the server and decides lock state.

The Windows-specific parts (window info, shutdown) are injected through `platform`, so this
module can be tested on any OS.
"""
import json
import os
import secrets
import socket
import threading
import time
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from . import VERSION
from .config import AgentConfig

MAX_QUEUED_EVENTS = 1000
EVENTS_PER_POLL = 100
IGNORED_PROCESSES = {"", "labagent.exe", "lockapp.exe", "searchhost.exe", "shellexperiencehost.exe"}
TITLE_EVENT_MIN_GAP = 15.0  # seconds between title-change events inside the same app


@dataclass(frozen=True)
class View:
    locked: bool
    ends_at: float | None   # epoch when locked/unlocked state is expected to flip; None = unknown
    message: str
    connected: bool
    lab_name: str
    warn_seconds: int
    exempt: bool
    notice: str = ""        # admin message shown on the lock screen


def agent_headers(token: str, content_type: str = "application/json") -> dict:
    # Cloudflare menolak User-Agent bawaan urllib ("Python-urllib", error 1010), jadi pakai nama sendiri.
    return {"Content-Type": content_type, "X-Agent-Token": token, "User-Agent": f"LabAgent/{VERSION}"}


def http_post_json(url: str, body: dict, token: str, timeout: float = 5.0) -> dict:
    request = urllib.request.Request(url, data=json.dumps(body).encode(), headers=agent_headers(token))
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def http_post_bytes(url: str, data: bytes, token: str, timeout: float = 15.0) -> None:
    request = urllib.request.Request(url, data=data, headers=agent_headers(token, "image/jpeg"))
    with urllib.request.urlopen(request, timeout=timeout) as response:
        response.read()


def local_ip() -> str:
    """Alamat LAN PC ini (antarmuka rute bawaan). UDP connect tidak mengirim paket apa pun."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("10.255.255.255", 1))
            return sock.getsockname()[0]
    except OSError:
        return ""


class Agent:
    def __init__(self, cfg: AgentConfig, platform, post=http_post_json, clock=time.time,
                 upload=http_post_bytes, lan_ip=local_ip):
        self.cfg, self.platform, self._post, self._clock = cfg, platform, post, clock
        self._upload, self._lan_ip = upload, lan_ip
        self.host = platform.hostname()
        self.user = platform.username()
        self.exempt = self.user.lower() in cfg.exempt_users

        self._mutex = threading.Lock()
        self._events: deque = deque(maxlen=MAX_QUEUED_EVENTS)
        self._boot_id = secrets.token_hex(4)
        self._counter = 0
        self._started = clock()
        self._poll_interval = 5.0
        self._last_ok: float | None = None
        self._lab_name = "Lab Komputer"
        self._warn_seconds = 300
        self._message = "Menghubungi server…"
        self._notice = ""
        self._locked, self._ends_at = self._load_state()
        self._logged_locked = None
        self._last_app: tuple[str, str] | None = None
        self._last_title_event = 0.0
        self._exe_sha_cache: str | None = None
        self._shot_interval = 0.0          # detik; 0 = server belum meminta screenshot
        self._next_shot = 0.0
        self._shot_error = ""

        self._state_file = Path(cfg.state_dir) / "state.json"
        self.add_event("agent_start", f"Agent v{VERSION} dimulai, user {self.user}")

    # state ---------------------------------------------------------------
    def _load_state(self):
        try:
            saved = json.loads((Path(self.cfg.state_dir) / "state.json").read_text())
            return bool(saved["locked"]), saved.get("ends_at")
        except (OSError, ValueError, KeyError):
            return True, None  # unknown → assume locked until the server says otherwise

    def _save_state(self):
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps({"locked": self._locked, "ends_at": self._ends_at}))
            os.replace(tmp, self._state_file)
        except OSError:
            pass  # state persistence is best effort

    def view(self, now: float | None = None) -> View:
        now = self._clock() if now is None else now
        with self._mutex:
            locked, ends_at, message = self._locked, self._ends_at, self._message
            if ends_at is not None and now >= ends_at:
                # The server told us when this state ends; keep enforcing it while offline.
                locked, ends_at = not locked, None
                message = "Saat ini di luar jadwal sesi lab." if locked else ""
            elif (locked and self._last_ok is None and self.cfg.offline_boot_policy == "unlock"
                  and now - self._started >= self.cfg.boot_grace_seconds):
                locked, message = False, ""
            connected = self._last_ok is not None and now - self._last_ok <= self._poll_interval * 3
            return View(
                locked=locked and not self.exempt,
                ends_at=ends_at,
                message=message,
                connected=connected,
                lab_name=self._lab_name,
                warn_seconds=self._warn_seconds,
                exempt=self.exempt,
                notice=self._notice,
            )

    # events --------------------------------------------------------------
    def add_event(self, type_: str, detail: str):
        with self._mutex:
            self._counter += 1
            self._events.append({
                "eid": f"{self._boot_id}-{self._counter}",
                "ts": self._clock(),
                "type": type_,
                "detail": detail[:300],
            })

    def sample(self):
        """Called every couple of seconds: logs app changes and lock transitions."""
        view = self.view()
        if view.locked != self._logged_locked:
            if self._logged_locked is not None or view.locked:
                self.add_event("lock" if view.locked else "unlock",
                               "Layar dikunci" if view.locked else "Kunci dibuka")
            self._logged_locked = view.locked
        if view.locked:
            return
        app, title = self.platform.foreground()
        app = app or ""
        if app.lower() in IGNORED_PROCESSES:
            return
        now = self._clock()
        changed_app = self._last_app is None or self._last_app[0] != app
        changed_title = self._last_app is not None and self._last_app[1] != title
        if changed_app or (changed_title and now - self._last_title_event >= TITLE_EVENT_MIN_GAP):
            shown = f"{app} | {title}" if self.cfg.log_titles and title else app
            self.add_event("app", shown)
            self._last_title_event = now
        self._last_app = (app, title)

    def _exe_sha(self) -> str:
        if self._exe_sha_cache is None:
            self._exe_sha_cache = getattr(self.platform, "exe_sha256", lambda: "")()
        return self._exe_sha_cache

    # network -------------------------------------------------------------
    def poll_once(self) -> bool:
        locked = self.view().locked
        app, title = ("(layar terkunci)", "") if locked else self.platform.foreground()
        with self._mutex:
            batch = list(self._events)[:EVENTS_PER_POLL]
        body = {
            "host": self.host,
            "user": self.user,
            "app": app,
            "title": title if self.cfg.log_titles else "",
            "idle": self.platform.idle_seconds(),
            "uptime": self.platform.uptime_seconds(),
            "mem": self.platform.memory_percent(),
            "locked": locked,
            "version": VERSION,
            "exe_sha": self._exe_sha(),
            "lan_ip": self._lan_ip(),
            "events": batch,
        }
        try:
            reply = self._post(self.cfg.server_url.rstrip("/") + "/api/agent/poll", body, self.cfg.token)
        except Exception:
            return False
        now = self._clock()
        with self._mutex:
            for _ in batch:
                self._events.popleft()
            self._last_ok = now
            self._locked = bool(reply["locked"])
            changes_in = reply.get("changes_in")
            self._ends_at = None if changes_in is None else now + int(changes_in)
            self._message = reply.get("message", "")
            self._notice = str(reply.get("notice") or "")[:300]
            self._poll_interval = max(2.0, float(reply.get("poll_interval", 5)))
            self._lab_name = reply.get("lab_name", self._lab_name)
            self._warn_seconds = int(reply.get("warn_seconds", self._warn_seconds))
            self._shot_interval = max(0.0, float(reply.get("screenshot_interval") or 0))
        self._save_state()
        for action in reply.get("commands", []):
            self.power(action)
        return True

    # screenshot ----------------------------------------------------------
    def screenshot_once(self) -> bool:
        """Kirim satu gambar layar bila sudah waktunya. Tidak pernah saat layar terkunci atau server tak terjangkau."""
        view, now = self.view(), self._clock()
        if self._shot_interval <= 0 or view.locked or not view.connected or now < self._next_shot:
            return False
        self._next_shot = now + self._shot_interval
        grab = getattr(self.platform, "screenshot", None)
        if grab is None:
            return False
        try:
            data = grab()
        except Exception as exc:
            if repr(exc) != self._shot_error:        # catat sekali per jenis galat, bukan tiap interval
                self._shot_error = repr(exc)
                self.add_event("info", f"Screenshot gagal: {exc!r}")
            return False
        if not data or self.view().locked:           # terkunci selagi mengambil gambar: buang
            return False
        self._shot_error = ""
        url = self.cfg.server_url.rstrip("/") + "/api/agent/screenshot?host=" + urllib.parse.quote(self.host)
        try:
            self._upload(url, data, self.cfg.token)
        except Exception:
            return False
        return True

    def run_screenshots(self, stop: threading.Event):
        """Utas terpisah supaya unggahan lambat tidak menunda pembaruan status kunci."""
        while not stop.wait(1.0):
            try:
                self.screenshot_once()
            except Exception:
                pass

    def power(self, action: str):
        if action in ("shutdown", "restart"):
            self.platform.power(action, self.cfg.force_shutdown)

    def user_power(self, action: str):
        """Shutdown/restart button on the lock screen: make sure the server hears about it first."""
        self.add_event("user_action", f"Tombol {action} ditekan di layar kunci")
        self.poll_once()
        self.power(action)

    def run_forever(self, stop: threading.Event):
        next_poll = 0.0
        while not stop.wait(self.cfg.sample_interval):
            try:
                self.sample()
                if self._clock() >= next_poll:
                    self.poll_once()
                    next_poll = self._clock() + self._poll_interval
            except Exception as exc:  # the loop must never die, or the lock stops updating
                self.add_event("info", f"Kesalahan internal agent: {exc!r}")
