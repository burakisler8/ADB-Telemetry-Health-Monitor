"""
droidperf/adb_manager.py
------------------------
Thin wrapper around ADB (Android Debug Bridge) subprocess calls.

Responsibilities:
  - Detect connected Android devices via `adb devices`.
  - Execute arbitrary `adb shell` commands on a specific device.
  - Connect / disconnect Wi-Fi ADB devices via `adb connect / disconnect`.

All other modules in this package must go through this module for ADB I/O;
they must never call subprocess directly.
"""

import logging
import subprocess
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# Default timeout (seconds) for short commands like `adb devices`.
_SHORT_TIMEOUT = 10
# Default timeout (seconds) for potentially slower shell commands.
_COMMAND_TIMEOUT = 30


def get_connected_devices() -> List[str]:
    """
    Run `adb devices` and return the serial numbers of online devices.

    Parses every non-header line that ends with the word "device" (ADB's
    marker for an authorised, online device), excluding "offline" or
    "unauthorized" entries.

    Returns:
        List[str]: Serial numbers of connected, authorised devices.
                   Returns an empty list when ADB is unavailable or no
                   devices are found.
    """
    try:
        result = subprocess.run(
            ["adb", "devices"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_SHORT_TIMEOUT,
        )
        lines = result.stdout.strip().splitlines()
        # Line 0 is the fixed header: "List of devices attached"
        devices = [
            line.split("\t")[0]
            for line in lines[1:]
            if line.endswith("\tdevice")
        ]
        if devices:
            logger.info("Found %d connected device(s): %s", len(devices), devices)
        else:
            logger.warning("No authorised devices found. Check USB connection and ADB authorisation.")
        return devices

    except FileNotFoundError:
        logger.error(
            "ADB executable not found. "
            "Ensure ADB is installed and its directory is added to PATH."
        )
        return []
    except subprocess.TimeoutExpired:
        logger.error("Timed out while running `adb devices` (limit: %ds).", _SHORT_TIMEOUT)
        return []
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("Unexpected error running `adb devices`: %s", exc)
        return []


def connect_wifi(host_port: str) -> Tuple[bool, str]:
    """
    Connect to an Android device over Wi-Fi using ``adb connect``.

    Args:
        host_port (str): Target address in ``"host:port"`` form,
                         e.g. ``"192.168.1.42:5555"``.

    Returns:
        Tuple[bool, str]: ``(True, message)`` on success,
                          ``(False, error_message)`` on failure.
    """
    host_port = host_port.strip()
    if not host_port:
        return False, "Address is empty."

    try:
        result = subprocess.run(
            ["adb", "connect", host_port],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_SHORT_TIMEOUT,
        )
        output = (result.stdout + result.stderr).strip()
        # ADB prints "connected to …" or "already connected to …" on success.
        success = "connected to" in output.lower()
        if success:
            logger.info("Wi-Fi ADB connected: %s (%s)", host_port, output)
        else:
            logger.warning("Wi-Fi ADB connect failed for %s: %s", host_port, output)
        return success, output

    except FileNotFoundError:
        msg = "ADB executable not found. Check PATH."
        logger.error(msg)
        return False, msg
    except subprocess.TimeoutExpired:
        msg = f"Connection timed out after {_SHORT_TIMEOUT}s."
        logger.error("connect_wifi timeout for %s.", host_port)
        return False, msg
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("Unexpected error in connect_wifi: %s", exc)
        return False, str(exc)


def disconnect_wifi(host_port: str) -> Tuple[bool, str]:
    """
    Disconnect a Wi-Fi ADB device using ``adb disconnect``.

    Args:
        host_port (str): Target address in ``"host:port"`` form.

    Returns:
        Tuple[bool, str]: ``(True, message)`` on success,
                          ``(False, error_message)`` on failure.
    """
    host_port = host_port.strip()
    if not host_port:
        return False, "Address is empty."

    try:
        result = subprocess.run(
            ["adb", "disconnect", host_port],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_SHORT_TIMEOUT,
        )
        output = (result.stdout + result.stderr).strip()
        success = "disconnected" in output.lower() or result.returncode == 0
        if success:
            logger.info("Wi-Fi ADB disconnected: %s", host_port)
        else:
            logger.warning("disconnect_wifi unexpected output for %s: %s", host_port, output)
        return success, output

    except FileNotFoundError:
        msg = "ADB executable not found. Check PATH."
        logger.error(msg)
        return False, msg
    except subprocess.TimeoutExpired:
        msg = f"Disconnect timed out after {_SHORT_TIMEOUT}s."
        logger.error("disconnect_wifi timeout for %s.", host_port)
        return False, msg
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("Unexpected error in disconnect_wifi: %s", exc)
        return False, str(exc)


def run_adb_command(device_id: str, command: str) -> Optional[str]:
    """
    Execute an `adb shell` command on a specific device and return its output.

    Args:
        device_id (str): Serial number of the target device (from
                         ``get_connected_devices``).
        command (str):   Shell command string to execute, e.g.
                         ``"dumpsys meminfo com.example.app"``.

    Returns:
        Optional[str]: Raw stdout text from the command, or ``None`` if the
                       command could not be executed or timed out.
    """
    full_cmd = ["adb", "-s", device_id, "shell"] + command.split()
    logger.debug("Running: %s", " ".join(full_cmd))

    try:
        result = subprocess.run(
            full_cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_COMMAND_TIMEOUT,
        )
        if result.returncode != 0:
            logger.warning(
                "Command '%s' exited with code %d on device '%s'. Stderr: %s",
                command,
                result.returncode,
                device_id,
                result.stderr.strip(),
            )
        return result.stdout

    except subprocess.TimeoutExpired:
        logger.error(
            "Command '%s' timed out after %ds on device '%s'.",
            command,
            _COMMAND_TIMEOUT,
            device_id,
        )
        return None
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception(
            "Unexpected error running '%s' on device '%s': %s",
            command,
            device_id,
            exc,
        )
        return None
