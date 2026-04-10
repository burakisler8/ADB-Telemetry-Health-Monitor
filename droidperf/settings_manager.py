"""
droidperf/settings_manager.py
------------------------------
Persistent application settings backed by a JSON file.

All configurable values live here so that widgets never hard-code
defaults.  The settings file is written to the project root as
``settings.json`` and is created with sensible defaults on first run.

Public API:
    get(key, default=None) -> Any
    set(key, value)        -> None
    save()                 -> None
    load()                 -> None
    all()                  -> dict
"""

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Resolved once at import time — always relative to this file's package root.
_SETTINGS_PATH = Path(__file__).parent.parent / "settings.json"

_DEFAULTS: dict = {
    # Monitoring
    "default_interval": 5,
    "rolling_window": 60,
    "auto_mode": True,
    # Output
    "output_dir": "reports",
    "log_level": "INFO",
    # Alerts
    "alert_ram_kb": 0,          # 0 = disabled
    "alert_cpu_pct": 0,         # 0 = disabled
    "alert_temp_c": 0,          # 0 = disabled
    "alert_batt_drop": 0,       # 0 = disabled
    "spike_std_multiplier": 3.0,
    # Notifications
    "webhook_url": "",
    "os_notifications": False,
    "crash_screenshots": True,   # capture device screenshot on crash/ANR
    # Reports
    "report_retention_days": 0,  # 0 = keep forever
    "report_tag_default": "",
    # Presets (list of {name, packages})
    "presets": [],
    # System
    "minimize_to_tray": False,
    "wifi_adb_history": [],      # list of "host:port" strings
    "language": "en",            # "en" | "tr"
    # Scheduler
    "schedule_enabled": False,
    "schedule_time": "02:00",       # HH:MM
    "schedule_duration_min": 30,
    "schedule_repeat": "daily",     # "daily" | "once"
}


class _SettingsManager:
    """
    Singleton that manages reading and writing ``settings.json``.

    Merges stored values over defaults so new keys added in future
    versions are always present without breaking existing config files.
    """

    def __init__(self) -> None:
        self._data: dict = dict(_DEFAULTS)
        self.load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        """
        Return the value for *key*, or *default* if not found.

        Args:
            key (str):     Settings key.
            default (Any): Fallback when key is absent.
        """
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        """
        Update *key* in memory and persist immediately.

        Args:
            key (str):   Settings key.
            value (Any): New value.
        """
        self._data[key] = value
        self.save()

    def all(self) -> dict:
        """Return a shallow copy of all settings."""
        return dict(self._data)

    def load(self) -> None:
        """
        Load settings from disk, merging over defaults.

        Creates ``settings.json`` with defaults if the file does not exist.
        """
        if not _SETTINGS_PATH.exists():
            self.save()
            logger.info("Created default settings.json at '%s'.", _SETTINGS_PATH)
            return
        try:
            with open(_SETTINGS_PATH, encoding="utf-8") as fh:
                stored = json.load(fh)
            # Merge: defaults first, then stored (preserves unknown future keys).
            self._data = {**_DEFAULTS, **stored}
            logger.debug("Settings loaded from '%s'.", _SETTINGS_PATH)
        except (OSError, json.JSONDecodeError) as exc:
            logger.error("Failed to load settings ('%s'): %s — using defaults.", _SETTINGS_PATH, exc)
            self._data = dict(_DEFAULTS)

    def save(self) -> None:
        """Persist current settings to ``settings.json``."""
        try:
            with open(_SETTINGS_PATH, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, indent=2, ensure_ascii=False)
            logger.debug("Settings saved to '%s'.", _SETTINGS_PATH)
        except OSError as exc:
            logger.error("Failed to save settings: %s", exc)


# Module-level singleton — import and use directly.
settings = _SettingsManager()
