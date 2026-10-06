"""Logika pembaruan agent yang tidak bergantung pada Windows (bisa diuji di mana saja).

Rilis datang dari server (diunggah admin lewat dashboard). Agent hanya memastikan unduhan utuh:
ukuran dan SHA-256 harus cocok dengan manifest dan awalan file `MZ` (exe Windows). Tidak ada tanda tangan.
"""
import hashlib
import re

VERSION_RE = re.compile(r"^[0-9A-Za-z._-]{1,32}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_RELEASE_BYTES = 200 * 1024 * 1024
VERIFY_WAIT_SECONDS = 30


def parse_manifest(raw) -> dict | None:
    """Memeriksa bentuk manifest dari server. Mengembalikan dict bersih atau None."""
    if not isinstance(raw, dict):
        return None
    version, sha, size = raw.get("version"), raw.get("sha256"), raw.get("size")
    if not (isinstance(version, str) and VERSION_RE.match(version)):
        return None
    if not (isinstance(sha, str) and SHA_RE.match(sha)):
        return None
    if not (isinstance(size, int) and 0 < size <= MAX_RELEASE_BYTES):
        return None
    return {"version": version, "sha256": sha, "size": size}


def file_sha256(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_windows_exe(path) -> bool:
    with open(path, "rb") as f:
        return f.read(2) == b"MZ"


def apply_script(install_dir: str, new_sha: str) -> str:
    """Batch yang menukar exe, menyalakan ulang agent, lalu memverifikasi; gagal = kembali ke exe lama.

    Dijalankan terlepas dari helper (helper keluar dulu karena exe-nya sendiri tidak bisa ditimpa).
    Helper baru menulis `helper.ok` berisi SHA-256 exe-nya; bila tidak muncul, exe lama dipulihkan
    dan SHA baru dicatat di `update-failed.txt` agar tidak dicoba lagi.
    """
    d = install_dir.rstrip("\\")
    lines = [
        "@echo off",
        "setlocal",
        f'set "DIR={d}"',
        f'set "NEWSHA={new_sha}"',
        "schtasks /Change /TN LabAgent /DISABLE >nul 2>&1",
        "schtasks /Change /TN LabAgentKillSwitch /DISABLE >nul 2>&1",
        "set /a TRIES=0",
        ":swap",
        "set /a TRIES+=1",
        "taskkill /F /IM labagent.exe >nul 2>&1",
        'move /y "%DIR%\\labagent.exe" "%DIR%\\labagent.bak" >nul 2>&1',
        'if not exist "%DIR%\\labagent.exe" goto placed',
        "if %TRIES% GEQ 15 goto unchanged",
        "ping -n 3 127.0.0.1 >nul",
        "goto swap",
        ":placed",
        'move /y "%DIR%\\labagent.new" "%DIR%\\labagent.exe" >nul 2>&1',
        'if not exist "%DIR%\\labagent.exe" goto rollback',
        "call :start_tasks",
        f"ping -n {VERIFY_WAIT_SECONDS + 1} 127.0.0.1 >nul",
        'findstr /c:"%NEWSHA%" "%DIR%\\helper.ok" >nul 2>&1',
        "if errorlevel 1 goto rollback",
        'del /f /q "%DIR%\\labagent.new" >nul 2>&1',
        "goto done",
        ":rollback",
        "schtasks /Change /TN LabAgent /DISABLE >nul 2>&1",
        "taskkill /F /IM labagent.exe >nul 2>&1",
        "ping -n 3 127.0.0.1 >nul",
        'del /f /q "%DIR%\\labagent.exe" >nul 2>&1',
        'move /y "%DIR%\\labagent.bak" "%DIR%\\labagent.exe" >nul 2>&1',
        '>"%DIR%\\update-failed.txt" echo %NEWSHA%',
        ":unchanged",
        'del /f /q "%DIR%\\labagent.new" >nul 2>&1',
        "call :start_tasks",
        ":done",
        "schtasks /Delete /TN LabAgentUpdate /F >nul 2>&1",
        'del "%~f0"',
        "exit /b 0",
        ":start_tasks",
        "schtasks /Change /TN LabAgent /ENABLE >nul 2>&1",
        "schtasks /Change /TN LabAgentKillSwitch /ENABLE >nul 2>&1",
        "schtasks /Run /TN LabAgentHelper >nul 2>&1",
        "schtasks /Run /TN LabAgentKillSwitch >nul 2>&1",
        "schtasks /Run /TN LabAgent >nul 2>&1",
        "exit /b 0",
    ]
    return "\r\n".join(lines) + "\r\n"

