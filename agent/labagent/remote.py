"""Menjalankan perintah shell kiriman admin (dipanggil pembantu SYSTEM, bukan agent user).

Skrip ditulis ke folder pemasangan (hanya admin yang bisa menulis) supaya user biasa tidak bisa menyelipkan
isi lain antara penulisan dan eksekusi. Keluaran diarahkan ke berkas dan hanya ekor terakhirnya yang dikirim,
jadi perintah yang membanjiri keluaran tidak menghabiskan memori. Proses yang melewati batas waktu dihentikan
bersama seluruh anak prosesnya.
"""
import os
import subprocess
import sys
from pathlib import Path

SHELLS = ("cmd", "powershell")
MAX_SCRIPT_CHARS = 4000
MAX_TIMEOUT = 900
OUTPUT_CHARS = 16000
FAILED_TO_START = -1


def valid_job(job) -> bool:
    return (isinstance(job, dict) and isinstance(job.get("id"), int) and job.get("shell") in SHELLS
            and isinstance(job.get("script"), str) and 0 < len(job["script"]) <= MAX_SCRIPT_CHARS
            and isinstance(job.get("timeout"), int) and 1 <= job["timeout"] <= MAX_TIMEOUT)


def windows_command(shell: str, path: Path) -> tuple[str, bytes]:
    """(baris perintah, isi berkas skrip). Keluaran dipaksa UTF-8 supaya huruf non-ASCII tidak rusak."""
    if shell == "powershell":
        return (f'powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{path}"',
                b"\xef\xbb\xbf[Console]::OutputEncoding = [Text.Encoding]::UTF8\r\n")
    return f'cmd.exe /d /s /c ""{path}""', b"@echo off\r\nchcp 65001 >nul\r\n"


def _tail(path: Path) -> str:
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            f.seek(max(0, size - OUTPUT_CHARS * 4))
            text = f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    text = text.replace("\r\n", "\n")
    return ("…(dipotong)\n" + text[-OUTPUT_CHARS:]) if len(text) > OUTPUT_CHARS else text


def _kill_tree(proc) -> None:
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=30,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    proc.kill()


def run_script(job: dict, work_dir, build=windows_command) -> tuple[int, str, bool]:
    """Menjalankan satu perintah. Mengembalikan (kode keluar, keluaran, waktu habis). `build` diganti saat tes."""
    folder = Path(work_dir)
    folder.mkdir(parents=True, exist_ok=True)
    suffix = ".ps1" if job["shell"] == "powershell" else ".cmd"
    script, out = folder / f"job-{job['id']}{suffix}", folder / f"job-{job['id']}.out"
    command, header = build(job["shell"], script)
    script.write_bytes(header + job["script"].replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8"))
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        with open(out, "wb") as sink:
            proc = subprocess.Popen(command, stdout=sink, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    cwd=str(folder), creationflags=flags, shell=False)
            try:
                code, timed_out = proc.wait(timeout=job["timeout"]), False
            except subprocess.TimeoutExpired:
                _kill_tree(proc)
                proc.wait(timeout=30)
                code, timed_out = proc.returncode, True
        return code, _tail(out), timed_out
    except OSError as exc:
        return FAILED_TO_START, f"gagal memulai: {exc}", False
    finally:
        for leftover in (script, out):
            try:
                leftover.unlink()
            except OSError:
                pass
