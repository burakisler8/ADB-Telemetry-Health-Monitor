"""
droidperf/collectors/process_stats.py
--------------------------------------
Collect per-process thread count and open file-descriptor count
from an Android device via the ``/proc/<pid>/`` virtual filesystem.

Strategy
--------
1. Resolve the PID for *package* using ``pidof <package>`` (or fall back
   to parsing ``ps -A``).
2. Read ``/proc/<pid>/status`` for ``Threads:`` field.
3. Count entries in ``/proc/<pid>/fd/`` for the open FD count.

Public API
----------
    get_process_stats(device_id, package) -> dict
"""

import logging
import re
from typing import Optional

from droidperf.adb_manager import run_adb_command

logger = logging.getLogger(__name__)

_THREADS_RE = re.compile(r"^Threads:\s*(\d+)", re.MULTILINE)


def _resolve_pid(device_id: str, package: str) -> Optional[int]:
    """
    Return the main PID for *package* on *device_id*.

    Tries ``pidof`` first (fast); falls back to ``ps -A`` grep.

    Args:
        device_id (str): ADB device serial.
        package (str):   Package name.

    Returns:
        Optional[int]: First PID found, or ``None``.
    """
    # Fast path: pidof (available on Android 6+)
    output = run_adb_command(device_id, f"pidof {package}")
    if output:
        first = output.strip().split()[0]
        try:
            return int(first)
        except (ValueError, IndexError):
            pass

    # Fallback: parse `ps -A`
    ps_output = run_adb_command(device_id, "ps -A")
    if not ps_output:
        return None

    for line in ps_output.splitlines():
        if package in line:
            parts = line.split()
            # Typical ps columns: USER  PID  PPID  VSZ  RSS  WCHAN  ADDR  S  NAME
            if len(parts) >= 2:
                try:
                    return int(parts[1])
                except ValueError:
                    continue
    return None


def _get_thread_count(device_id: str, pid: int) -> Optional[int]:
    """
    Read the thread count from ``/proc/<pid>/status``.

    Args:
        device_id (str): ADB device serial.
        pid (int):       Process ID.

    Returns:
        Optional[int]: Thread count, or ``None`` on failure.
    """
    output = run_adb_command(device_id, f"cat /proc/{pid}/status")
    if not output:
        return None
    match = _THREADS_RE.search(output)
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            pass
    return None


def _get_fd_count(device_id: str, pid: int) -> Optional[int]:
    """
    Count open file descriptors by listing ``/proc/<pid>/fd/``.

    Args:
        device_id (str): ADB device serial.
        pid (int):       Process ID.

    Returns:
        Optional[int]: Open FD count, or ``None`` on failure.
    """
    output = run_adb_command(device_id, f"ls /proc/{pid}/fd")
    if output is None:
        return None
    if "Permission denied" in output or "No such file" in output:
        return None
    # Each line is one FD entry.
    lines = [ln for ln in output.splitlines() if ln.strip()]
    return len(lines) if lines else None


def get_process_stats(device_id: str, package: str) -> dict:
    """
    Return thread count and open FD count for *package* on *device_id*.

    Args:
        device_id (str): ADB device serial number.
        package (str):   Android package name.

    Returns:
        dict with keys:
            - ``thread_count`` (int | None): Number of threads in the process.
            - ``fd_count``     (int | None): Number of open file descriptors.
    """
    empty = {"thread_count": None, "fd_count": None}

    try:
        pid = _resolve_pid(device_id, package)
        if pid is None:
            logger.debug("PID not found for package '%s' on '%s'.", package, device_id)
            return empty

        thread_count = _get_thread_count(device_id, pid)
        fd_count = _get_fd_count(device_id, pid)

        return {"thread_count": thread_count, "fd_count": fd_count}

    except Exception as exc:  # pylint: disable=broad-except
        logger.error(
            "Unexpected error collecting process stats for '%s' on '%s': %s",
            package, device_id, exc,
        )
        return empty
