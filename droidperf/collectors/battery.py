"""
droidperf/collectors/battery.py
--------------------------------
Collects battery metrics from a connected Android device.

Uses `adb shell dumpsys battery` and parses level, temperature,
voltage, and charging status into a typed dictionary.

Public API:
    get_battery_info(device_id) -> Optional[Dict]
"""

import logging
import re
from typing import Dict, Optional

from droidperf.adb_manager import run_adb_command

logger = logging.getLogger(__name__)

# Android BatteryManager status constants
_STATUS_MAP: Dict[str, str] = {
    "1": "Unknown",
    "2": "Charging",
    "3": "Discharging",
    "4": "Not Charging",
    "5": "Full",
}


def _extract_field(output: str, field_name: str) -> Optional[str]:
    """
    Extract the value of a named field from `dumpsys battery` output.

    Args:
        output (str):     Full stdout of the `dumpsys battery` command.
        field_name (str): Exact field label as it appears in the output
                          (e.g. "level", "temperature").

    Returns:
        Optional[str]: Stripped field value, or ``None`` if not found.
    """
    pattern = re.compile(
        rf"^\s+{re.escape(field_name)}:\s+(.+)$", re.MULTILINE
    )
    match = pattern.search(output)
    return match.group(1).strip() if match else None


def get_battery_info(device_id: str) -> Optional[Dict]:
    """
    Return battery metrics for the connected device.

    Runs `adb shell dumpsys battery` and parses:
      - Battery level (%)
      - Temperature in °C (converted from tenths-of-degree raw value)
      - Voltage in millivolts
      - Human-readable charging status

    Args:
        device_id (str): Serial number of the target Android device.

    Returns:
        Optional[Dict]: Dictionary with keys:
            - ``level``       (int):   Battery percentage (0–100).
            - ``temperature`` (float): Temperature in °C.
            - ``voltage_mv``  (int):   Voltage in millivolts.
            - ``status``      (str):   Human-readable charging status.
        Returns ``None`` if the command fails or output cannot be parsed.
    """
    output = run_adb_command(device_id, "dumpsys battery")
    if output is None:
        logger.error("No output from `dumpsys battery` on device '%s'.", device_id)
        return None

    try:
        level = int(_extract_field(output, "level") or "")
        raw_temp = int(_extract_field(output, "temperature") or "")
        voltage = int(_extract_field(output, "voltage") or "")
        status_code = _extract_field(output, "status") or "1"
    except (ValueError, TypeError) as exc:
        logger.error(
            "Failed to parse battery fields on device '%s': %s", device_id, exc
        )
        return None

    info: Dict = {
        "level": level,
        "temperature": raw_temp / 10.0,  # tenths of °C → °C
        "voltage_mv": voltage,
        "status": _STATUS_MAP.get(status_code, f"Unknown({status_code})"),
    }
    logger.debug(
        "device='%s' battery: level=%d%% temp=%.1f°C voltage=%dmV status=%s",
        device_id,
        info["level"],
        info["temperature"],
        info["voltage_mv"],
        info["status"],
    )
    return info
