import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from labagent.config import AgentConfig  # noqa: E402
from labagent.core import Agent  # noqa: E402


class FakePlatform:
    def __init__(self, user="siswa"):
        self.user = user
        self.fg = ("notepad.exe", "Untitled")
        self.powered = []

    def hostname(self): return "PC-01"
    def username(self): return self.user
    def foreground(self): return self.fg
    def idle_seconds(self): return 3
    def uptime_seconds(self): return 100
    def memory_percent(self): return 40
    def power(self, action, force): self.powered.append((action, force))


class Clock:
    def __init__(self): self.now = 1_000_000.0
    def __call__(self): return self.now


class FakeServer:
    def __init__(self):
        self.reply = {"locked": False, "message": "", "changes_in": None, "commands": [],
                      "poll_interval": 5, "warn_seconds": 300, "lab_name": "Lab"}
        self.up = True
        self.bodies = []
        self.uploads = []

    def __call__(self, url, body, token):
        if not self.up:
            raise OSError("down")
        self.bodies.append(body)
        return dict(self.reply)


@pytest.fixture
def env(tmp_path):
    clock, server, platform = Clock(), FakeServer(), FakePlatform()

    def make(**overrides):
        cfg = AgentConfig(server_url="http://x", token="t", state_dir=str(tmp_path), **overrides)
        return Agent(cfg, platform, post=server, clock=clock,
                     upload=lambda url, data, token: server.uploads.append((url, data, token)),
                     lan_ip=lambda: "192.168.1.50")

    return clock, server, platform, make


def test_starts_locked_until_server_answers(env):
    _, server, _, make = env
    agent = make()
    assert agent.view().locked is True
    assert agent.poll_once() is True
    assert agent.view().locked is False and agent.view().connected


def test_locked_reply_includes_countdown_and_message(env):
    clock, server, _, make = env
    agent = make()
    server.reply.update(locked=True, message="Di luar jadwal", changes_in=600)
    agent.poll_once()
    view = agent.view()
    assert view.locked and view.message == "Di luar jadwal"
    assert view.ends_at == clock.now + 600


def test_unlocked_agent_relocks_itself_at_deadline_even_when_server_is_down(env):
    clock, server, _, make = env
    agent = make()
    server.reply.update(locked=False, changes_in=120)
    agent.poll_once()
    server.up = False
    clock.now += 119
    assert agent.view().locked is False
    clock.now += 2
    view = agent.view()
    assert view.locked is True and view.ends_at is None and "jadwal" in view.message


def test_locked_agent_opens_at_deadline_offline(env):
    clock, server, _, make = env
    agent = make()
    server.reply.update(locked=True, changes_in=60)
    agent.poll_once()
    server.up = False
    clock.now += 61
    assert agent.view().locked is False


def test_restart_of_agent_keeps_previous_lock_state(env):
    clock, server, _, make = env
    first = make()
    server.reply.update(locked=False, changes_in=300)
    first.poll_once()
    clock.now += 400          # user kills the agent; watchdog restarts it after the session ended
    server.up = False
    second = make()
    assert second.view().locked is True


def test_boot_offline_policy_lock_vs_unlock(env):
    clock, server, _, make = env
    server.up = False
    locked = make(offline_boot_policy="lock")
    clock.now += 3600
    assert locked.view().locked is True


def test_boot_offline_policy_unlock_after_grace(tmp_path):
    clock, server, platform = Clock(), FakeServer(), FakePlatform()
    server.up = False
    cfg = AgentConfig(server_url="http://x", token="t", state_dir=str(tmp_path / "n"),
                      offline_boot_policy="unlock", boot_grace_seconds=60)
    agent = Agent(cfg, platform, post=server, clock=clock)
    assert agent.view().locked is True
    clock.now += 61
    assert agent.view().locked is False


def test_exempt_user_is_never_locked(tmp_path):
    clock, server, platform = Clock(), FakeServer(), FakePlatform(user="Administrator")
    cfg = AgentConfig(server_url="http://x", token="t", state_dir=str(tmp_path))
    agent = Agent(cfg, platform, post=server, clock=clock)
    server.reply.update(locked=True, changes_in=None)
    agent.poll_once()
    assert agent.view().locked is False


def test_commands_from_server_are_executed(env):
    _, server, platform, make = env
    agent = make(force_shutdown=True)
    server.reply["commands"] = ["restart"]
    agent.poll_once()
    assert platform.powered == [("restart", True)]


def test_events_are_sent_once_and_kept_when_offline(env):
    clock, server, platform, make = env
    agent = make()
    agent.poll_once()
    server.up = False
    platform.fg = ("chrome.exe", "Google")
    agent.sample()
    assert agent.poll_once() is False
    server.up = True
    agent.poll_once()
    agent.poll_once()
    sent = [e for body in server.bodies for e in body["events"]]
    ids = [e["eid"] for e in sent]
    assert len(ids) == len(set(ids))
    assert any(e["type"] == "app" and e["detail"].startswith("chrome.exe") for e in sent)
    assert server.bodies[-1]["events"] == []


def test_app_events_only_on_change_and_titles_can_be_redacted(env):
    clock, server, platform, make = env
    agent = make(log_titles=False)
    agent.poll_once()
    for _ in range(3):
        agent.sample()
    platform.fg = ("chrome.exe", "Rahasia")
    clock.now += 1
    agent.sample()
    agent.sample()
    details = [e["detail"] for e in agent._events if e["type"] == "app"]
    assert details == ["notepad.exe", "chrome.exe"]


def test_no_app_events_while_locked_and_lock_transition_logged(env):
    clock, server, platform, make = env
    agent = make()
    server.reply.update(locked=True)
    agent.poll_once()
    platform.fg = ("games.exe", "Play")
    agent.sample()
    types = [e["type"] for e in agent._events]
    assert "app" not in types and "lock" in types


def test_user_power_button_reports_then_powers_off(env):
    _, server, platform, make = env
    agent = make()
    agent.user_power("shutdown")
    assert platform.powered == [("shutdown", True)]
    assert any(e["type"] == "user_action" for e in server.bodies[-1]["events"])


def test_status_reports_placeholder_while_locked(env):
    _, server, platform, make = env
    agent = make()
    server.reply.update(locked=True)
    agent.poll_once()
    agent.poll_once()
    assert server.bodies[-1]["app"] == "(layar terkunci)" and server.bodies[-1]["locked"] is True


def test_poll_reports_exe_sha_when_platform_knows_it():
    platform = FakePlatform()
    platform.exe_sha256 = lambda: "ab" * 32
    sent = []
    agent = Agent(AgentConfig(server_url="http://x", token="t", state_dir="/nonexistent-x"), platform,
                  post=lambda url, body, token: sent.append(body) or {"locked": False}, clock=Clock())
    assert agent.poll_once()
    assert sent[0]["exe_sha"] == "ab" * 32


# --- screenshot berkala -------------------------------------------------------------
def shots(platform, data=b"\xff\xd8\xffgambar"):
    platform.grabs = 0

    def screenshot():
        platform.grabs += 1
        if isinstance(data, Exception):
            raise data
        return data
    platform.screenshot = screenshot


def test_poll_reports_lan_ip(env):
    _, server, _, make = env
    make().poll_once()
    assert server.bodies[-1]["lan_ip"] == "192.168.1.50"


def test_screenshot_sent_at_interval_while_unlocked(env):
    clock, server, platform, make = env
    shots(platform)
    server.reply["screenshot_interval"] = 30
    agent = make()
    assert agent.screenshot_once() is False          # belum pernah terhubung: tidak ada gambar
    agent.poll_once()
    assert agent.screenshot_once() is True
    url, data, token = server.uploads[0]
    assert url == "http://x/api/agent/screenshot?host=PC-01" and data.startswith(b"\xff\xd8") and token == "t"
    assert agent.screenshot_once() is False          # belum waktunya
    clock.now += 31
    agent.poll_once()
    assert agent.screenshot_once() is False and platform.grabs == 2    # diambil lagi, tetapi sama: tidak dikirim
    platform.screenshot = lambda: b"\xff\xd8\xffgambar-baru"
    clock.now += 31
    agent.poll_once()
    assert agent.screenshot_once() is True and len(server.uploads) == 2


def test_no_screenshot_when_locked_or_disabled(env):
    clock, server, platform, make = env
    shots(platform)
    agent = make()
    agent.poll_once()                                # server tidak mengirim screenshot_interval
    assert agent.screenshot_once() is False and platform.grabs == 0
    server.reply.update(screenshot_interval=10, locked=True)
    agent.poll_once()
    assert agent.screenshot_once() is False and platform.grabs == 0
    server.reply["locked"] = False
    agent.poll_once()
    assert agent.screenshot_once() is True


def test_no_screenshot_while_server_unreachable(env):
    clock, server, platform, make = env
    shots(platform)
    server.reply["screenshot_interval"] = 10
    agent = make()
    agent.poll_once()
    server.up = False
    clock.now += 60
    agent.poll_once()
    assert agent.screenshot_once() is False and platform.grabs == 0


def test_screenshot_failure_logged_once(env):
    clock, server, platform, make = env
    shots(platform, RuntimeError("layar tidak tersedia"))
    server.reply["screenshot_interval"] = 10
    agent = make()
    agent.poll_once()
    for _ in range(3):
        assert agent.screenshot_once() is False
        clock.now += 11
        agent.poll_once()
    details = [e["detail"] for body in server.bodies for e in body["events"]]
    assert sum("Screenshot gagal" in d for d in details) == 1


def test_platform_without_screenshot_support(env):
    _, server, _, make = env
    server.reply["screenshot_interval"] = 10
    agent = make()
    agent.poll_once()
    assert agent.screenshot_once() is False and server.uploads == []


def test_requests_carry_own_user_agent():
    from labagent import VERSION
    from labagent.core import agent_headers
    assert agent_headers("t")["User-Agent"] == f"LabAgent/{VERSION}"


def test_unchanged_screenshot_not_resent_until_keepalive(env):
    clock, server, platform, make = env
    shots(platform)
    server.reply["screenshot_interval"] = 10
    agent = make()
    agent.poll_once()
    assert agent.screenshot_once() is True
    for _ in range(3):                               # layar sama persis: tidak ada unggahan baru
        clock.now += 11
        agent.poll_once()
        assert agent.screenshot_once() is False
    assert len(server.uploads) == 1
    shots(platform, b"\xff\xd8\xffberubah")           # layar berubah: langsung dikirim
    clock.now += 11
    agent.poll_once()
    assert agent.screenshot_once() is True and len(server.uploads) == 2
    clock.now += 125                                  # gambar sama, tetapi sudah lama: kirim ulang sebagai penanda hidup
    agent.poll_once()
    assert agent.screenshot_once() is True and len(server.uploads) == 3


def test_screenshot_resent_after_being_disabled_and_enabled(env):
    clock, server, platform, make = env
    shots(platform)
    server.reply["screenshot_interval"] = 10
    agent = make()
    agent.poll_once()
    assert agent.screenshot_once() is True
    server.reply["screenshot_interval"] = 0           # server membuang gambar saat dimatikan
    clock.now += 11
    agent.poll_once()
    server.reply["screenshot_interval"] = 10
    clock.now += 11
    agent.poll_once()
    assert agent.screenshot_once() is True and len(server.uploads) == 2


def test_failed_upload_is_retried_even_if_image_is_the_same(env):
    clock, server, platform, make = env
    shots(platform)
    server.reply["screenshot_interval"] = 10
    attempts = []

    def flaky(url, data, token):
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError("jaringan putus")
    agent = Agent(AgentConfig(server_url="http://x", token="t", state_dir="/nonexistent-x"), platform,
                  post=server, clock=clock, upload=flaky, lan_ip=lambda: "")
    agent.poll_once()
    assert agent.screenshot_once() is False
    clock.now += 11
    agent.poll_once()
    assert agent.screenshot_once() is True and len(attempts) == 2


# --- folder bersama -------------------------------------------------------------------------------
class SharedWorld:
    """Agent dengan klien folder bersama tiruan; server tiruan mengembalikan aturan lewat balasan poll."""

    def __init__(self, tmp_path, mode="receive", path="Desktop/shared", server_url="http://192.168.1.17:8080"):
        self.clock, self.platform = Clock(), FakePlatform()
        self.profile = tmp_path / "profil"
        self.platform.profile_dir = lambda: str(self.profile)
        self.files = {}                                  # isi folder pusat tiruan
        self.reply = {"locked": False, "message": "", "changes_in": None, "commands": [], "poll_interval": 5,
                      "warn_seconds": 300, "lab_name": "Lab", "shared": {"mode": mode, "path": path, "interval": 10}}
        self.bodies = []
        world = self

        class Client:
            def __init__(self, url, token, host): world.client_args = (url, token, host)
            def index(self):
                import hashlib
                return {p: {"sha256": hashlib.sha256(d).hexdigest(), "size": len(d), "mtime": 1_700_000_000.0}
                        for p, d in world.files.items()}
            def download(self, path): yield world.files[path]
            def upload(self, *a): raise AssertionError("tidak boleh mengunggah")
            def delete(self, *a): raise AssertionError("tidak boleh menghapus")

        def post(url, body, token):
            self.bodies.append(body)
            return dict(self.reply)
        cfg = AgentConfig(server_url=server_url, token="t", state_dir=str(tmp_path / "state"))
        self.agent = Agent(cfg, self.platform, post=post, clock=self.clock, lan_ip=lambda: "",
                           shared_client=Client, is_local=lambda url: url.startswith("http://192.168."))

    def poll(self):
        assert self.agent.poll_once()


def test_shared_follows_server_rules_and_logs_summary(tmp_path):
    w = SharedWorld(tmp_path)
    assert w.agent.shared_once() is False                # belum pernah terhubung: tidak menyinkronkan
    w.poll()
    w.files["tugas/a.txt"] = b"isi"
    assert w.agent.shared_once() is True
    assert (w.profile / "Desktop" / "shared" / "tugas" / "a.txt").read_bytes() == b"isi"
    assert w.client_args == ("http://192.168.1.17:8080", "t", "PC-01")
    w.poll()
    shared = w.bodies[-1]["shared"]
    assert shared == {"ok": True, "files": 1, "error": ""}
    details = [e["detail"] for body in w.bodies for e in body["events"]]
    w.poll()
    details += [e["detail"] for e in w.bodies[-1]["events"]]
    assert "Folder bersama: 1 diunduh" in details


def test_shared_off_or_without_path_does_nothing(tmp_path):
    for mode, path in (("off", "Desktop/shared"), ("both", "")):
        w = SharedWorld(tmp_path / mode, mode=mode, path=path)
        w.poll()
        w.files["a"] = b"x"
        assert w.agent.shared_once() is False
        assert not (w.profile).exists() and w.bodies[-1]["shared"] is None


def test_shared_never_runs_through_non_lan_server(tmp_path):
    w = SharedWorld(tmp_path, server_url="https://lab.fog.tf")
    w.poll()
    w.files["a"] = b"x"
    assert w.agent.shared_once() is False and not w.profile.exists()
    w.poll()
    status = w.bodies[-1]["shared"]
    assert status["ok"] is False and "LAN" in status["error"]


def test_shared_rejects_unsafe_folder_from_server(tmp_path):
    for bad in ("../x", "C:/Windows", "a/../b"):
        w = SharedWorld(tmp_path / bad.replace("/", "_").replace(":", "_"), path=bad)
        w.poll()
        w.files["a"] = b"x"
        assert w.agent.shared_once() is False
        w.poll()
        assert w.bodies[-1]["shared"]["error"] == "folder tujuan tidak valid"


def test_shared_error_logged_once_and_recovers(tmp_path):
    w = SharedWorld(tmp_path)
    w.poll()
    boom = {"on": True}
    original = w.agent._shared_client

    class Broken:
        def __init__(self, *a): pass
        def index(self):
            if boom["on"]:
                raise OSError("server menolak")
            return {}
    w.agent._shared_client = Broken
    for _ in range(3):
        assert w.agent.shared_once() is False
    w.poll()
    details = [e["detail"] for e in w.bodies[-1]["events"] if "Folder bersama" in e["detail"]]
    assert len(details) == 1 and "server menolak" in details[0]
    boom["on"] = False
    w.agent.shared_once()
    w.poll()
    assert w.bodies[-1]["shared"]["ok"] is True


def test_poll_reply_clamps_shared_interval_and_ignores_garbage(tmp_path):
    w = SharedWorld(tmp_path)
    w.reply["shared"] = {"mode": "both", "path": "Desktop/s", "interval": 1}
    w.poll()
    assert w.agent._shared_cfg == {"mode": "both", "path": "Desktop/s", "interval": 5.0}
    w.reply["shared"] = {"mode": "terserah", "path": "x"}
    w.poll()
    assert w.agent._shared_cfg["mode"] == "both"                 # balasan aneh diabaikan
