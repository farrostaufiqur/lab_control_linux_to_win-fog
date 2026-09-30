"""PyInstaller entry point (keeps the labagent package importable when frozen)."""
from labagent.main import main

if __name__ == "__main__":
    main()
