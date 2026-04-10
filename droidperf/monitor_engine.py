"""
droidperf/monitor_engine.py
----------------------------
Orchestrates multi-package telemetry collection in a background thread.

``MonitorEngine`` gathers RAM, CPU, battery, network I/O, disk I/O, and
process-level (thread/FD) metrics for one or more Android packages on a
connected device.  When no package list is provided it auto-discovers
running user packages each cycle via ``process_discovery.get_running_packages()``.

Records are flushed to CSV after every cycle and delivered to the caller
through a user-supplied ``on_snapshot`` callback (designed to be used with
a ``queue.Queue`` so the GUI thread stays decoupled from the worker).

On ``stop()`` an HTML report is generated from all accumulated records via
``reporter.generate_html_report()``.

Public API:
    MonitorEngine(device_id, packages, interval, output_dir,
                  on_snapshot, on_error)
    MonitorEngine.start()
    MonitorEngine.stop()
    MonitorEngine.is_running -> bool
    MonitorEngine.auto_mode  -> bool
"""

import csv
import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

from droidperf.adb_manager import run_adb_command
from droidperf.settings_manager import settings as _settings
from droidperf.alert_engine import AlertEngine
from droidperf.collectors.battery import get_battery_info
from droidperf.collectors.battery_stats import get_battery_attribution, reset_battery_stats
from droidperf.collectors.cpu import get_cpu_usage
from droidperf.collectors.disk_io import get_disk_io
from droidperf.collectors.memory import get_total_pss
from droidperf.collectors.network import get_network_stats
from droidperf.collectors.process_stats import get_process_stats
from droidperf.db import TelemetryDB
from droidperf.logcat_watcher import LogcatWatcher
from droidperf.process_discovery import get_running_packages
from droidperf.reporter import generate_html_report

logger = logging.getLogger(__name__)

# CSV column order for incremental writes.
_CSV_FIELDNAMES = [
    "timestamp",
    "device_id",
    "package",
    "ram_pss_kb",
    "cpu_total_pct",
    "cpu_user_pct",
    "cpu_kernel_pct",
    "cpu_load_1m",
    "cpu_load_5m",
    "cpu_load_15m",
    "batt_level",
    "batt_temp_c",
    "batt_voltage_mv",
    "batt_status",
    # Network I/O (deltas per cycle, bytes)
    "net_rx_delta_bytes",
    "net_tx_delta_bytes",
    # Disk I/O (deltas per cycle, bytes)
    "disk_read_delta_bytes",
    "disk_write_delta_bytes",
    # Process-level stats
    "thread_count",
    "fd_count",
]


class MonitorEngine:
    """
    Background telemetry engine for multi-package Android monitoring.

    Spawns a single daemon thread that polls the device at a fixed
    ``interval`` and collects RAM, CPU, and battery metrics.  Each cycle
    produces one record dict per monitored package; all records are
    delivered to ``on_snapshot`` and written to an incrementally-flushed
    CSV file.

    Args:
        device_id (str):
            ADB serial number of the target device.
        packages (List[str] | None):
            Explicit list of package names to monitor.  Pass ``None`` or
            an empty list to enable auto-discovery mode, which runs
            ``get_running_packages()`` at the start of every cycle.
        interval (float):
            Polling interval in seconds (default 5.0).
        output_dir (Path):
            Directory for CSV and HTML report output (created if absent).
        on_snapshot (Callable[[List[Dict]], None] | None):
            Called from the worker thread after each cycle with the list
            of new record dicts.  The caller is responsible for any
            thread-safety (typically ``queue.Queue.put``).
        on_error (Callable[[str], None] | None):
            Called from the worker thread when a fatal error occurs,
            with a human-readable message string.
    """

    def __init__(
        self,
        device_id: str,
        packages: Optional[List[str]],
        interval: float = 5.0,
        output_dir: Path = Path("reports"),
        on_snapshot: Optional[Callable[[List[Dict]], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        on_alert: Optional[Callable] = None,
    ) -> None:
        self._device_id = device_id
        self._packages: List[str] = list(packages) if packages else []
        self._interval = max(1.0, float(interval))
        self._output_dir = Path(output_dir)
        self._on_snapshot = on_snapshot
        self._on_error = on_error

        # Alert engine — fires on_alert for threshold / spike events.
        self._alert_engine: Optional[AlertEngine] = (
            AlertEngine(on_alert) if on_alert is not None else None
        )

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._all_records: List[Dict] = []
        self._csv_path: Optional[Path] = None
        self._csv_file = None
        self._csv_writer = None
        self._logcat_watcher: Optional[LogcatWatcher] = None
        self._db: Optional[TelemetryDB] = None
        self._db_session_id: Optional[int] = None
        self._meta_logcat_events: List[Dict] = []

        # Previous cumulative counters for delta computation (keyed by package).
        # Structure: {pkg: {"rx": int, "tx": int, "read": int, "write": int}}
        self._prev_io: Dict[str, Dict[str, Optional[int]]] = {}

        logger.info(
            "MonitorEngine created — device='%s' packages=%s interval=%.1fs "
            "output_dir='%s' auto_mode=%s",
            self._device_id,
            self._packages or "<auto-discover>",
            self._interval,
            self._output_dir,
            self.auto_mode,
        )

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        """``True`` while the worker thread is alive."""
        return self._thread is not None and self._thread.is_alive()

    @property
    def auto_mode(self) -> bool:
        """``True`` when no explicit package list was provided at construction."""
        return not bool(self._packages)

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    def start(self) -> None:
        """
        Start the background monitoring thread.

        Creates the output directory and CSV file, starts a
        ``LogcatWatcher`` for crash/ANR capture, and launches the
        daemon worker thread.  Calling ``start()`` on an already-running
        engine is a no-op (logs a warning).
        """
        if self.is_running:
            logger.warning("MonitorEngine.start() called but engine is already running.")
            return

        try:
            self._output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            msg = f"Cannot create output directory '{self._output_dir}': {exc}"
            logger.error(msg)
            if self._on_error:
                self._on_error(msg)
            return

        timestamp_tag = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._csv_path = self._output_dir / f"telemetry_{timestamp_tag}.csv"

        try:
            self._csv_file = open(  # noqa: WPS515  (kept open for incremental writes)
                self._csv_path, "w", newline="", encoding="utf-8"
            )
            self._csv_writer = csv.DictWriter(
                self._csv_file, fieldnames=_CSV_FIELDNAMES, extrasaction="ignore"
            )
            self._csv_writer.writeheader()
            self._csv_file.flush()
        except OSError as exc:
            msg = f"Cannot open CSV file '{self._csv_path}': {exc}"
            logger.error(msg)
            if self._on_error:
                self._on_error(msg)
            return

        # Reset battery stats so per-app attribution covers only this session.
        reset_battery_stats(self._device_id)

        # Start logcat watcher for crash / ANR events.
        # Pass screenshot_dir only when the user has enabled crash screenshots.
        _scr_dir = self._output_dir if _settings.get("crash_screenshots", True) else None
        try:
            self._logcat_watcher = LogcatWatcher(
                self._device_id,
                screenshot_dir=_scr_dir,
            )
            self._logcat_watcher.start()
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("Could not start LogcatWatcher: %s", exc)
            self._logcat_watcher = None

        # Open SQLite DB (best-effort — failures don't block CSV monitoring).
        try:
            db_path = self._output_dir / "telemetry.db"
            self._db = TelemetryDB(db_path)
            self._db_session_id = self._db.open_session(
                self._device_id, self._packages
            )
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("Could not open TelemetryDB (will continue CSV-only): %s", exc)
            self._db = None
            self._db_session_id = None

        self._stop_event.clear()
        self._all_records.clear()
        self._prev_io.clear()
        if self._alert_engine:
            self._alert_engine.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="MonitorEngine-Worker",
            daemon=True,
        )
        self._thread.start()
        logger.info("MonitorEngine started (thread='%s').", self._thread.name)

    def stop(self) -> None:
        """
        Signal the worker thread to stop and wait for it to finish.

        After the thread exits, closes the CSV file and generates the
        HTML report.  Calling ``stop()`` on an engine that is not running
        is a no-op (logs a warning).
        """
        if not self.is_running:
            logger.warning("MonitorEngine.stop() called but engine is not running.")
            return

        logger.info("MonitorEngine stop requested.")
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=self._interval + 10)
            if self._thread.is_alive():
                logger.warning("Worker thread did not stop within the timeout.")

        self._cleanup()
        logger.info("MonitorEngine stopped.")

    # ------------------------------------------------------------------
    # Internal — worker loop
    # ------------------------------------------------------------------

    def _run_loop(self) -> None:
        """
        Main polling loop executed in the daemon thread.

        Iterates until the stop event is set.  Each iteration:
          1. Optionally auto-discovers packages.
          2. Fetches battery info once (device-level).
          3. Fetches RAM and CPU for each package.
          4. Assembles record dicts and delivers them to the caller.
          5. Sleeps for the configured interval.
        """
        logger.debug("Worker loop started.")
        while not self._stop_event.is_set():
            cycle_start = time.monotonic()
            try:
                self._collect_cycle()
            except Exception as exc:  # pylint: disable=broad-except
                msg = f"Unexpected error in monitoring cycle: {exc}"
                logger.exception(msg)
                if self._on_error:
                    self._on_error(msg)
                # Do not abort the loop on transient errors.

            elapsed = time.monotonic() - cycle_start
            sleep_time = max(0.0, self._interval - elapsed)
            self._stop_event.wait(timeout=sleep_time)

        logger.debug("Worker loop exited.")

    def _collect_cycle(self) -> None:
        """
        Execute a single collection cycle and deliver results.

        Fetches device-level battery info first, then iterates over
        each target package to collect RAM and CPU.  Builds a flat
        record dict per package and appends to ``self._all_records``.
        """
        packages = (
            get_running_packages(self._device_id)
            if self.auto_mode
            else self._packages
        )
        if not packages:
            logger.warning(
                "device='%s': no packages to monitor (auto_mode=%s).",
                self._device_id,
                self.auto_mode,
            )
            return

        timestamp = datetime.now().isoformat(timespec="seconds")

        # Battery is device-level — fetch once per cycle.
        batt = get_battery_info(self._device_id)
        batt_level = batt["level"] if batt else None
        batt_temp_c = batt["temperature"] if batt else None
        batt_voltage_mv = batt["voltage_mv"] if batt else None
        batt_status = batt["status"] if batt else None

        # Pre-fetch dumpsys cpuinfo once per cycle.  This covers ALL running
        # processes with delta-based CPU%, so each per-package get_cpu_usage()
        # call can look up its process without an extra ADB round-trip.
        cpuinfo_cache = run_adb_command(self._device_id, "dumpsys cpuinfo")

        rows: List[Dict] = []
        for pkg in packages:
            ram_pss_kb = get_total_pss(self._device_id, pkg)
            cpu_info = get_cpu_usage(self._device_id, pkg, cpuinfo_output=cpuinfo_cache)

            # ── Network I/O delta ─────────────────────────────────────
            net = get_network_stats(self._device_id, pkg)
            prev_io = self._prev_io.get(pkg, {})
            net_rx_delta = self._delta(net.get("rx_bytes"), prev_io.get("net_rx"))
            net_tx_delta = self._delta(net.get("tx_bytes"), prev_io.get("net_tx"))

            # ── Disk I/O delta ────────────────────────────────────────
            disk = get_disk_io(self._device_id, pkg)
            disk_read_delta  = self._delta(disk.get("read_bytes"), prev_io.get("disk_read"))
            disk_write_delta = self._delta(disk.get("write_bytes"), prev_io.get("disk_write"))

            # Save current cumulative values for next cycle.
            self._prev_io[pkg] = {
                "net_rx":    net.get("rx_bytes"),
                "net_tx":    net.get("tx_bytes"),
                "disk_read":  disk.get("read_bytes"),
                "disk_write": disk.get("write_bytes"),
            }

            # ── Process stats ─────────────────────────────────────────
            proc = get_process_stats(self._device_id, pkg)

            record: Dict = {
                "timestamp": timestamp,
                "device_id": self._device_id,
                "package": pkg,
                "ram_pss_kb": ram_pss_kb,
                "cpu_total_pct": cpu_info.get("total_pct") if cpu_info else None,
                "cpu_user_pct": cpu_info.get("user_pct") if cpu_info else None,
                "cpu_kernel_pct": cpu_info.get("kernel_pct") if cpu_info else None,
                "cpu_load_1m": cpu_info.get("load_1m") if cpu_info else None,
                "cpu_load_5m": cpu_info.get("load_5m") if cpu_info else None,
                "cpu_load_15m": cpu_info.get("load_15m") if cpu_info else None,
                "batt_level": batt_level,
                "batt_temp_c": batt_temp_c,
                "batt_voltage_mv": batt_voltage_mv,
                "batt_status": batt_status,
                "net_rx_delta_bytes": net_rx_delta,
                "net_tx_delta_bytes": net_tx_delta,
                "disk_read_delta_bytes": disk_read_delta,
                "disk_write_delta_bytes": disk_write_delta,
                "thread_count": proc.get("thread_count"),
                "fd_count": proc.get("fd_count"),
            }
            rows.append(record)

        self._all_records.extend(rows)
        self._write_csv_rows(rows)

        # Evaluate alerts after writing so spikes are always recorded.
        if self._alert_engine:
            try:
                self._alert_engine.check(rows)
            except Exception as exc:  # pylint: disable=broad-except
                logger.error("AlertEngine.check raised an exception: %s", exc)

        if self._on_snapshot:
            try:
                self._on_snapshot(rows)
            except Exception as exc:  # pylint: disable=broad-except
                logger.error("on_snapshot callback raised an exception: %s", exc)

    def _write_meta_json(
        self,
        html_path: Path,
        package_label: str,
        packages: List[str],
        logcat_events: Optional[List[Dict]] = None,
    ) -> None:
        """
        Write a sidecar ``<report_name>.meta.json`` file alongside the HTML
        report so the ReportPanel can display tags and support full-text search
        without opening the HTML file.  Logcat events are also stored so that
        the PDF export can include them without re-parsing the HTML.

        Args:
            html_path (Path):           Path of the generated HTML report.
            package_label (str):        Display name (single pkg or "N packages").
            packages (List[str]):       All packages present in the session.
            logcat_events (List[Dict]): Captured crash/ANR events (optional).
        """
        self._meta_logcat_events = logcat_events or []
        meta_path = html_path.with_suffix(".meta.json")
        timestamps = [r.get("timestamp") for r in self._all_records if r.get("timestamp")]
        meta = {
            "report_file": html_path.name,
            "device_id": self._device_id,
            "package_label": package_label,
            "packages": packages,
            "record_count": len(self._all_records),
            "session_start": timestamps[0] if timestamps else None,
            "session_end": timestamps[-1] if timestamps else None,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "logcat_events": self._meta_logcat_events,
        }
        try:
            with open(meta_path, "w", encoding="utf-8") as fh:
                json.dump(meta, fh, indent=2)
            logger.debug("Sidecar metadata written: '%s'.", meta_path.name)
        except OSError as exc:
            logger.warning("Could not write sidecar metadata: %s", exc)

    @staticmethod
    def _delta(
        current: Optional[int],
        previous: Optional[int],
    ) -> Optional[int]:
        """
        Compute a non-negative delta between two cumulative counters.

        Returns ``None`` when either value is unknown (first cycle or
        permission-denied reads).  Returns 0 if the counter wrapped
        or was reset (current < previous).

        Args:
            current (int | None):  Latest cumulative counter value.
            previous (int | None): Previous cumulative counter value.

        Returns:
            Optional[int]: Bytes transferred this cycle, or ``None``.
        """
        if current is None or previous is None:
            return None
        diff = current - previous
        return max(0, diff)

    def _write_csv_rows(self, rows: List[Dict]) -> None:
        """
        Write *rows* to the open CSV file and flush immediately.

        Args:
            rows (List[Dict]): Record dicts from the current cycle.
        """
        if self._csv_writer is None or self._csv_file is None:
            return
        try:
            self._csv_writer.writerows(rows)
            self._csv_file.flush()
        except OSError as exc:
            logger.error("Failed to write CSV rows: %s", exc)

        # Parallel write to SQLite (best-effort).
        if self._db is not None and self._db_session_id is not None:
            try:
                self._db.insert_records(self._db_session_id, rows)
            except Exception as exc:  # pylint: disable=broad-except
                logger.warning("DB insert_records failed: %s", exc)

    # ------------------------------------------------------------------
    # Internal — cleanup and report generation
    # ------------------------------------------------------------------

    def _cleanup(self) -> None:
        """
        Close open resources and generate the final HTML report.

        Stops the ``LogcatWatcher``, closes the CSV file handle, and
        delegates report generation to ``reporter.generate_html_report()``.
        """
        # Stop logcat watcher and collect events.
        logcat_events: List[Dict] = []
        if self._logcat_watcher is not None:
            try:
                self._logcat_watcher.stop()
                logcat_events = list(self._logcat_watcher.events)
            except Exception as exc:  # pylint: disable=broad-except
                logger.warning("Error stopping LogcatWatcher: %s", exc)
            self._logcat_watcher = None

        # Close CSV.
        if self._csv_file is not None:
            try:
                self._csv_file.close()
            except OSError as exc:
                logger.warning("Error closing CSV file: %s", exc)
            self._csv_file = None
            self._csv_writer = None

        # Generate HTML report.
        if not self._all_records:
            logger.warning("No records collected — skipping HTML report generation.")
            return

        # Determine a representative package label for the report title.
        packages_seen = sorted(
            {r["package"] for r in self._all_records if r.get("package")}
        )
        package_label = (
            packages_seen[0] if len(packages_seen) == 1
            else f"{len(packages_seen)} packages"
        )

        timestamp_tag = datetime.now().strftime("%Y%m%d_%H%M%S")
        html_path = self._output_dir / f"report_{timestamp_tag}.html"

        # Collect per-app battery attribution for the session.
        battery_attribution = {}
        try:
            battery_attribution = get_battery_attribution(self._device_id)
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("Battery attribution collection failed: %s", exc)

        try:
            generate_html_report(
                records=self._all_records,
                logcat_events=logcat_events,
                device_id=self._device_id,
                package_name=package_label,
                output_path=html_path,
                battery_attribution=battery_attribution,
            )
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("HTML report generation failed: %s", exc)

        # Write sidecar metadata for the ReportPanel (search / display).
        self._write_meta_json(html_path, package_label, packages_seen, logcat_events)

        # Close DB session.
        if self._db is not None and self._db_session_id is not None:
            try:
                self._db.close_session(
                    self._db_session_id,
                    report_html=str(html_path.name),
                )
            except Exception as exc:  # pylint: disable=broad-except
                logger.warning("DB close_session failed: %s", exc)
            finally:
                self._db.close()
                self._db = None
                self._db_session_id = None
