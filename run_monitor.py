"""
run_monitor.py
--------------
CLI entry point for the ADB Telemetry & Health Monitor.

Monitors a connected Android device for a configurable duration, collecting
RAM, CPU, and battery metrics at each interval, watching logcat for
crash/ANR events in the background, and generating CSV + HTML reports on exit.

New flags (Task 9):
    --fail-on-crash         Exit with code 2 if any crash/ANR was detected.
    --alert-threshold KEY=N Inline alert thresholds (e.g. ram_kb=512000).
                            Supported keys: ram_kb, cpu_pct, temp_c, batt_drop.
    --output-format         One of: csv (default), json, both.
                            json writes a ``telemetry_<ts>.json`` file.

Usage examples:
    python run_monitor.py --package com.example.app
    python run_monitor.py --package com.example.app --duration 120 --interval 5
    python run_monitor.py --package com.example.app --device emulator-5554
    python run_monitor.py --package com.example.app --fail-on-crash
    python run_monitor.py --package com.example.app \\
        --alert-threshold ram_kb=512000 --alert-threshold cpu_pct=80
    python run_monitor.py --package com.example.app --output-format json
"""

import argparse
import csv
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from droidperf.adb_manager import get_connected_devices
from droidperf.alert_engine import AlertEngine, AlertEvent
from droidperf.collectors.battery import get_battery_info
from droidperf.collectors.cpu import get_cpu_usage
from droidperf.collectors.memory import get_total_pss
from droidperf.logcat_watcher import LogcatWatcher
from droidperf.reporter import generate_html_report, save_csv
from droidperf.settings_manager import settings


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def setup_logging(log_level: str = "INFO") -> None:
    """
    Configure root logger to write to stdout and a timestamped log file.

    Args:
        log_level (str): Logging level name (e.g. "DEBUG", "INFO").
    """
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / f"monitor_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
    )
    logging.getLogger(__name__).info("Log file: %s", log_file)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    """
    Parse and return CLI arguments.

    Returns:
        argparse.Namespace: Parsed argument values.
    """
    parser = argparse.ArgumentParser(
        description="ADB Telemetry & Health Monitor — Android performance tracker",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--package", required=True,
        help="Target Android package name (e.g. com.example.myapp)",
    )
    parser.add_argument(
        "--duration", type=int, default=60,
        help="Total monitoring duration in seconds",
    )
    parser.add_argument(
        "--interval", type=float, default=5.0,
        help="Metric collection interval in seconds",
    )
    parser.add_argument(
        "--device", default=None,
        help="Device serial number. Auto-detected when only one device is connected.",
    )
    parser.add_argument(
        "--output-dir", default="reports",
        help="Directory for output files",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity",
    )
    parser.add_argument(
        "--fail-on-crash",
        action="store_true",
        help="Exit with code 2 if any crash or ANR event was detected in logcat.",
    )
    parser.add_argument(
        "--alert-threshold",
        action="append",
        metavar="KEY=VALUE",
        dest="alert_thresholds",
        default=[],
        help=(
            "Inline alert threshold. Repeat for multiple thresholds. "
            "Supported keys: ram_kb, cpu_pct, temp_c, batt_drop. "
            "Example: --alert-threshold ram_kb=512000"
        ),
    )
    parser.add_argument(
        "--output-format",
        choices=["csv", "json", "both"],
        default="csv",
        help="Output file format(s). 'both' writes CSV and JSON.",
    )
    return parser.parse_args()


def _apply_cli_thresholds(threshold_args: List[str]) -> None:
    """
    Parse ``KEY=VALUE`` threshold strings and write them to settings.

    Supported keys:
        ram_kb   → alert_ram_kb
        cpu_pct  → alert_cpu_pct
        temp_c   → alert_temp_c
        batt_drop → alert_batt_drop

    Args:
        threshold_args (List[str]): Raw ``KEY=VALUE`` strings from CLI.
    """
    key_map = {
        "ram_kb":    "alert_ram_kb",
        "cpu_pct":   "alert_cpu_pct",
        "temp_c":    "alert_temp_c",
        "batt_drop": "alert_batt_drop",
    }
    logger = logging.getLogger(__name__)
    for item in threshold_args:
        if "=" not in item:
            logger.warning("Ignoring invalid --alert-threshold value: '%s'", item)
            continue
        key, _, raw_val = item.partition("=")
        key = key.strip().lower()
        settings_key = key_map.get(key)
        if not settings_key:
            logger.warning("Unknown threshold key '%s'. Valid keys: %s", key, list(key_map))
            continue
        try:
            value = float(raw_val)
            settings.set(settings_key, value)
            logger.info("Alert threshold set: %s = %s", settings_key, value)
        except ValueError:
            logger.warning("Non-numeric value for threshold '%s': '%s'", key, raw_val)


# ---------------------------------------------------------------------------
# Metric snapshot
# ---------------------------------------------------------------------------

def collect_snapshot(device_id: str, package_name: str) -> Dict:
    """
    Collect one sample of all metrics and return a flat dict row.

    Missing values (e.g. package not running) are stored as ``None`` so the
    CSV and HTML report can distinguish "zero" from "unavailable".

    Args:
        device_id (str):    Target device serial number.
        package_name (str): Package to monitor.

    Returns:
        Dict: Flat row containing a timestamp and all metric fields.
    """
    ram: Optional[int] = get_total_pss(device_id, package_name)
    cpu: Dict = get_cpu_usage(device_id, package_name) or {}
    batt: Dict = get_battery_info(device_id) or {}

    return {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "device_id": device_id,
        "package": package_name,
        "ram_pss_kb": ram,
        "cpu_total_pct": cpu.get("total_pct"),
        "cpu_user_pct": cpu.get("user_pct"),
        "cpu_kernel_pct": cpu.get("kernel_pct"),
        "cpu_load_1m": cpu.get("load_1m"),
        "cpu_load_5m": cpu.get("load_5m"),
        "cpu_load_15m": cpu.get("load_15m"),
        "batt_level": batt.get("level"),
        "batt_temp_c": batt.get("temperature"),
        "batt_voltage_mv": batt.get("voltage_mv"),
        "batt_status": batt.get("status"),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    """
    Parse arguments, run the monitoring loop, and generate reports.

    Exit codes:
        0  — success, no crashes detected (or --fail-on-crash not set)
        1  — startup error (no device, bad args, etc.)
        2  — crashes/ANRs detected and --fail-on-crash was specified
    """
    args = parse_args()
    setup_logging(args.log_level)
    logger = logging.getLogger(__name__)

    # Apply inline threshold overrides before creating the AlertEngine.
    if args.alert_thresholds:
        _apply_cli_thresholds(args.alert_thresholds)

    # ---- Device selection ------------------------------------------------
    device_id: str = args.device or ""
    if not device_id:
        devices = get_connected_devices()
        if not devices:
            logger.error("No connected devices found. Connect a device and retry.")
            sys.exit(1)
        if len(devices) > 1:
            logger.error(
                "Multiple devices found: %s. Use --device <serial> to pick one.",
                devices,
            )
            sys.exit(1)
        device_id = devices[0]

    logger.info(
        "Monitor starting | device=%s | package=%s | duration=%ds | interval=%.1fs",
        device_id, args.package, args.duration, args.interval,
    )

    # ---- Output paths ----------------------------------------------------
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    run_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path  = output_dir / f"telemetry_{run_ts}.csv"
    json_path = output_dir / f"telemetry_{run_ts}.json"
    html_path = output_dir / f"report_{run_ts}.html"

    # ---- Alert engine (CLI-mode: log only) --------------------------------
    alert_events: List[AlertEvent] = []

    def _on_alert(event: AlertEvent) -> None:
        alert_events.append(event)
        level = logging.WARNING
        logger.log(level, "ALERT [%s] %s", event.kind.upper(), event.message)

    alert_engine = AlertEngine(_on_alert)

    # ---- Logcat watcher (background daemon thread) -----------------------
    watcher = LogcatWatcher(device_id, screenshot_dir=output_dir)
    watcher.start()

    # ---- Monitoring loop with incremental CSV write ----------------------
    records: List[Dict] = []
    csv_writer: Optional[csv.DictWriter] = None
    fmt = args.output_format

    csv_file = None
    if fmt in ("csv", "both"):
        try:
            csv_file = open(csv_path, "w", newline="", encoding="utf-8")
        except OSError as exc:
            logger.error("Cannot open CSV file '%s': %s", csv_path, exc)
            sys.exit(1)

    end_time = time.monotonic() + args.duration
    try:
        while time.monotonic() < end_time:
            snapshot = collect_snapshot(device_id, args.package)
            records.append(snapshot)

            # Incremental CSV write.
            if csv_file is not None:
                if csv_writer is None:
                    csv_writer = csv.DictWriter(csv_file, fieldnames=list(snapshot.keys()))
                    csv_writer.writeheader()
                csv_writer.writerow(snapshot)
                csv_file.flush()

            # Real-time alert evaluation.
            try:
                alert_engine.check([snapshot])
            except Exception as exc:  # pylint: disable=broad-except
                logger.error("AlertEngine error: %s", exc)

            logger.info(
                "Sample #%-3d | RAM=%s KB | CPU=%s%% | Battery=%s%%",
                len(records),
                snapshot["ram_pss_kb"],
                snapshot["cpu_total_pct"],
                snapshot["batt_level"],
            )
            time.sleep(args.interval)

    except KeyboardInterrupt:
        logger.warning("Monitoring interrupted by user (Ctrl-C).")

    finally:
        if csv_file is not None:
            csv_file.close()

    # ---- Shutdown --------------------------------------------------------
    watcher.stop()
    watcher.join(timeout=5)

    logger.info(
        "Done. %d samples collected | %d logcat event(s) | %d alert(s) fired.",
        len(records), len(watcher.events), len(alert_events),
    )

    # ---- JSON output -------------------------------------------------------
    if fmt in ("json", "both"):
        try:
            with open(json_path, "w", encoding="utf-8") as jf:
                json.dump(
                    {
                        "device_id": device_id,
                        "package": args.package,
                        "records": records,
                        "logcat_events": watcher.events,
                        "alert_events": [e._asdict() for e in alert_events],
                    },
                    jf,
                    indent=2,
                    ensure_ascii=False,
                    default=str,
                )
            logger.info("JSON output saved: '%s'.", json_path)
        except OSError as exc:
            logger.error("Failed to write JSON output: %s", exc)

    # ---- HTML report generation ------------------------------------------
    generate_html_report(
        records, watcher.events, device_id, args.package, html_path
    )
    logger.info("Reports saved to '%s/'.", output_dir)

    # ---- Exit code --------------------------------------------------------
    if args.fail_on_crash and watcher.events:
        logger.error(
            "--fail-on-crash: %d crash/ANR event(s) detected. Exiting with code 2.",
            len(watcher.events),
        )
        sys.exit(2)


if __name__ == "__main__":
    main()
