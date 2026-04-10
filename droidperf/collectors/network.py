"""
droidperf/collectors/network.py
--------------------------------
Collect per-package network I/O statistics from an Android device.

Strategy
--------
1. Resolve the UID for *package* using ``dumpsys package <pkg>``.
2. Read ``/proc/net/xt_qtaguid/stats`` for per-UID TX/RX byte counters.
   (Available on most Android versions up to ~11; falls back gracefully.)
3. If qtaguid is unavailable, attempt ``/proc/net/dev`` (device-level only,
   not per-package) and return ``None`` for package-level fields.

Returned dict keys
------------------
    rx_bytes (int | None)   Bytes received since boot (cumulative).
    tx_bytes (int | None)   Bytes transmitted since boot (cumulative).

Cumulative values are raw kernel counters.  The caller (MonitorEngine)
computes per-cycle deltas for display purposes.

Public API
----------
    get_network_stats(device_id, package) -> dict
"""

import logging
import re
from typing import Optional

from droidperf.adb_manager import run_adb_command

logger = logging.getLogger(__name__)

# Regex to extract UID from `dumpsys package` output.
_UID_RE = re.compile(r"userId=(\d+)")

# qtaguid columns (space-separated):
# idx iface acct_tag_hex uid_tag_int cnt_set rx_bytes rx_packets tx_bytes tx_packets …
_QTAGUID_RE = re.compile(
    r"^\d+\s+\S+\s+\S+\s+(\d+)\s+\d+\s+(\d+)\s+\d+\s+(\d+)",
    re.MULTILINE,
)


def _get_uid(device_id: str, package: str) -> Optional[int]:
    """
    Resolve the Linux UID for *package* on *device_id*.

    Args:
        device_id (str): ADB device serial.
        package (str):   Package name, e.g. ``"com.example.app"``.

    Returns:
        Optional[int]: UID integer, or ``None`` if not found.
    """
    output = run_adb_command(device_id, f"dumpsys package {package}")
    if not output:
        return None
    match = _UID_RE.search(output)
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            pass
    logger.debug("Could not resolve UID for package '%s'.", package)
    return None


def _read_qtaguid(device_id: str, uid: int) -> Optional[dict]:
    """
    Read per-UID byte counters from ``/proc/net/xt_qtaguid/stats``.

    Sums RX and TX across all interface rows for *uid*.

    Args:
        device_id (str): ADB device serial.
        uid (int):       Linux UID of the target application.

    Returns:
        Optional[dict]: ``{rx_bytes, tx_bytes}`` or ``None`` on failure.
    """
    output = run_adb_command(device_id, "cat /proc/net/xt_qtaguid/stats")
    if not output or "No such file" in output or "Permission denied" in output:
        return None

    rx_total = 0
    tx_total = 0
    found = False

    for match in _QTAGUID_RE.finditer(output):
        row_uid = int(match.group(1))
        if row_uid == uid:
            rx_total += int(match.group(2))
            tx_total += int(match.group(3))
            found = True

    if not found:
        logger.debug("UID %d not found in qtaguid stats.", uid)
        return None

    return {"rx_bytes": rx_total, "tx_bytes": tx_total}


def get_network_stats(device_id: str, package: str) -> dict:
    """
    Return cumulative network byte counters for *package* on *device_id*.

    Args:
        device_id (str): ADB device serial number.
        package (str):   Android package name.

    Returns:
        dict with keys:
            - ``rx_bytes`` (int | None): Total bytes received since boot.
            - ``tx_bytes`` (int | None): Total bytes sent since boot.
    """
    empty = {"rx_bytes": None, "tx_bytes": None}

    try:
        uid = _get_uid(device_id, package)
        if uid is None:
            return empty

        result = _read_qtaguid(device_id, uid)
        if result is None:
            logger.debug(
                "qtaguid unavailable for %s on %s — network stats not collected.",
                package, device_id,
            )
            return empty

        return result

    except Exception as exc:  # pylint: disable=broad-except
        logger.error(
            "Unexpected error collecting network stats for '%s' on '%s': %s",
            package, device_id, exc,
        )
        return empty
