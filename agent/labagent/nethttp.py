"""Header HTTP bersama untuk semua permintaan agent ke server."""
from . import VERSION


def agent_headers(token: str, content_type: str = "application/json") -> dict:
    # Cloudflare menolak User-Agent bawaan urllib ("Python-urllib", error 1010), jadi pakai nama sendiri.
    return {"Content-Type": content_type, "X-Agent-Token": token, "User-Agent": f"LabAgent/{VERSION}"}
