import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from labagent import config, servers  # noqa: E402
from labagent.servers import Servers  # noqa: E402

LAN, NET = "http://192.168.1.17:8080", "https://lab.fog.tf"


def test_uses_first_reachable_and_sticks_to_it():
    s, calls, down = Servers([LAN + "/", NET]), [], {LAN}

    def request(base):
        calls.append(base)
        if base in down:
            raise OSError("tidak terjangkau")
        return base
    assert s.call(request) == NET and calls == [LAN, NET]
    calls.clear()
    assert s.call(request) == NET and calls == [NET]        # tidak mencoba LAN tiap kali
    down = {NET}
    calls.clear()
    assert s.call(request) == LAN and calls == [NET, LAN] and s.active == LAN


def test_all_down_raises_last_error():
    def request(base):
        raise OSError(base)
    with pytest.raises(OSError, match="lab.fog.tf"):
        Servers([LAN, NET]).call(request)


def test_config_uses_built_in_urls(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"token": "t"}))
    monkeypatch.setattr(servers, "BUILT_IN", (LAN, NET))
    assert config.load(path).server_urls == (LAN, NET)
    monkeypatch.setattr(servers, "BUILT_IN", ())
    with pytest.raises(ValueError, match="build.conf"):
        config.load(path)


def test_config_json_can_override_urls(tmp_path, monkeypatch):
    monkeypatch.setattr(servers, "BUILT_IN", (LAN,))
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"token": "t", "server_url": NET}))
    assert config.load(path).server_urls == (NET,)
    path.write_text(json.dumps({"token": "t", "server_urls": ["ftp://x"]}))
    with pytest.raises(ValueError):
        config.load(path)
