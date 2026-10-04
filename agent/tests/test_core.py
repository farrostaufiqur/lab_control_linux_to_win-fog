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
        cfg = AgentConfig(server_urls=("http://x",), token="t", state_dir=str(tmp_path), **overrides)
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
    cfg = AgentConfig(server_urls=("http://x",), token="t", state_dir=str(tmp_path / "n"),
                      offline_boot_policy="unlock", boot_grace_seconds=60)
    agent = Agent(cfg, platform, post=server, clock=clock)
    assert agent.view().locked is True
    clock.now += 61
    assert agent.view().locked is False


def test_exempt_user_is_never_locked(tmp_path):
    clock, server, platform = Clock(), FakeServer(), FakePlatform(user="Administrator")
    cfg = AgentConfig(server_urls=("http://x",), token="t", state_dir=str(tmp_path))
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
    agent = Agent(AgentConfig(server_urls=("http://x",), token="t", state_dir="/nonexistent-x"), platform,
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
    assert agent.screenshot_once() is True and platform.grabs == 2


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


def test_poll_and_screenshot_fall_back_to_second_server(tmp_path):
    clock, platform = Clock(), FakePlatform()
    shots(platform)
    urls, uploads = [], []

    def post(url, body, token):
        urls.append(url)
        if url.startswith("http://lan"):
            raise OSError("LAN tidak terjangkau")
        return {"locked": False, "changes_in": None, "commands": [], "screenshot_interval": 10}
    cfg = AgentConfig(server_urls=("http://lan", "https://net"), token="t", state_dir=str(tmp_path))
    agent = Agent(cfg, platform, post=post, clock=clock, lan_ip=lambda: "",
                  upload=lambda url, data, token: uploads.append(url))
    assert agent.poll_once() is True
    assert urls == ["http://lan/api/agent/poll", "https://net/api/agent/poll"]
    assert agent.screenshot_once() is True and uploads == ["https://net/api/agent/screenshot?host=PC-01"]
