"""
droidperf/collectors/cpu.py
---------------------------
Collects CPU usage metrics from a connected Android device.

Per-process CPU strategy (two-step):
  1. ``adb shell top -n 1``       — system-wide CPU summary (user%, sys%).
  2. ``adb shell dumpsys cpuinfo`` — per-process delta-based CPU (primary).
     ``top -n 1`` only shows the "top-N" processes and omits low-CPU apps,
     so ``dumpsys cpuinfo`` is used as the authoritative per-process source.
     The caller may pass a pre-fetched ``cpuinfo_output`` string so that the
     MonitorEngine can share a single ``dumpsys cpuinfo`` call across all
     per-package iterations, avoiding redundant ADB round-trips.

Load averages are always read from ``/proc/loadavg``.

Public API:
    get_cpu_usage(device_id, package_name, cpuinfo_output=None) -> Optional[Dict]
"""

import logging
import re
from typing import Dict, Optional, Tuple

from droidperf.adb_manager import run_adb_command

logger = logging.getLogger(__name__)

# Modern Android top summary: "400%cpu  12%user  0%nice  45%sys 343%idle ..."
_MODERN_CPU_RE = re.compile(
    r"\d+%cpu\s+(\d+(?:\.\d+)?)%user.*?(\d+(?:\.\d+)?)%sys", re.IGNORECASE
)
# Legacy Android top summary: "User 12%, System 8%, IOW 0%, IRQ 0%"
_LEGACY_CPU_RE = re.compile(
    r"User\s+(\d+(?:\.\d+)?)%.*?System\s+(\d+(?:\.\d+)?)%", re.IGNORECASE
)
# dumpsys cpuinfo per-process line: "  5% 1234/com.example.app: 4% user + 1% kernel"
_CPUINFO_PROCESS_RE = re.compile(
    r"(\d+(?:\.\d+)?)%\s+\d+/([^\s:]+):\s+"
    r"(\d+(?:\.\d+)?)%\s+user\s+\+\s+(\d+(?:\.\d+)?)%\s+kernel",
)
# /proc/loadavg: "0.52 0.58 0.59 1/453 12345"
_LOADAVG_RE = re.compile(r"^([\d.]+)\s+([\d.]+)\s+([\d.]+)")


# ---------------------------------------------------------------------------
# Internal helpers — top parsers
# ---------------------------------------------------------------------------

def _top_is_usable(output: Optional[str]) -> bool:
    """
    Return ``True`` when ``top`` output looks valid and parseable.

    Some device firmware builds ship a broken ``top`` binary that crashes
    immediately (SIGSEGV, exit code 139), returning empty stdout.

    Args:
        output: Raw stdout from the ``top -n 1`` command.

    Returns:
        bool: ``True`` if the output contains a recognisable process table.
    """
    if not output or not output.strip():
        return False
    upper = output.upper()
    return "PID" in upper or "%CPU" in upper or "USER" in upper


def _parse_overall_cpu(output: str) -> Tuple[Optional[float], Optional[float]]:
    """
    Extract overall user% and sys% from ``top`` or ``dumpsys cpuinfo`` output.

    Tries the modern format first, then falls back to legacy format.

    Args:
        output (str): Full stdout of the command.

    Returns:
        Tuple of (user_pct, sys_pct); either value may be ``None``.
    """
    for pattern in (_MODERN_CPU_RE, _LEGACY_CPU_RE):
        match = pattern.search(output)
        if match:
            return float(match.group(1)), float(match.group(2))
    return None, None


def _parse_top_process_cpu(output: str, package_name: str) -> Optional[float]:
    """
    Find the process entry for ``package_name`` in ``top`` output and
    return its instantaneous CPU percentage.

    Locates the header line to determine the ``%CPU`` column index, then
    scans all subsequent lines for the package name.

    Args:
        output (str):       Full stdout of ``top -n 1``.
        package_name (str): Package name to search for.

    Returns:
        Optional[float]: CPU % for the process, or ``None`` if not found.
    """
    lines = output.splitlines()
    cpu_col: Optional[int] = None

    for line in lines:
        if "PID" in line.upper() and "CPU" in line.upper():
            for i, token in enumerate(line.split()):
                if "CPU" in token.upper():
                    cpu_col = i
                    break
            break

    if cpu_col is None:
        logger.debug("Could not locate %%CPU column in `top` header.")
        return None

    for line in lines:
        if package_name in line:
            parts = line.split()
            if len(parts) > cpu_col:
                try:
                    return float(parts[cpu_col].rstrip("%"))
                except ValueError:
                    continue
    return None


# ---------------------------------------------------------------------------
# Internal helpers — dumpsys cpuinfo parsers
# ---------------------------------------------------------------------------

def _parse_cpuinfo_process_cpu(output: str, package_name: str) -> Optional[float]:
    """
    Extract per-process CPU% from ``dumpsys cpuinfo`` output.

    Matches lines like:
        ``  5% 1234/com.example.app: 4% user + 1% kernel``

    ``dumpsys cpuinfo`` reports a delta-based percentage over the last
    sampling window for ALL running processes, making it more reliable
    than ``top -n 1`` which truncates its process list.

    Args:
        output (str):       Full stdout of ``dumpsys cpuinfo``.
        package_name (str): Package name to search for.

    Returns:
        Optional[float]: Total CPU % for the process, or ``None`` if absent.
    """
    for match in _CPUINFO_PROCESS_RE.finditer(output):
        if package_name in match.group(2):
            return float(match.group(1))
    return None


# ---------------------------------------------------------------------------
# Internal helpers — load averages
# ---------------------------------------------------------------------------

def _get_load_averages(
    device_id: str,
) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """
    Read ``/proc/loadavg`` and return the 1-min, 5-min, 15-min load averages.

    Args:
        device_id (str): Serial number of the target Android device.

    Returns:
        Tuple of (load_1m, load_5m, load_15m); all ``None`` on failure.
    """
    output = run_adb_command(device_id, "cat /proc/loadavg")
    if output:
        match = _LOADAVG_RE.match(output.strip())
        if match:
            return float(match.group(1)), float(match.group(2)), float(match.group(3))
    logger.warning("Could not read /proc/loadavg on device '%s'.", device_id)
    return None, None, None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_cpu_usage(
    device_id: str,
    package_name: str,
    cpuinfo_output: Optional[str] = None,
) -> Optional[Dict]:
    """
    Return a real-time CPU snapshot for a package and overall system load.

    **Per-process CPU strategy (two-step):**

    1. ``top -n 1`` is run for the system-wide summary (user%, sys%) and
       as a fast-path attempt to find the process.
    2. If the process is not found in ``top`` (common when the app has low
       CPU and is excluded from the truncated list), ``dumpsys cpuinfo`` is
       consulted — it reports delta-based CPU% for *all* running processes.
       The caller may supply pre-fetched ``cpuinfo_output`` to avoid an
       extra ADB round-trip when monitoring many packages in the same cycle.

    Args:
        device_id (str):    Serial number of the target Android device.
        package_name (str): Application package name, e.g. "com.example.app".
        cpuinfo_output (str | None):
            Pre-fetched ``dumpsys cpuinfo`` stdout.  When ``None`` and the
            per-process CPU cannot be found in ``top``, a fresh
            ``dumpsys cpuinfo`` is fetched automatically.

    Returns:
        Optional[Dict]: Dictionary with keys:
            - ``total_pct``  (float | None): Per-process CPU %.
            - ``user_pct``   (float | None): Overall system user-space CPU %.
            - ``kernel_pct`` (float | None): Overall system kernel CPU %.
            - ``load_1m``    (float | None): 1-minute load average.
            - ``load_5m``    (float | None): 5-minute load average.
            - ``load_15m``   (float | None): 15-minute load average.
        Returns ``None`` if both ``top`` and ``dumpsys cpuinfo`` fail.
    """
    # ── Step 1: top -n 1 (system-wide summary + fast-path process lookup) ──
    top_output = run_adb_command(device_id, "top -n 1")
    top_usable = _top_is_usable(top_output)

    if not top_usable:
        logger.warning(
            "device='%s': `top -n 1` returned unusable output "
            "(possibly SIGSEGV/exit 139). Falling back to `dumpsys cpuinfo`.",
            device_id,
        )

    # System-wide user% / kernel% from the top summary line.
    user_pct: Optional[float] = None
    sys_pct: Optional[float] = None
    if top_usable and top_output:
        user_pct, sys_pct = _parse_overall_cpu(top_output)

    # Fast-path per-process lookup from top (works when the process is
    # listed in the truncated output).
    process_cpu: Optional[float] = None
    if top_usable and top_output:
        process_cpu = _parse_top_process_cpu(top_output, package_name)

    # ── Step 2: dumpsys cpuinfo (reliable per-process, all processes) ──────
    # Used whenever top didn't find the process, OR when top was unusable.
    if process_cpu is None:
        if cpuinfo_output is None:
            # Fetch on-demand if not pre-supplied by the caller.
            cpuinfo_output = run_adb_command(device_id, "dumpsys cpuinfo")

        if cpuinfo_output:
            process_cpu = _parse_cpuinfo_process_cpu(cpuinfo_output, package_name)
            # Also derive system-wide CPU from cpuinfo when top was unusable.
            if not top_usable:
                user_pct, sys_pct = _parse_overall_cpu(cpuinfo_output)

    if process_cpu is None:
        logger.warning(
            "Package '%s' not found in CPU output on device '%s'. "
            "The app may not be running.",
            package_name,
            device_id,
        )

    # ── Load averages (always from /proc/loadavg) ───────────────────────────
    load_1m, load_5m, load_15m = _get_load_averages(device_id)

    logger.debug(
        "device='%s' package='%s' cpu=%.1f%% user=%.1f%% sys=%.1f%% load=%.2f",
        device_id,
        package_name,
        process_cpu or 0.0,
        user_pct or 0.0,
        sys_pct or 0.0,
        load_1m or 0.0,
    )
    return {
        "total_pct": process_cpu,
        "user_pct": user_pct,
        "kernel_pct": sys_pct,
        "load_1m": load_1m,
        "load_5m": load_5m,
        "load_15m": load_15m,
    }
