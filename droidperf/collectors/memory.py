"""
droidperf/collectors/memory.py
------------------------------
Collects RAM usage metrics from a connected Android device.

Uses `adb shell dumpsys meminfo <package_name>` and parses the "TOTAL" row,
which reports the Proportional Set Size (PSS) in kilobytes — the most
meaningful single-value RAM indicator for a running process.

Public API:
    get_total_pss(device_id, package_name) -> Optional[int]
"""

import logging
import re
from typing import Optional

from droidperf.adb_manager import run_adb_command

logger = logging.getLogger(__name__)

# Matches the TOTAL summary line produced by `dumpsys meminfo`.
# Example line:
#   "                   TOTAL       45312      18240      ..."
# Capture group 1 → total PSS in KB (first integer after "TOTAL").
_TOTAL_PSS_PATTERN = re.compile(r"^\s*TOTAL\s+(\d+)", re.MULTILINE)


def get_total_pss(device_id: str, package_name: str) -> Optional[int]:
    """
    Return the total PSS RAM usage for a package on a given device.

    Executes ``adb shell dumpsys meminfo <package_name>`` and extracts the
    first integer on the ``TOTAL`` line, which represents the process's
    total Proportional Set Size in kilobytes.

    Args:
        device_id (str):    Serial number of the target Android device.
        package_name (str): Application package name, e.g.
                            ``"com.example.myapp"``.

    Returns:
        Optional[int]: Total PSS in KB, or ``None`` if the command failed
                       or the output could not be parsed.

    Example:
        >>> pss = get_total_pss("emulator-5554", "com.example.myapp")
        >>> print(f"RAM usage: {pss} KB")
    """
    output = run_adb_command(device_id, f"dumpsys meminfo {package_name}")

    if output is None:
        logger.error(
            "Received no output from meminfo for package '%s' on device '%s'.",
            package_name,
            device_id,
        )
        return None

    if not output.strip():
        logger.warning(
            "Empty meminfo output for package '%s' on device '%s'. "
            "The package may not be running.",
            package_name,
            device_id,
        )
        return None

    match = _TOTAL_PSS_PATTERN.search(output)
    if not match:
        logger.warning(
            "Could not locate the TOTAL line in meminfo output "
            "for package '%s' on device '%s'.",
            package_name,
            device_id,
        )
        return None

    total_pss_kb = int(match.group(1))
    logger.debug(
        "device='%s' package='%s' total_pss=%d KB",
        device_id,
        package_name,
        total_pss_kb,
    )
    return total_pss_kb
