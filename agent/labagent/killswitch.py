"""Pemulihan darurat oleh admin: mencopot agent bila drive berisi file dengan nama tertentu dipasang.

Untuk saat server rusak dan PC lab tidak bisa dibuka. Berjalan sebagai proses terpisah
(`labagent.exe --watch-usb`, akun SYSTEM) karena agent biasa berjalan sebagai user standar dan
tidak berhak menghapus tugas terjadwal maupun folder pemasangannya.

Yang disimpan di PC hanya SHA-256 nama file (config.json). Tiap drive dibaca sekali per pemasangan,
dan hanya nama file di tingkat pertama.
"""
import hashlib
import threading
from typing import Callable, Mapping

WATCH_INTERVAL = 3.0


def name_hash(name: str) -> str:
    """Windows file names are case-insensitive, so the hash ignores case and surrounding spaces."""
    return hashlib.sha256(name.strip().lower().encode("utf-8")).hexdigest()


class DriveWatcher:
    def __init__(
        self,
        expected_sha256: str,
        list_drives: Callable[[], Mapping[str, object]],
        list_names: Callable[[str], list[str] | None],
    ):
        """`list_drives` -> {drive root: volume identity}; `list_names(root)` -> first-level names, or None if unreadable."""
        self._expected = expected_sha256.strip().lower()
        self._list_drives, self._list_names = list_drives, list_names
        self._seen: dict[str, object] = {}

    def scan(self) -> str | None:
        """Returns the drive root that carried the secret file, else None."""
        current = dict(self._list_drives())
        self._seen = {root: ident for root, ident in self._seen.items() if root in current}
        for root, ident in current.items():
            if self._seen.get(root) == ident:
                continue  # already read while this drive stayed plugged in
            names = self._list_names(root)
            if names is None:
                continue  # not readable yet (media still loading): try again on the next scan
            self._seen[root] = ident
            if any(name_hash(name) == self._expected for name in names):
                return root
        return None


def watch_forever(watcher: DriveWatcher, on_trigger: Callable[[str], None],
                  stop: threading.Event, interval: float = WATCH_INTERVAL) -> None:
    while not stop.is_set():
        try:
            root = watcher.scan()
            if root is not None:
                on_trigger(root)
                return
        except Exception:
            pass  # a flaky drive must never stop the watcher
        stop.wait(interval)


def cleanup_script(*dirs: str) -> str:
    """Batch yang menghapus folder-folder setelah exe yang sedang jalan melepaskannya."""
    lines = ["@echo off"]
    for i, d in enumerate(dirs):
        lines += [
            "for /l %%i in (1,1,20) do (",
            f'  rmdir /s /q "{d}" 2>nul',
            f'  if not exist "{d}" goto done{i}',
            "  ping -n 3 127.0.0.1 >nul",
            ")",
            f":done{i}",
        ]
    lines.append('del "%~f0"')
    return "\r\n".join(lines) + "\r\n"
