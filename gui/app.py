"""
gui/app.py
----------
Root application window for the ADB Telemetry & Health Monitor.

Builds the main two-column layout:
  - Left sidebar: ``ControlPanel`` (multi-device selection, mode, interval,
    start/stop controls) + ``RankingPanel`` (CPU ranking).
  - Right area: ``CTkTabview`` with three tabs:
      • Dashboard — ``StatCards`` (live KPI strip) + ``ChartPanel``
      • Reports   — ``ReportPanel`` (HTML report browser)
      • Settings  — ``SettingsPanel`` (persistent config + package presets)

Multi-device support: one ``MonitorEngine`` is spawned per selected
device.  All engines share the same ``queue.Queue`` and deliver their
snapshots (tagged with ``device_id``) to the same chart and ranking
panels so metrics from every device are visualised together.

Background monitoring is performed by ``MonitorEngine`` instances in
daemon threads.  Chart updates are delivered thread-safely via a
``queue.Queue`` that is drained every 500 ms by ``_poll_queue()``.
"""

import logging
import queue
import threading
from pathlib import Path
from typing import Dict, List, Optional

from droidperf.alert_engine import AlertEvent
from droidperf import notifier

import customtkinter as ctk

try:
    import pystray
    from PIL import Image as _PilImage, ImageDraw as _PilDraw
    _TRAY_AVAILABLE = True
except ImportError:
    _TRAY_AVAILABLE = False

from droidperf.adb_manager import get_connected_devices
from droidperf.i18n import t
from droidperf.monitor_engine import MonitorEngine
from droidperf.settings_manager import settings as app_settings
from gui.widgets.chart_panel import ChartPanel
from gui.widgets.control_panel import ControlPanel
from gui.widgets.ranking_panel import RankingPanel
from gui.widgets.report_panel import ReportPanel
from gui.widgets.screenshots_panel import ScreenshotsPanel
from gui.widgets.settings_panel import SettingsPanel
from gui.widgets.stat_cards import StatCards

logger = logging.getLogger(__name__)

# Apply global CustomTkinter appearance settings before any widget is created.
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


class App(ctk.CTk):
    """
    Main application window.

    Wires together the ``ControlPanel``, ``ChartPanel``, and
    ``ReportPanel`` widgets and manages the ``MonitorEngine`` lifecycle
    for one or more simultaneously connected devices.

    The ``queue.Queue`` pattern decouples the background worker threads
    from the Tkinter event loop: each ``MonitorEngine`` only calls
    ``queue.put()``, while ``_poll_queue()`` (scheduled via
    ``after()``) drains the queue on the main thread and forwards rows
    to the chart panel.
    """

    def __init__(self) -> None:
        super().__init__()

        self.title(t("app_title"))
        self.geometry("1280x780")
        self.minsize(860, 520)

        # Maps device_id → MonitorEngine for all active sessions.
        self._engines: Dict[str, MonitorEngine] = {}
        self._queue: queue.Queue = queue.Queue()
        self._alert_queue: queue.Queue = queue.Queue()
        self._output_dir = Path("reports")

        self._banner_hide_job = None
        self._tray_icon: Optional[object] = None
        self._scheduler_stop = threading.Event()

        self._build_layout()
        self.protocol("WM_DELETE_WINDOW", self.on_closing)

        # Kick off periodic queue polling and initial device scan.
        self._poll_queue()
        self._refresh_devices()
        self._cleanup_old_reports()
        self._start_scheduler()

        logger.info("App window created.")

    # ------------------------------------------------------------------
    # Layout construction
    # ------------------------------------------------------------------

    def _build_layout(self) -> None:
        """Create the sidebar + tabview layout and instantiate child panels."""
        # Column 0: fixed-width sidebar (ControlPanel top, RankingPanel bottom).
        # Column 1: expanding content area (tabview).
        self.grid_columnconfigure(0, weight=0, minsize=280)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=0)

        # Left sidebar — ControlPanel (row 0).
        self.control_panel = ControlPanel(
            master=self,
            on_start=self._on_start,
            on_stop=self._on_stop,
            on_refresh_devices=self._refresh_devices,
            on_preset_load=self._on_preset_load,
            width=280,
            corner_radius=0,
        )
        self.control_panel.grid(row=0, column=0, sticky="nsew")

        # Left sidebar — RankingPanel pinned to the bottom (row 1).
        self.ranking_panel = RankingPanel(master=self, width=280, corner_radius=0)
        self.ranking_panel.grid(row=1, column=0, sticky="nsew")

        # Right area — tabbed view (spans both rows).
        tab_view = ctk.CTkTabview(self)
        tab_view.grid(row=0, column=1, rowspan=2, sticky="nsew", padx=(4, 0))

        tab_view.add(t("tab_dashboard"))
        tab_view.add(t("tab_reports"))
        tab_view.add(t("tab_crashes"))
        tab_view.add(t("tab_settings"))

        # ── Dashboard tab ─────────────────────────────────────────────
        dash_tab = tab_view.tab(t("tab_dashboard"))
        dash_tab.grid_rowconfigure(0, weight=0)   # stat cards
        dash_tab.grid_rowconfigure(1, weight=0)   # alert banner (hidden)
        dash_tab.grid_rowconfigure(2, weight=1)   # chart panel
        dash_tab.grid_columnconfigure(0, weight=1)

        # Top strip: live KPI summary cards.
        self.stat_cards = StatCards(master=dash_tab)
        self.stat_cards.grid(row=0, column=0, sticky="ew", padx=4, pady=(4, 0))

        # Alert banner — hidden until an alert fires.
        self._alert_banner = ctk.CTkLabel(
            dash_tab,
            text="",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#ffffff",
            fg_color="#922b21",
            corner_radius=6,
            anchor="w",
        )
        self._alert_banner_visible = False

        # Main area: real-time Matplotlib chart panel.
        self.chart_panel = ChartPanel(master=dash_tab)
        self.chart_panel.grid(row=2, column=0, sticky="nsew")

        # ── Reports tab ───────────────────────────────────────────────
        self.report_panel = ReportPanel(
            master=tab_view.tab(t("tab_reports")),
            reports_dir=self._output_dir,
        )
        self.report_panel.pack(fill="both", expand=True)

        # ── Crashes tab ───────────────────────────────────────────────
        self.screenshots_panel = ScreenshotsPanel(
            master=tab_view.tab(t("tab_crashes")),
        )
        self.screenshots_panel.pack(fill="both", expand=True)

        # ── Settings tab ──────────────────────────────────────────────
        self.settings_panel = SettingsPanel(
            master=tab_view.tab(t("tab_settings")),
            on_preset_load=self._on_preset_load,
            on_preset_saved=lambda: self.control_panel.refresh_presets(),
            on_settings_saved=self._on_settings_saved,
        )
        self.settings_panel.pack(fill="both", expand=True)

    # ------------------------------------------------------------------
    # Queue polling (thread-safe chart updates)
    # ------------------------------------------------------------------

    def _poll_queue(self) -> None:
        """
        Drain the snapshot queue and forward rows to the chart panel.

        Scheduled to run every 500 ms via ``after()``.
        """
        try:
            while True:
                rows = self._queue.get_nowait()
                self.stat_cards.update(rows)
                self.chart_panel.update(rows)
                self.ranking_panel.update(rows)
        except queue.Empty:
            pass
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("Error processing snapshot queue: %s", exc)

        # Drain alert queue — show the most recent alert in the banner.
        latest_alert: Optional[AlertEvent] = None
        try:
            while True:
                latest_alert = self._alert_queue.get_nowait()
        except queue.Empty:
            pass
        if latest_alert is not None:
            self._show_alert_banner(latest_alert)

        self.after(250, self._poll_queue)

    # ------------------------------------------------------------------
    # Device refresh
    # ------------------------------------------------------------------

    def _refresh_devices(self) -> None:
        """
        Query ADB for connected devices and update the control panel.

        Updates the device checkbox list and status label with results.
        """
        try:
            devices = get_connected_devices()
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("Error refreshing devices: %s", exc)
            devices = []

        self.control_panel.populate_devices(devices)

        if not devices:
            self.control_panel.set_status("No device found", "#e74c3c")
        else:
            self.control_panel.set_status(
                f"{len(devices)} device(s) found", "#27ae60"
            )

    # ------------------------------------------------------------------
    # Monitoring lifecycle
    # ------------------------------------------------------------------

    def _on_start(self, settings: dict) -> None:
        """
        Validate settings, create one ``MonitorEngine`` per selected
        device, and start all engines concurrently.

        Args:
            settings (dict): Dict from ``ControlPanel.get_settings()``.
                             Keys: ``device_ids``, ``mode``, ``packages``,
                             ``interval``.
        """
        device_ids: List[str] = settings.get("device_ids", [])
        if not device_ids:
            self.control_panel.set_status(
                "Please select at least one device.", "#e74c3c"
            )
            logger.warning("Start aborted: no device selected.")
            return

        mode = settings.get("mode", "auto")
        packages = settings.get("packages", [])

        if mode == "custom" and not packages:
            self.control_panel.set_status(
                "Custom mode: enter at least one package.", "#e74c3c"
            )
            logger.warning("Start aborted: custom mode with empty package list.")
            return

        interval = float(settings.get("interval", 5.0))

        # Stop any running engines before starting new ones.
        if self._engines:
            logger.info("Stopping %d existing engine(s) before restart.", len(self._engines))
            for engine in self._engines.values():
                if engine.is_running:
                    engine._stop_event.set()  # noqa: SLF001
            self._engines.clear()

        self.stat_cards.clear()
        self.chart_panel.clear()
        self.ranking_panel.clear()
        self._hide_alert_banner()

        for device_id in device_ids:
            engine = MonitorEngine(
                device_id=device_id,
                packages=packages if mode == "custom" else [],
                interval=interval,
                output_dir=self._output_dir,
                on_snapshot=self._on_snapshot,
                on_error=lambda msg, did=device_id: self._on_error(did, msg),
                on_alert=self._on_alert_threadsafe,
            )
            engine.start()
            self._engines[device_id] = engine

        self.control_panel.set_running(True)
        device_label = ", ".join(device_ids)
        self.control_panel.set_status(f"Monitoring… [{device_label}]", "#f39c12")
        logger.info(
            "Monitoring started — devices=%s mode=%s packages=%s interval=%.1fs",
            device_ids, mode, packages or "<auto>", interval,
        )

    def _on_stop(self) -> None:
        """
        Stop all running ``MonitorEngine`` instances without blocking the UI.

        Each engine is stopped in a background thread; ``_on_stop_complete``
        is called on the main thread once all engines have finished.
        """
        running = [e for e in self._engines.values() if e.is_running]
        if not running:
            logger.warning("Stop requested but no engines are running.")
            return

        self.control_panel.set_running(False)
        self.control_panel.set_status("Stopping — generating report...", "#f39c12")

        engines_snapshot = list(running)
        threading.Thread(
            target=self._stop_engines_bg,
            args=(engines_snapshot,),
            daemon=True,
            name="EngineStopWorker",
        ).start()

    def _stop_engines_bg(self, engines: List[MonitorEngine]) -> None:
        """
        Background thread: stop all engines and notify the main thread.

        Never touches Tkinter widgets directly — all UI updates are
        marshalled back via ``after(0, ...)``.
        """
        for engine in engines:
            try:
                engine.stop()
                logger.info("Engine for device '%s' stopped.", engine._device_id)  # noqa: SLF001
            except Exception as exc:  # pylint: disable=broad-except
                logger.error("Error while stopping engine: %s", exc)

        self._engines.clear()
        self.after(0, self._on_stop_complete)

    def _on_stop_complete(self) -> None:
        """
        Called on the main thread once all engines have fully stopped.

        Updates the status label and refreshes the reports panel.
        """
        self.control_panel.set_status("Stopped. Report(s) saved.", "#27ae60")
        self.report_panel._scan_reports()   # noqa: SLF001
        self.report_panel._populate()       # noqa: SLF001
        self.screenshots_panel.refresh()
        logger.info("Stop complete — UI refreshed.")

    # ------------------------------------------------------------------
    # Engine callbacks (called from background threads)
    # ------------------------------------------------------------------

    def _on_alert_threadsafe(self, event: AlertEvent) -> None:
        """
        Receive an alert from a monitoring thread (thread-safe).

        Sends outbound notifications (webhook / OS toast) synchronously
        in the worker thread, then enqueues the event for the main thread
        to update the GUI banner.

        Args:
            event (AlertEvent): Triggered alert descriptor.
        """
        # Outbound notifications — safe to call from background thread.
        try:
            notifier.notify(event)
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("Notifier raised an exception: %s", exc)

        # GUI update via queue — consumed by _poll_queue() on main thread.
        self._alert_queue.put(event)

    def _on_snapshot(self, rows: list) -> None:
        """
        Receive a snapshot from a monitoring thread (thread-safe).

        Only enqueues the rows; chart rendering happens on the main
        thread via ``_poll_queue()``.

        Args:
            rows (list): Record dicts produced by one monitoring cycle.
        """
        self._queue.put(rows)

    def _on_error(self, device_id: str, msg: str) -> None:
        """
        Handle a fatal error from a monitoring thread (thread-safe).

        Schedules status and button-state updates on the main thread via
        ``after(0, ...)``.

        Args:
            device_id (str): Serial of the device that raised the error.
            msg (str):       Human-readable error message.
        """
        logger.error("MonitorEngine error [device='%s']: %s", device_id, msg)
        self.after(
            0,
            lambda: self.control_panel.set_status(
                f"Error [{device_id}]: {msg}", "#e74c3c"
            ),
        )
        # Only reset buttons if no other engines are still running.
        self.after(0, self._check_all_stopped)

    def _check_all_stopped(self) -> None:
        """Disable Start/Stop if no engines are active after an error."""
        if not any(e.is_running for e in self._engines.values()):
            self.control_panel.set_running(False)

    # ------------------------------------------------------------------
    # Settings callbacks
    # ------------------------------------------------------------------

    def _cleanup_old_reports(self) -> None:
        """
        Delete HTML reports (and paired CSV + meta.json files) that are
        older than ``report_retention_days`` setting.

        Skips cleanup when retention is set to 0 (keep forever).
        """
        retention_days: int = app_settings.get("report_retention_days", 0)
        if retention_days <= 0:
            return

        if not self._output_dir.exists():
            return

        import time
        cutoff = time.time() - retention_days * 86400
        deleted = 0

        try:
            for html_path in self._output_dir.glob("report_*.html"):
                try:
                    if html_path.stat().st_mtime < cutoff:
                        html_path.unlink()
                        # Paired CSV
                        csv_name = html_path.name.replace(
                            "report_", "telemetry_", 1
                        ).replace(".html", ".csv")
                        csv_path = self._output_dir / csv_name
                        if csv_path.exists():
                            csv_path.unlink()
                        # Sidecar meta
                        meta_path = html_path.with_suffix(".meta.json")
                        if meta_path.exists():
                            meta_path.unlink()
                        deleted += 1
                except OSError as exc:
                    logger.warning("Could not delete old report '%s': %s", html_path.name, exc)
        except OSError as exc:
            logger.error("Error during report cleanup: %s", exc)

        if deleted:
            logger.info("Auto-cleanup removed %d report(s) older than %d days.", deleted, retention_days)

    def _show_alert_banner(self, event: AlertEvent) -> None:
        """
        Display the alert message in the red banner above the charts.

        The banner auto-hides after 8 seconds.

        Args:
            event (AlertEvent): Alert to display.
        """
        kind_label = "⚠  SPIKE" if event.kind == "spike" else "⚠  ALERT"
        self._alert_banner.configure(
            text=f"  {kind_label}  {event.message}",
        )
        if not self._alert_banner_visible:
            self._alert_banner.grid(row=1, column=0, sticky="ew", padx=4, pady=(2, 0))
            self._alert_banner_visible = True

        # Auto-hide after 8 s (cancel any pending hide first).
        if hasattr(self, "_banner_hide_job") and self._banner_hide_job:
            self.after_cancel(self._banner_hide_job)
        self._banner_hide_job = self.after(8000, self._hide_alert_banner)

    def _hide_alert_banner(self) -> None:
        """Remove the alert banner from the grid."""
        if self._alert_banner_visible:
            self._alert_banner.grid_forget()
            self._alert_banner_visible = False
        self._banner_hide_job = None

    def _on_settings_saved(self) -> None:
        """
        Called by ``SettingsPanel`` whenever settings are persisted.

        Re-applies settings that should take effect immediately without
        an application restart (e.g. report retention cleanup).
        """
        self._cleanup_old_reports()

    def _on_preset_load(self, packages: List[str]) -> None:
        """
        Called by either ``SettingsPanel`` or ``ControlPanel`` when the user
        loads a preset.

        Switches the control panel to Custom mode, fills the package textbox,
        and keeps the SettingsPanel textbox in sync.

        Args:
            packages (List[str]): Package names from the preset.
        """
        if not packages:
            return
        # Update control panel.
        self.control_panel._mode_var.set("custom")   # noqa: SLF001
        self.control_panel._on_mode_change()          # noqa: SLF001
        tb = self.control_panel._package_textbox      # noqa: SLF001
        tb.delete("0.0", "end")
        tb.insert("0.0", "\n".join(packages))
        # Keep SettingsPanel preset textbox in sync.
        try:
            sp_tb = self.settings_panel._preset_pkg_box   # noqa: SLF001
            sp_tb.delete("0.0", "end")
            sp_tb.insert("0.0", "\n".join(packages))
        except Exception:  # pylint: disable=broad-except
            pass
        logger.info("Preset loaded into control panel (%d packages).", len(packages))

    # ------------------------------------------------------------------
    # Window close
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Scheduled monitoring
    # ------------------------------------------------------------------

    def _start_scheduler(self) -> None:
        """
        Launch the background scheduler thread if scheduling is enabled.

        The thread wakes every 30 seconds, compares current time to the
        configured start time, and triggers monitoring for the configured
        duration.  For ``"once"`` repeats the scheduler disables itself
        after the first triggered run.
        """
        if not app_settings.get("schedule_enabled", False):
            return

        threading.Thread(
            target=self._scheduler_loop,
            daemon=True,
            name="SchedulerThread",
        ).start()
        logger.info("Scheduler started.")

    def _scheduler_loop(self) -> None:
        """
        Background loop: check every 30 s whether a scheduled run is due.

        Fires ``_on_start`` on the main thread via ``after(0, ...)`` when
        the current time matches the configured ``schedule_time`` (within
        a 30-second window).
        """
        import time as _time
        from datetime import datetime as _dt

        triggered_today: Optional[str] = None   # date string of last trigger

        while not self._scheduler_stop.is_set():
            _time.sleep(30)

            if not app_settings.get("schedule_enabled", False):
                continue

            now = _dt.now()
            today_str = now.strftime("%Y-%m-%d")
            sched_time_str = app_settings.get("schedule_time", "02:00")

            try:
                h, m = (int(p) for p in sched_time_str.split(":")[:2])
            except ValueError:
                continue

            due = now.hour == h and now.minute == m

            if due and triggered_today != today_str and not self._engines:
                triggered_today = today_str
                duration_min: int = int(app_settings.get("schedule_duration_min", 30))
                logger.info(
                    "Scheduled monitoring triggered at %s (duration=%d min).",
                    sched_time_str, duration_min,
                )
                self.after(0, self._scheduled_start, duration_min)

                # For "once" mode, disable scheduler after first trigger.
                if app_settings.get("schedule_repeat", "daily") == "once":
                    app_settings.set("schedule_enabled", False)
                    logger.info("Scheduler disabled after one-time run.")

    def _scheduled_start(self, duration_min: int) -> None:
        """
        Trigger monitoring from the scheduler on the main thread.

        Uses whatever devices and mode are currently selected in the
        control panel.  Automatically stops after ``duration_min`` minutes.

        Args:
            duration_min (int): How long to monitor before auto-stopping.
        """
        settings_dict = self.control_panel.get_settings()
        if not settings_dict.get("device_ids"):
            logger.warning("Scheduler: no devices selected — skipping run.")
            return
        self._on_start(settings_dict)
        stop_ms = int(duration_min * 60 * 1000)
        self.after(stop_ms, self._on_stop)
        logger.info("Scheduled run started; will auto-stop in %d min.", duration_min)

    # ------------------------------------------------------------------
    # System tray
    # ------------------------------------------------------------------

    def _make_tray_icon_image(self):
        """Create a simple coloured circle as the tray icon image."""
        size = 64
        img = _PilImage.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = _PilDraw.Draw(img)
        draw.ellipse([4, 4, size - 4, size - 4], fill="#27ae60", outline="#1e8449", width=3)
        draw.text((18, 18), "ADB", fill="white")
        return img

    def _start_tray(self) -> None:
        """
        Start a pystray tray icon in a daemon thread and hide the window.

        The tray icon provides Show, Stop & Report, and Exit options.
        """
        if not _TRAY_AVAILABLE:
            logger.warning("pystray/Pillow not available — cannot minimise to tray.")
            self.destroy()
            return

        self.withdraw()

        def _show(_icon, _item):
            self.after(0, self.deiconify)
            self.after(0, self.lift)

        def _stop_and_report(_icon, _item):
            self.after(0, self._on_stop)
            self.after(500, self.deiconify)

        def _exit(_icon, _item):
            _icon.stop()
            self.after(0, self._do_destroy)

        menu = pystray.Menu(
            pystray.MenuItem("Show Window", _show, default=True),
            pystray.MenuItem("Stop & Generate Report", _stop_and_report),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit", _exit),
        )
        icon_img = self._make_tray_icon_image()
        self._tray_icon = pystray.Icon(
            "ADB Monitor",
            icon_img,
            "ADB Telemetry & Health Monitor",
            menu,
        )
        threading.Thread(
            target=self._tray_icon.run,
            daemon=True,
            name="TrayIconThread",
        ).start()
        logger.info("Minimised to system tray.")

    def _do_destroy(self) -> None:
        """Stop tray icon, scheduler, and destroy the window cleanly."""
        self._scheduler_stop.set()
        if self._tray_icon:
            try:
                self._tray_icon.stop()
            except Exception:  # pylint: disable=broad-except
                pass
        for engine in self._engines.values():
            if engine.is_running:
                try:
                    engine._stop_event.set()  # noqa: SLF001
                except Exception:  # pylint: disable=broad-except
                    pass
        self.destroy()

    # ------------------------------------------------------------------
    # Window close
    # ------------------------------------------------------------------

    def on_closing(self) -> None:
        """
        Handle the window close button.

        If "Minimize to System Tray" is enabled, hides the window instead
        of destroying it.  Otherwise exits cleanly.
        """
        if app_settings.get("minimize_to_tray", False) and _TRAY_AVAILABLE:
            logger.info("Minimising to tray instead of closing.")
            self._start_tray()
            return

        logger.info("Application closing.")
        self._scheduler_stop.set()
        for engine in self._engines.values():
            if engine.is_running:
                try:
                    engine._stop_event.set()  # noqa: SLF001
                except Exception as exc:  # pylint: disable=broad-except
                    logger.error("Error signalling engine stop on close: %s", exc)
        self.destroy()
