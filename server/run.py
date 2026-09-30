"""Entry point: python run.py [--config config.toml]

If the config file does not exist it is created with random secrets.
"""
import argparse
from pathlib import Path

from waitress import serve

from labserver import config
from labserver.app import create_app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(Path(__file__).parent / "config.toml"))
    path = Path(parser.parse_args().config).resolve()

    if not path.exists():
        password, token = config.write_default(path)
        print(f"Konfigurasi baru dibuat: {path}")
        print(f"  Password dashboard : {password}")
        print(f"  Token agent        : {token}")
        print("Simpan token ini, dibutuhkan saat memasang agent di client Windows.\n")

    cfg = config.load(path)
    print(f"Lab Control berjalan di http://{cfg.host}:{cfg.port} (zona waktu {cfg.timezone})")
    serve(create_app(cfg), host=cfg.host, port=cfg.port, threads=8)


if __name__ == "__main__":
    main()
