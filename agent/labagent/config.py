import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import servers


@dataclass(frozen=True)
class AgentConfig:
    token: str
    server_urls: tuple = ()                 # kosong di config.json = pakai alamat yang ditanam saat build
    offline_boot_policy: str = "lock"       # what to do at startup if the server can't be reached: lock | unlock
    boot_grace_seconds: int = 60            # only used when offline_boot_policy == "unlock"
    log_titles: bool = True                 # include window titles in activity log
    force_shutdown: bool = True             # shutdown/restart with /f (closes apps without asking to save)
    always_block_taskmgr: bool = True       # keep Task Manager disabled for non-exempt users at all times
    exempt_users: tuple = field(default=("administrator", "admin", "labadmin"))
    state_dir: str = r"C:\ProgramData\LabAgent"
    kill_file_sha256: str = ""              # SHA-256 of the secret file name that removes the agent (empty = feature off)
    sample_interval: float = 2.0
    update_public_key: str = ""             # kunci publik Ed25519 (hex 64 karakter) untuk memverifikasi rilis agent; kosong = pembaruan otomatis mati


def default_path() -> Path:
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent.parent
    return base / "config.json"


def load(path: Path | None = None) -> AgentConfig:
    path = path or default_path()
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    known = AgentConfig.__dataclass_fields__
    values = {k: v for k, v in raw.items() if k in known}
    if isinstance(raw.get("server_url"), str):
        values["server_urls"] = (raw["server_url"],)
    if "server_urls" in values:
        values["server_urls"] = tuple(str(u) for u in values["server_urls"])
    else:
        values.setdefault("server_urls", tuple(servers.BUILT_IN))
    if "exempt_users" in values:
        values["exempt_users"] = tuple(str(u).lower() for u in values["exempt_users"])
    cfg = AgentConfig(**values)
    if not cfg.server_urls:
        raise ValueError("alamat server kosong: isi deploy\\build.conf lalu build ulang")
    if any(not u.startswith(("http://", "https://")) for u in cfg.server_urls):
        raise ValueError("alamat server harus diawali http:// atau https://")
    if cfg.offline_boot_policy not in ("lock", "unlock"):
        raise ValueError("offline_boot_policy harus 'lock' atau 'unlock'")
    key = cfg.update_public_key
    if key and (len(key) != 64 or any(c not in "0123456789abcdefABCDEF" for c in key)):
        raise ValueError("update_public_key harus 64 karakter heksadesimal")
    return cfg
