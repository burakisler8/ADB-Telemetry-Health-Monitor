"""
droidperf/process_discovery.py
-------------------------------
Discovers running Android application packages on a connected device.

Uses ``adb shell ps -A`` to list all running processes and filters the
output to return only user-installed app packages identified by their
reverse-domain naming convention (e.g. ``com.example.myapp``).

System prefixes (``com.android``, ``com.google.android``, ``android.``,
``system``) are excluded so the result contains only third-party or
OEM application packages.

Public API:
    get_running_packages(device_id) -> List[str]
"""

import logging
import re
from typing import List

from droidperf.adb_manager import run_adb_command

logger = logging.getLogger(__name__)

# Matches a valid reverse-domain package name such as com.example.myapp.
# Rules:
#   - Starts with a lowercase letter.
#   - Each component contains only [a-z0-9_].
#   - Must have at least THREE components (vendor + namespace + name).
#     This filters out two-part native services like media.codec,
#     media.extractor, media.metrics which are not user-space apps.
_PACKAGE_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){2,}$")

# Process name prefixes that identify Android system packages to be excluded.
_SYSTEM_PREFIXES = (
    "com.android.",
    "com.google.android.",
    "android.",
    "system",
    "media.",
    "webview.",
)


def _is_user_package(name: str) -> bool:
    """
    Return ``True`` when *name* looks like a user-installed app package.

    A name qualifies when it:
      1. Matches the reverse-domain regex (at least one dot, lowercase
         alphanumeric components).
      2. Does not start with any of the known Android system prefixes.

    Args:
        name (str): Candidate package name extracted from a ``ps`` output line.

    Returns:
        bool: ``True`` if the name is a user-space application package.
    """
    if not _PACKAGE_RE.match(name):
        return False
    for prefix in _SYSTEM_PREFIXES:
        if name.startswith(prefix):
            return False
    return True


def get_running_packages(device_id: str) -> List[str]:
    """
    Return a sorted, deduplicated list of running user-app package names.

    Executes ``adb shell ps -A`` on the target device, extracts the last
    column (process name) from each output line, and filters for names
    that follow the reverse-domain convention while excluding system
    prefixes.

    Args:
        device_id (str): Serial number of the target Android device as
                         returned by ``adb_manager.get_connected_devices()``.

    Returns:
        List[str]: Sorted list of discovered package names. Returns an
                   empty list if the command fails or no packages are found.

    Example:
        >>> packages = get_running_packages("emulator-5554")
        >>> print(packages)
        ['com.example.app', 'org.mozilla.firefox']
    """
    output = run_adb_command(device_id, "ps -A")
    if output is None:
        logger.error(
            "No output from `ps -A` on device '%s'. "
            "Cannot discover running packages.",
            device_id,
        )
        return []

    packages: set = set()
    for line in output.splitlines():
        parts = line.split()
        if not parts:
            continue
        # The process name is the last whitespace-separated token on each line.
        candidate = parts[-1].strip()
        if _is_user_package(candidate):
            packages.add(candidate)

    result = sorted(packages)
    logger.info(
        "device='%s': discovered %d running user package(s).",
        device_id,
        len(result),
    )
    logger.debug("Discovered packages: %s", result)
    return result
