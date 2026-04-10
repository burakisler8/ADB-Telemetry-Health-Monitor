"""
droidperf/collectors/disk_io.py
---------------------------------
Collect per-process disk I/O statistics from an Android device via
``/proc/<pid>/io`` (Linux kernel proc filesystem).

The ``/proc/<pid>/io`` file exposes cumulative byte and operation counts
since process start.  MonitorEngine stores the previous snapshot and
computes per-cycle deltas for read_bytes_delta / write_bytes_delta.

Public API
----------
    get_disk_io(device_id, package) -> dict
"""

import logging
import re
from typing import Optional

from droidperf.adb_manager import run_adb_command
from droidperf.collectors.process_stats import _resolve_pid

logger = logging.getLogger(__name__)

# /proc/<pid>/io field patterns
_READ_BYTES_RE  = re.compile(r"^read_bytes:\s*(\d+)",  re.MULTILINE)
_WRITE_BYTES_RE = re.compile(r"^write_bytes:\s*(\d+)", re.MULTILINE)
_RCHAR_RE       = re.compile(r"^rchar:\s*(\d+)",       re.MULTILINE)
_WCHAR_RE       = re.compile(r"^wchar:\s*(\d+)",       re.MULTILINE)


def _read_proc_io(device_id: str, pid: int) -> Optional[dict]:
    """
    Read ``/proc/<pid>/io`` and return raw counters.

    Args:
        device_id (str): ADB device serial.
        pid (int):       Target process ID.

    Returns:
        Optional[dict]: Raw counter dict, or ``None`` on failure.
    """
    output = run_adb_command(device_id, f"cat /proc/{pid}/io")
    if not output:
        return None
    if "Permission denied" in output or "No such file" in output:
        logger.debug("/proc/%d/io not accessible on %s.", pid, device_id)
        return None

    def _extract(pattern: re.Pattern) -> Optional[int]:
        match = pattern.search(output)
        if match:
            try:
                return int(match.group(1))
            except ValueError:
                pass
        return None

    return {
        "read_bytes":  _extract(_READ_BYTES_RE),
        "write_bytes": _extract(_WRITE_BYTES_RE),
        "rchar":       _extract(_RCHAR_RE),
        "wchar":       _extract(_WCHAR_RE),
    }


def get_disk_io(device_id: str, package: str) -> dict:
    """
    Return cumulative disk I/O byte counters for *package* on *device_id*.

    ``read_bytes`` and ``write_bytes`` count actual storage I/O (page-cache
    misses).  ``rchar`` / ``wchar`` include all read()/write() syscall bytes
    (cached reads included) and are higher but always available.

    Args:
        device_id (str): ADB device serial number.
        package (str):   Android package name.

    Returns:
        dict with keys:
            - ``read_bytes``  (int | None): Cumulative bytes read from storage.
            - ``write_bytes`` (int | None): Cumulative bytes written to storage.
            - ``rchar``       (int | None): Cumulative read() syscall bytes.
            - ``wchar``       (int | None): Cumulative write() syscall bytes.
    """
    empty = {"read_bytes": None, "write_bytes": None, "rchar": None, "wchar": None}

    try:
        pid = _resolve_pid(device_id, package)
        if pid is None:
            logger.debug("PID not found for '%s' on '%s' — skipping disk I/O.", package, device_id)
            return empty

        result = _read_proc_io(device_id, pid)
        if result is None:
            return empty

        return result

    except Exception as exc:  # pylint: disable=broad-except
        logger.error(
            "Unexpected error collecting disk I/O for '%s' on '%s': %s",
            package, device_id, exc,
        )
        return empty
