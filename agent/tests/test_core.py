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
        return Agent(cfg, platform, post=server, clock=clock)

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
