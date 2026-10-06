import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from labagent import config  # noqa: E402

INSTALL = Path(__file__).parent.parent / "deploy" / "install.ps1"
# parameter yang memang sengaja diisi ulang dari install.conf
INTENDED = {"servero", "serverurl", "token", "killfilename", "exemptusers", "offlinebootpolicy"}


def test_no_variable_collides_with_a_parameter():
    """PowerShell tidak membedakan huruf besar-kecil: `$config = ...` menimpa parameter `$Config` yang bertipe [string]
    sehingga config.json berisi teks "System.Collections.Specialized.OrderedDictionary" (agent crash: 'str' has no 'items')."""
    text = INSTALL.read_text(encoding="utf-8")
    block = re.search(r"param\((.*?)\n\)", text, re.S).group(1)
    params = {m.lower() for m in re.findall(r"\[[^\]]+\]\s*\$(\w+)", block)}
    body = text[text.index("\n)", text.index("param(")) + 2:]
    assigned = {m.lower() for m in re.findall(r"^\s*\$(\w+)\s*=", body, re.M)}
    assert params >= {"serverurl", "token", "configfile"}
    assert (params & assigned) <= INTENDED, params & assigned - INTENDED


def test_install_vm_cmd_passes_the_renamed_parameter():
    cmd = (INSTALL.parent / "install-vm.cmd").read_text()
    assert '-ConfigFile "%~dp0install-vm.conf"' in cmd


@pytest.mark.parametrize("content", ['"System.Collections.Specialized.OrderedDictionary"', "[]", "123", "null"])
def test_corrupt_config_gives_a_clear_error(tmp_path, content):
    path = tmp_path / "config.json"
    path.write_text(content)
    with pytest.raises(ValueError, match="rusak"):
        config.load(path)


def test_valid_config_still_loads(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"server_url": "http://x", "token": "t"}))
    assert config.load(path).server_url == "http://x"
