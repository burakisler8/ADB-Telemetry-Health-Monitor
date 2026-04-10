"""
droidperf/logcat_watcher.py
---------------------------
Background thread that streams `adb logcat` output and captures lines
containing crash- or ANR-related keywords.

When a crash or ANR is detected and a ``screenshot_dir`` is provided,
the watcher takes an automatic device screenshot (``adb screencap``) in
a short-lived daemon thread so the logcat reader is never blocked.

Runs as a daemon thread so it is automatically killed when the main
process exits. Call ``stop()`` for a clean, graceful shutdown.

Public API:
    LogcatWatcher(device_id, screenshot_dir=None)  — threading.Thread subclass
"""

import logging
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Keywords that indicate a noteworthy event in logcat
_KEYWORDS = ("Exception", "ANR", "FATAL", "Fatal", "crash", "Crash", "force close")

# Crash-specific keywords that also trigger a screenshot (subset of _KEYWORDS)
_CRASH_KEYWORDS = ("ANR", "FATAL", "Fatal", "crash", "Crash", "force close")


class LogcatWatcher(threading.Thread):
    """
    Daemon thread that streams ``adb logcat`` and stores matching events.

    Optionally captures device screenshots when crash/ANR keywords are
    detected.  Screenshots are saved as
    ``<screenshot_dir>/crash_<timestamp>.png``.

    Attributes:
        events (List[Dict]): Accumulated list of captured log events.
            Each entry has keys ``timestamp`` (ISO-8601 str), ``line`` (str),
            and optionally ``screenshot`` (str — path to the PNG file).
    """

    def __init__(
        self,
        device_id: str,
        screenshot_dir: Optional[Path] = None,
    ) -> None:
        """
        Initialise the watcher for a specific device.

        Args:
            device_id (str):            Serial number of the Android device.
            screenshot_dir (Path|None): Directory to save crash screenshots.
                                        ``None`` disables screenshot capture.
        """
        super().__init__(daemon=True, name="LogcatWatcher")
        self.device_id = device_id
        self._screenshot_dir = Path(screenshot_dir) if screenshot_dir else None
        self.events: List[Dict[str, str]] = []
        self._stop_event = threading.Event()
        self._process: Optional[subprocess.Popen] = None

    # ------------------------------------------------------------------
    # Thread lifecycle
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Stream logcat output and capture lines matching keywords."""
        cmd = ["adb", "-s", self.device_id, "logcat", "-v", "time"]
        logger.info("LogcatWatcher started for device '%s'.", self.device_id)

        try:
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            for line in self._process.stdout:
                if self._stop_event.is_set():
                    break
                if any(kw in line for kw in _KEYWORDS):
                    self._record_event(line.rstrip())

        except FileNotFoundError:
            logger.error(
                "ADB not found. LogcatWatcher cannot start for device '%s'.",
                self.device_id,
            )
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("LogcatWatcher encountered an unexpected error: %s", exc)
        finally:
            self._terminate_process()
            logger.info(
                "LogcatWatcher stopped. %d event(s) captured.", len(self.events)
            )

    def stop(self) -> None:
        """Signal the watcher to stop and terminate the logcat subprocess."""
        self._stop_event.set()
        self._terminate_process()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _record_event(self, line: str) -> None:
        """
        Store a matched logcat line with an ISO-8601 timestamp.

        If the line matches a crash/ANR keyword and ``screenshot_dir`` was
        provided, a screenshot is captured asynchronously.

        Args:
            line (str): The logcat output line that matched a keyword.
        """
        ts = datetime.now().isoformat(timespec="seconds")
        event: Dict[str, str] = {
            "timestamp": ts,
            "line": line,
        }
        self.events.append(event)
        logger.warning("Logcat event: %s", line)

        # Capture screenshot for crash/ANR events (non-blocking).
        if self._screenshot_dir and any(kw in line for kw in _CRASH_KEYWORDS):
            threading.Thread(
                target=self._capture_screenshot,
                args=(ts, event),
                daemon=True,
                name="ScreenshotCapture",
            ).start()

    def _capture_screenshot(self, timestamp: str, event: dict) -> None:
        """
        Capture a screenshot from the device and save it locally.

        Uses ``adb screencap`` to take the screenshot on-device, then
        ``adb pull`` to download it.  Writes the local path back to the
        *event* dict under the ``"screenshot"`` key.

        Args:
            timestamp (str): ISO-8601 timestamp string for the filename.
            event (dict):    The event dict to update with the screenshot path.
        """
        try:
            self._screenshot_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("Cannot create screenshot dir: %s", exc)
            return

        safe_ts = timestamp.replace(":", "-")
        device_path = "/sdcard/adb_monitor_crash.png"
        local_path = self._screenshot_dir / f"crash_{safe_ts}.png"

        # Step 1: capture on device.
        try:
            result = subprocess.run(
                ["adb", "-s", self.device_id, "shell", "screencap", "-p", device_path],
                timeout=10,
                capture_output=True,
            )
            if result.returncode != 0:
                logger.warning("screencap failed (rc=%d).", result.returncode)
                return
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("screencap command error: %s", exc)
            return

        # Step 2: pull to host.
        try:
            result = subprocess.run(
                ["adb", "-s", self.device_id, "pull", device_path, str(local_path)],
                timeout=15,
                capture_output=True,
            )
            if result.returncode == 0:
                event["screenshot"] = str(local_path)
                logger.info("Crash screenshot saved: '%s'.", local_path.name)
            else:
                logger.warning("adb pull failed (rc=%d).", result.returncode)
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("adb pull error: %s", exc)

    def _terminate_process(self) -> None:
        """Terminate the logcat subprocess if it is still running."""
        if self._process and self._process.poll() is None:
            try:
                self._process.terminate()
                self._process.wait(timeout=5)
            except Exception as exc:  # pylint: disable=broad-except
                logger.warning(
                    "Could not cleanly terminate logcat process: %s", exc
                )
