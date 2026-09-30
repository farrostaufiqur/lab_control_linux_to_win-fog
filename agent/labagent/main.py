import argparse
import sys
import threading
from pathlib import Path

from . import config
from .core import Agent


def watch_usb(cfg: config.AgentConfig, config_path: Path) -> None:
    """Proses SYSTEM terpisah: mencopot agent bila drive berisi file rahasia dipasang."""
    from . import killswitch, winapi

    if not cfg.kill_file_sha256:
        return  # fitur mati
    guard = winapi.acquire_single_instance("Global\\LabAgentKillSwitchSingleton")
    if guard is None:
        return
    install_dir = str(config_path.parent)
    watcher = killswitch.DriveWatcher(cfg.kill_file_sha256, winapi.list_drives, winapi.list_names)
    killswitch.watch_forever(
        watcher, lambda _root: winapi.remove_agent(install_dir, cfg.state_dir), threading.Event())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--watch-usb", action="store_true", help="mode pemulihan darurat (dijalankan sebagai SYSTEM)")
    args = parser.parse_args()

    if sys.platform != "win32":
        raise SystemExit("Agent ini hanya berjalan di Windows.")

    if args.watch_usb:
        path = args.config or config.default_path()
        watch_usb(config.load(path), path)
        return

    from . import winapi
    from .overlay import Overlay

    guard = winapi.acquire_single_instance()
    if guard is None:
        return  # another copy is already running in this session (the watchdog task fires every minute)

    cfg = config.load(args.config)
    agent = Agent(cfg, winapi.WindowsPlatform())
    stop = threading.Event()
    threading.Thread(target=agent.run_forever, args=(stop,), daemon=True).start()
    try:
        Overlay(agent, winapi, cfg).run()
    finally:
        stop.set()


if __name__ == "__main__":
    main()
