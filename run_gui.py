"""
run_gui.py
----------
Entry point to launch the ADB Telemetry & Health Monitor desktop GUI.

Usage:
    python run_gui.py
"""
import logging
import sys


def main() -> None:
    """Configure logging and launch the CustomTkinter application."""
    # Read persisted log level from settings before configuring logging.
    try:
        from droidperf.settings_manager import settings as _settings
        _level_str: str = _settings.get("log_level", "INFO")
        _level = getattr(logging, _level_str.upper(), logging.INFO)
    except Exception:  # pylint: disable=broad-except
        _level = logging.INFO

    logging.basicConfig(
        level=_level,
        format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    try:
        from gui.app import App
    except ImportError as exc:
        print(f"Missing dependency: {exc}")
        print("Run: pip install -r requirements.txt")
        sys.exit(1)

    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
