"""
droidperf/i18n.py
-----------------
Minimal internationalisation (i18n) helper for the ADB Telemetry & Health Monitor.

Loads a JSON locale file from ``locale/<lang>.json`` relative to the project
root and exposes a single ``t(key, **fmt)`` function for translated strings.

The active language is read from ``settings_manager.settings.get("language")``
at first call and cached for the process lifetime.  Changing the language
requires an application restart (setting is saved to ``settings.json``).

Supported languages: ``"en"`` (default), ``"tr"``.

Public API:
    t(key, **fmt) -> str
    set_language(lang_code)
    current_language() -> str
"""

import json
import logging
from pathlib import Path
from typing import Dict

logger = logging.getLogger(__name__)

# Resolved once at import time relative to this file's parent's parent (project root).
_LOCALE_DIR = Path(__file__).parent.parent / "locale"

_SUPPORTED = {"en", "tr"}
_DEFAULT_LANG = "en"

# Module-level cache: language code → translation dict.
_cache: Dict[str, Dict[str, str]] = {}
_active_lang: str = ""


def _load(lang: str) -> Dict[str, str]:
    """
    Load and cache the translation dict for *lang*.

    Falls back to English if the requested locale file is missing.

    Args:
        lang (str): Two-letter language code (e.g. ``"en"``).

    Returns:
        Dict[str, str]: Key → translated string mapping.
    """
    if lang in _cache:
        return _cache[lang]

    path = _LOCALE_DIR / f"{lang}.json"
    if not path.exists():
        logger.warning("Locale file '%s' not found — falling back to English.", path)
        lang = _DEFAULT_LANG
        path = _LOCALE_DIR / f"{lang}.json"

    try:
        with open(path, encoding="utf-8") as fh:
            data: Dict[str, str] = json.load(fh)
        _cache[lang] = data
        logger.debug("Loaded locale '%s' (%d keys).", lang, len(data))
        return data
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("Failed to load locale '%s': %s — using empty dict.", lang, exc)
        _cache[lang] = {}
        return _cache[lang]


def _active() -> Dict[str, str]:
    """Return the translation dict for the currently active language."""
    global _active_lang
    if not _active_lang:
        try:
            from droidperf.settings_manager import settings
            _active_lang = settings.get("language", _DEFAULT_LANG)
        except Exception:  # pylint: disable=broad-except
            _active_lang = _DEFAULT_LANG
    return _load(_active_lang)


def t(key: str, **fmt) -> str:
    """
    Return the translated string for *key* in the active language.

    If the key is not found, returns the key itself (with a warning).
    Optional ``**fmt`` keyword arguments are applied via ``str.format()``.

    Args:
        key (str):   Translation key (e.g. ``"btn_start"``).
        **fmt:       Format substitutions (e.g. ``devices="A, B"``).

    Returns:
        str: Translated and optionally formatted string.

    Example::

        from droidperf.i18n import t
        label = t("msg_monitoring", devices="emulator-5554")
    """
    table = _active()
    value = table.get(key)
    if value is None:
        # Fallback: try English, then return the key itself.
        en = _load(_DEFAULT_LANG)
        value = en.get(key, key)
        if value == key:
            logger.debug("i18n: key '%s' not found in any locale.", key)
    if fmt:
        try:
            value = value.format(**fmt)
        except KeyError as exc:
            logger.warning("i18n format error for key '%s': %s", key, exc)
    return value


def set_language(lang_code: str) -> None:
    """
    Change the active language and persist to settings.

    Takes effect immediately for subsequent ``t()`` calls.
    A full UI rebuild or application restart is needed to update
    already-rendered widgets.

    Args:
        lang_code (str): Language code — one of ``"en"``, ``"tr"``.
    """
    global _active_lang
    if lang_code not in _SUPPORTED:
        logger.warning("Unsupported language '%s' — keeping '%s'.", lang_code, _active_lang)
        return
    _active_lang = lang_code
    try:
        from droidperf.settings_manager import settings
        settings.set("language", lang_code)
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("Could not persist language setting: %s", exc)
    logger.info("Language changed to '%s'.", lang_code)


def current_language() -> str:
    """
    Return the currently active language code.

    Returns:
        str: e.g. ``"en"`` or ``"tr"``.
    """
    _active()  # ensure _active_lang is initialised
    return _active_lang
