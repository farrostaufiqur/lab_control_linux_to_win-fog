import secrets
import tomllib
from dataclasses import dataclass
from pathlib import Path

TEMPLATE = """\
# Konfigurasi Lab Control Server
lab_name = "Lab Komputer"
timezone = "Asia/Jakarta"      # zona waktu untuk jadwal sesi

host = "0.0.0.0"
port = 8080
db_path = "labcontrol.db"

admin_user = "admin"
admin_password = "{admin_password}"   # password dashboard
agent_token = "{agent_token}"         # harus sama dengan token di semua client Windows

poll_interval = 5              # detik, seberapa sering agent lapor ke server
online_timeout = 20            # detik tanpa laporan sebelum PC dianggap offline
command_ttl = 120              # detik, perintah shutdown/restart kedaluwarsa jika PC offline
event_retention_days = 30      # log lebih lama dari ini dihapus otomatis
warn_seconds = 300             # client menampilkan peringatan sekian detik sebelum kunci

# Keamanan akses online (Cloudflare Tunnel)
block_agent_via_tunnel = true  # tolak /api/agent/* jika datang lewat Cloudflare; agent hanya boleh lewat LAN
max_login_failures = 5         # salah password sebanyak ini dari satu IP ...
login_block_seconds = 300      # ... memblokir IP itu selama sekian detik
"""


@dataclass(frozen=True)
class Config:
    lab_name: str
    timezone: str
    host: str
    port: int
    db_path: str
    admin_user: str
    admin_password: str
    agent_token: str
    poll_interval: int
    online_timeout: int
    command_ttl: int
    event_retention_days: int
    warn_seconds: int
    block_agent_via_tunnel: bool = True
    max_login_failures: int = 5
    login_block_seconds: int = 300


def write_default(path: Path) -> tuple[str, str]:
    admin_password = secrets.token_urlsafe(12)
    agent_token = secrets.token_urlsafe(24)
    path.write_text(TEMPLATE.format(admin_password=admin_password, agent_token=agent_token))
    path.chmod(0o600)
    return admin_password, agent_token


def load(path: Path) -> Config:
    raw = tomllib.loads(path.read_text())
    base = path.parent
    db_path = Path(raw.get("db_path", "labcontrol.db"))
    if not db_path.is_absolute():
        db_path = base / db_path
    cfg = Config(
        lab_name=raw.get("lab_name", "Lab Komputer"),
        timezone=raw.get("timezone", "Asia/Jakarta"),
        host=raw.get("host", "0.0.0.0"),
        port=int(raw.get("port", 8080)),
        db_path=str(db_path),
        admin_user=raw.get("admin_user", "admin"),
        admin_password=raw["admin_password"],
        agent_token=raw["agent_token"],
        poll_interval=int(raw.get("poll_interval", 5)),
        online_timeout=int(raw.get("online_timeout", 20)),
        command_ttl=int(raw.get("command_ttl", 120)),
        event_retention_days=int(raw.get("event_retention_days", 30)),
        warn_seconds=int(raw.get("warn_seconds", 300)),
        block_agent_via_tunnel=bool(raw.get("block_agent_via_tunnel", True)),
        max_login_failures=int(raw.get("max_login_failures", 5)),
        login_block_seconds=int(raw.get("login_block_seconds", 300)),
    )
    if len(cfg.admin_password) < 8 or len(cfg.agent_token) < 16:
        raise SystemExit("admin_password minimal 8 karakter dan agent_token minimal 16 karakter.")
    return cfg
