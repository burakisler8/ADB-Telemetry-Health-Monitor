"""
droidperf/collectors/battery_stats.py
---------------------------------------
Collects per-app battery attribution from a connected Android device.

Uses ``adb shell dumpsys batterystats`` to parse the "Estimated power use"
section, which lists mAh consumed by each UID/package since the last reset.

Call ``reset_battery_stats()`` at session start to zero out the counters so
subsequent measurements reflect only the monitored session.  Call
``get_battery_attribution()`` at session end to retrieve the breakdown.

Android batterystats output format (varies by version):

    Estimated power use (mAh):
      Capacity: 4000, Computed drain: 123.4, actual drain: 100-120
      Screen: 45.2
      Uid u0a123 (com.example.app): 12.3 ( cpu=8.5 wake=2.1 wifi=1.7 )
      Uid 1000 (android): 5.6 ( cpu=3.2 wake=0.8 wifi=1.6 )
      UID 2000: 18.30 ( cpu=14.2 ... )   ← uppercase UID, no package name

When Android does not resolve the UID to a package name (parentheses absent),
the module fetches a UID→package map from ``pm list packages -U`` and known
system UID constants, so labels show real package/component names instead of
raw UID numbers.

Public API:
    reset_battery_stats(device_id) -> bool
    get_battery_attribution(device_id) -> Dict[str, float]
    is_hw_component(label) -> bool
"""

import logging
import re
from typing import Dict, Optional

from droidperf.adb_manager import run_adb_command

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Compiled regexes  (re.IGNORECASE so "UID" and "Uid" both match)
# ---------------------------------------------------------------------------

# "  Uid u0a123 (com.example.app): 12.34 ( cpu=... )"
_UID_WITH_PKG_RE = re.compile(
    r"^\s+UID\s+\S+\s+\(([^)]+)\):\s+([\d.]+)",
    re.IGNORECASE,
)
# "  Uid 1000: 5.67"  or  "  UID u0a102: 0.41"  (no package name resolved)
_UID_NO_PKG_RE = re.compile(
    r"^\s+UID\s+(\S+):\s+([\d.]+)",
    re.IGNORECASE,
)
# "  Screen: 45.2"  /  "  Cell standby: 3.1"  (hardware component lines)
_COMPONENT_RE = re.compile(
    r"^\s+([A-Za-z][A-Za-z0-9 ]+?):\s+([\d.]+)\s*(?:\(|$)",
)
# "  package:com.example.app uid:10102"  from pm list packages -U
_PM_PACKAGE_UID_RE = re.compile(
    r"^package:(\S+)\s+uid:(\d+)",
    re.MULTILINE,
)

# ---------------------------------------------------------------------------
# Known Android system UIDs (never resolvable via pm list packages)
# ---------------------------------------------------------------------------

_SYSTEM_UID_NAMES: Dict[int, str] = {
    0:    "root",
    1000: "android (system server)",
    1001: "phone / telephony",
    1002: "bluetooth",
    1003: "nfc",
    2000: "shell",
    2001: "cache",
    9999: "nobody",
}

# Labels that represent hardware components, not apps.
_HW_COMPONENTS = frozenset([
    "screen", "cell", "cell standby", "wifi", "bluetooth",
    "idle", "radio", "sensors", "flashlight", "camera",
    "audio", "video", "phone", "modem",
])


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _uid_str_to_int(uid_str: str) -> Optional[int]:
    """
    Convert an Android UID string to its integer value.

    Handles both plain decimal UIDs (``"1000"``) and the compact
    ``u{user}a{app_index}`` format used by batterystats (``"u0a102"``
    maps to user-space UID ``10102``).

    Args:
        uid_str (str): Raw UID token from batterystats output.

    Returns:
        Optional[int]: Integer UID, or ``None`` when parsing fails.
    """
    uid_str = uid_str.strip()
    m = re.match(r"^u(\d+)a(\d+)$", uid_str, re.IGNORECASE)
    if m:
        user = int(m.group(1))
        app_index = int(m.group(2))
        # Standard formula: UID = user * 100000 + 10000 + app_index
        return user * 100_000 + 10_000 + app_index
    try:
        return int(uid_str)
    except ValueError:
        return None


def _get_uid_package_map(device_id: str) -> Dict[int, str]:
    """
    Build a UID → package name mapping by querying the device.

    Combines hard-coded system UID constants with results from
    ``adb shell pm list packages -U``.  Unknown UIDs keep their
    numeric representation.

    Args:
        device_id (str): ADB serial number of the target device.

    Returns:
        Dict[int, str]: Mapping of integer UID → human-readable label.
    """
    mapping: Dict[int, str] = dict(_SYSTEM_UID_NAMES)

    output = run_adb_command(device_id, "pm list packages -U")
    if not output:
        logger.warning(
            "device='%s': 'pm list packages -U' returned no output; "
            "UID names will fall back to system constants only.",
            device_id,
        )
        return mapping

    for m in _PM_PACKAGE_UID_RE.finditer(output):
        pkg_name = m.group(1)
        uid = int(m.group(2))
        mapping[uid] = pkg_name

    logger.debug(
        "device='%s': UID→package map built — %d entries.",
        device_id,
        len(mapping),
    )
    return mapping


def _resolve_uid_label(uid_str: str, uid_map: Dict[int, str]) -> str:
    """
    Resolve a raw UID token (e.g. ``"u0a102"`` or ``"1000"``) to a
    human-readable package or system component name.

    Falls back to ``"uid:<raw>"`` when the UID cannot be found in the map.

    Args:
        uid_str (str):             Raw UID token from batterystats.
        uid_map (Dict[int, str]):  UID → name mapping from the device.

    Returns:
        str: Resolved package name or fallback string.
    """
    uid_int = _uid_str_to_int(uid_str)
    if uid_int is not None and uid_int in uid_map:
        return uid_map[uid_int]
    # Fallback: keep it readable but clearly marked as unresolved.
    return f"uid:{uid_str}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def reset_battery_stats(device_id: str) -> bool:
    """
    Reset Android battery statistics on the target device.

    Should be called at session start so that ``get_battery_attribution``
    reflects only the current monitoring window and not historical usage.

    Args:
        device_id (str): ADB serial number of the target device.

    Returns:
        bool: ``True`` when the reset command succeeded, ``False`` otherwise.
    """
    result = run_adb_command(device_id, "dumpsys batterystats --reset")
    if result is None:
        logger.warning(
            "device='%s': failed to reset batterystats (root may be required).",
            device_id,
        )
        return False
    logger.info("device='%s': batterystats reset successfully.", device_id)
    return True


def get_battery_attribution(device_id: str) -> Dict[str, float]:
    """
    Return per-package and per-component battery consumption in mAh.

    Parses the ``Estimated power use`` section of ``dumpsys batterystats``.
    UID entries that lack package names in the batterystats output are
    resolved via ``pm list packages -U`` and well-known system UID constants
    (e.g. UID 1000 → ``"android (system server)"``).

    Only entries with a positive mAh value are included.  Results are sorted
    descending by consumption.

    Args:
        device_id (str): ADB serial number of the target device.

    Returns:
        Dict[str, float]: Mapping of label → mAh consumed.
            Empty dict when the command fails or the section is absent.
    """
    output = run_adb_command(device_id, "dumpsys batterystats")
    if not output:
        logger.error(
            "device='%s': no output from 'dumpsys batterystats'.", device_id
        )
        return {}

    # Fetch UID→package map once (used to resolve unresolved UID entries).
    uid_map = _get_uid_package_map(device_id)

    raw_attribution: Dict[str, float] = {}
    in_section = False

    for line in output.splitlines():
        # Detect section start.
        if "Estimated power use" in line:
            in_section = True
            continue

        if not in_section:
            continue

        # Section ends at the next non-indented, non-empty line.
        stripped = line.strip()
        if stripped and not line[0].isspace():
            break

        # Skip the "Capacity: ..." metadata line.
        if stripped.lower().startswith("capacity:"):
            continue

        # Try "Uid ... (package.name): mah" — package already resolved.
        m = _UID_WITH_PKG_RE.match(line)
        if m:
            label = m.group(1).strip()
            mah = float(m.group(2))
            if mah > 0:
                raw_attribution[label] = raw_attribution.get(label, 0.0) + mah
            continue

        # Try "Uid XXXX: mah" — UID only, resolve via uid_map.
        m = _UID_NO_PKG_RE.match(line)
        if m:
            label = _resolve_uid_label(m.group(1).strip(), uid_map)
            mah = float(m.group(2))
            if mah > 0:
                raw_attribution[label] = raw_attribution.get(label, 0.0) + mah
            continue

        # Try hardware component lines ("Screen: 45.2" etc.).
        m = _COMPONENT_RE.match(line)
        if m:
            label = m.group(1).strip()
            mah = float(m.group(2))
            if mah > 0:
                raw_attribution[label] = raw_attribution.get(label, 0.0) + mah

    if not raw_attribution:
        logger.warning(
            "device='%s': 'Estimated power use' section empty or not found. "
            "Some Android builds require root for batterystats.",
            device_id,
        )
        return {}

    # Sort descending by mAh so the highest consumers appear first.
    sorted_attr = dict(
        sorted(raw_attribution.items(), key=lambda kv: kv[1], reverse=True)
    )
    logger.debug(
        "device='%s': battery attribution — %d entries resolved, top: %s",
        device_id,
        len(sorted_attr),
        list(sorted_attr.items())[:3],
    )
    return sorted_attr


def is_hw_component(label: str) -> bool:
    """
    Return ``True`` when *label* refers to a hardware component rather than
    an app package (e.g. ``"Screen"``, ``"Cell standby"``).

    Args:
        label (str): Attribution label from ``get_battery_attribution``.

    Returns:
        bool: ``True`` for hardware/system components.
    """
    return label.lower() in _HW_COMPONENTS
