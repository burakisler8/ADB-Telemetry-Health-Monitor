"""
gui/widgets/settings_panel.py
------------------------------
Settings & Package Presets panel — displayed in the "Settings" tab.

Sections:
  1. Monitoring defaults  (interval, rolling window, output dir)
  2. Alert thresholds     (RAM KB, CPU %, temp °C, battery drop %)
  3. Notifications        (OS toast, webhook URL)
  4. Package presets      (save / load / delete named package lists)
  5. Reports              (retention days)
  6. System               (minimize to tray, log level)

All values are read from / written to the singleton
``droidperf.settings_manager.settings``.

Public API:
    SettingsPanel(master, **kwargs)
    SettingsPanel.get_presets() -> List[dict]
    SettingsPanel.load_preset(name) -> List[str]   (package list)
"""

import logging
from typing import Any, Callable, Dict, List, Optional

import customtkinter as ctk

from droidperf.i18n import t
from droidperf.settings_manager import settings

logger = logging.getLogger(__name__)

_PAD = dict(padx=14, pady=(4, 2))
_HEAD_PAD = dict(padx=14, pady=(12, 2))


def _section(parent: Any, text: str) -> None:
    ctk.CTkLabel(parent, text=text,
                 font=ctk.CTkFont(size=10, weight="bold"),
                 text_color="#7f8c8d", anchor="w").pack(fill="x", **_HEAD_PAD)
    ctk.CTkFrame(parent, height=1, fg_color="#2d2d2d").pack(fill="x", padx=14, pady=(0, 4))


def _row(parent: Any, label: str) -> ctk.CTkFrame:
    row = ctk.CTkFrame(parent, fg_color="transparent")
    row.pack(fill="x", **_PAD)
    ctk.CTkLabel(row, text=label, font=ctk.CTkFont(size=12),
                 anchor="w", width=180).pack(side="left")
    return row


class SettingsPanel(ctk.CTkScrollableFrame):
    """
    Scrollable settings editor backed by ``settings_manager.settings``.

    Args:
        master: Parent widget (Settings tab frame).
        on_preset_load (Callable[[List[str]], None] | None):
            Called with the package list when the user clicks "Load".
        **kwargs: Forwarded to ``ctk.CTkScrollableFrame.__init__``.
    """

    def __init__(
        self,
        master: Any,
        on_preset_load: Optional[Callable[[List[str]], None]] = None,
        on_preset_saved: Optional[Callable[[], None]] = None,
        on_settings_saved: Optional[Callable[[], None]] = None,
        **kwargs,
    ) -> None:
        kwargs.setdefault("label_text", "")
        kwargs.setdefault("fg_color", "transparent")
        super().__init__(master, **kwargs)

        self._on_preset_load = on_preset_load
        self._on_preset_saved = on_preset_saved
        self._on_settings_saved = on_settings_saved
        self._vars: Dict[str, Any] = {}
        self._build_ui()
        logger.debug("SettingsPanel initialised.")

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        self._build_monitoring()
        self._build_alerts()
        self._build_notifications()
        self._build_presets()
        self._build_reports()
        self._build_schedule()
        self._build_system()

    # ── Monitoring ─────────────────────────────────────────────────────

    def _build_monitoring(self) -> None:
        _section(self, t("section_monitoring"))

        self._vars["default_interval"] = ctk.StringVar(
            value=str(settings.get("default_interval", 5)))
        row = _row(self, t("setting_interval"))
        self._entry(row, "default_interval", width=70)

        self._vars["output_dir"] = ctk.StringVar(
            value=str(settings.get("output_dir", "reports")))
        row = _row(self, t("setting_output_dir"))
        self._entry(row, "output_dir", width=160)

    # ── Alerts ─────────────────────────────────────────────────────────

    def _build_alerts(self) -> None:
        _section(self, t("section_alerts"))

        for key, label, unit in [
            ("alert_ram_kb",    t("setting_alert_ram"),  "KB"),
            ("alert_cpu_pct",   t("setting_alert_cpu"),  "%"),
            ("alert_temp_c",    t("setting_alert_temp"), "°C"),
            ("alert_batt_drop", t("setting_alert_batt"), "%"),
        ]:
            self._vars[key] = ctk.StringVar(value=str(settings.get(key, 0)))
            row = _row(self, f"{label} ({unit})")
            self._entry(row, key, width=70)

        self._vars["spike_std_multiplier"] = ctk.StringVar(
            value=str(settings.get("spike_std_multiplier", 3.0)))
        row = _row(self, t("setting_spike_mult"))
        self._entry(row, "spike_std_multiplier", width=70)

    # ── Notifications ──────────────────────────────────────────────────

    def _build_notifications(self) -> None:
        _section(self, t("section_notifications"))

        self._vars["os_notifications"] = ctk.BooleanVar(
            value=settings.get("os_notifications", False))
        row = _row(self, t("setting_os_notif"))
        ctk.CTkSwitch(row, text="", variable=self._vars["os_notifications"],
                      command=self._save_all, width=44).pack(side="left")

        self._vars["crash_screenshots"] = ctk.BooleanVar(
            value=settings.get("crash_screenshots", True))
        row = _row(self, t("setting_crash_screenshots"))
        ctk.CTkSwitch(row, text="", variable=self._vars["crash_screenshots"],
                      command=self._save_all, width=44).pack(side="left")

        self._vars["webhook_url"] = ctk.StringVar(
            value=settings.get("webhook_url", ""))
        row = _row(self, t("setting_webhook"))
        self._entry(row, "webhook_url", width=220)

    # ── Presets ────────────────────────────────────────────────────────

    def _build_presets(self) -> None:
        _section(self, t("section_presets"))

        # Preset name entry + Save button
        save_row = ctk.CTkFrame(self, fg_color="transparent")
        save_row.pack(fill="x", padx=14, pady=(4, 2))

        self._preset_name_var = ctk.StringVar()
        ctk.CTkEntry(save_row, textvariable=self._preset_name_var,
                     placeholder_text=t("placeholder_preset_name"), width=160,
                     height=28).pack(side="left", padx=(0, 6))
        ctk.CTkButton(save_row, text=t("btn_save_preset"),
                      width=160, height=28, corner_radius=6,
                      command=self._save_preset).pack(side="left")

        # Package textbox for preset editing
        self._preset_pkg_box = ctk.CTkTextbox(self, height=70,
                                               font=ctk.CTkFont(size=11, family="Consolas"),
                                               corner_radius=6)
        self._preset_pkg_box.pack(fill="x", padx=14, pady=(2, 4))

        # Saved presets list
        ctk.CTkLabel(self, text=t("label_saved_presets"), font=ctk.CTkFont(size=11),
                     text_color="#888", anchor="w").pack(fill="x", padx=14)

        self._preset_list_frame = ctk.CTkScrollableFrame(
            self, height=80, label_text="", fg_color="#1a1a2e", corner_radius=6)
        self._preset_list_frame.pack(fill="x", padx=14, pady=(2, 6))

        self._refresh_preset_list()

    # ── Reports ────────────────────────────────────────────────────────

    def _build_reports(self) -> None:
        _section(self, t("section_reports"))

        self._vars["report_retention_days"] = ctk.StringVar(
            value=str(settings.get("report_retention_days", 0)))
        row = _row(self, t("setting_retention"))
        self._entry(row, "report_retention_days", width=70)

    # ── System ─────────────────────────────────────────────────────────

    # ── Scheduled monitoring ───────────────────────────────────────────

    def _build_schedule(self) -> None:
        """UI for scheduling automated monitoring runs."""
        _section(self, t("section_schedule"))

        self._vars["schedule_enabled"] = ctk.BooleanVar(
            value=settings.get("schedule_enabled", False))
        row = _row(self, t("setting_schedule_enabled"))
        ctk.CTkSwitch(row, text="", variable=self._vars["schedule_enabled"],
                      command=self._save_all, width=44).pack(side="left")

        self._vars["schedule_time"] = ctk.StringVar(
            value=settings.get("schedule_time", "02:00"))
        row = _row(self, t("setting_schedule_time"))
        self._entry(row, "schedule_time", width=80)

        self._vars["schedule_duration_min"] = ctk.StringVar(
            value=str(settings.get("schedule_duration_min", 30)))
        row = _row(self, t("setting_schedule_duration"))
        self._entry(row, "schedule_duration_min", width=70)

        self._vars["schedule_repeat"] = ctk.StringVar(
            value=settings.get("schedule_repeat", "daily"))
        row = _row(self, t("setting_schedule_repeat"))
        ctk.CTkOptionMenu(
            row,
            variable=self._vars["schedule_repeat"],
            values=["daily", "once"],
            width=100, height=28,
            command=lambda _: self._save_all(),
        ).pack(side="left")

    # ── System ─────────────────────────────────────────────────────────

    def _build_system(self) -> None:
        _section(self, t("section_system"))

        self._vars["minimize_to_tray"] = ctk.BooleanVar(
            value=settings.get("minimize_to_tray", False))
        row = _row(self, t("setting_tray"))
        ctk.CTkSwitch(row, text="", variable=self._vars["minimize_to_tray"],
                      command=self._save_all, width=44).pack(side="left")

        self._vars["log_level"] = ctk.StringVar(
            value=settings.get("log_level", "INFO"))
        row = _row(self, t("setting_log_level"))
        ctk.CTkOptionMenu(row, variable=self._vars["log_level"],
                          values=["DEBUG", "INFO", "WARNING", "ERROR"],
                          width=110, height=28,
                          command=lambda _: self._save_all()).pack(side="left")

        self._vars["language"] = ctk.StringVar(
            value=settings.get("language", "en"))
        row = _row(self, "Language / Dil")
        ctk.CTkOptionMenu(
            row,
            variable=self._vars["language"],
            values=["en", "tr"],
            width=80, height=28,
            command=self._on_language_change,
        ).pack(side="left")
        ctk.CTkLabel(
            row,
            text=t("label_restart_to_apply"),
            font=ctk.CTkFont(size=10),
            text_color="#888888",
        ).pack(side="left", padx=(6, 0))

        # Save button
        ctk.CTkButton(self, text=t("btn_save_settings"), height=34,
                      corner_radius=8, fg_color="#1e5799",
                      hover_color="#154e7a",
                      command=self._save_all).pack(padx=14, pady=12, fill="x")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _entry(self, row: ctk.CTkFrame, key: str, width: int = 120) -> None:
        """Bind a CTkEntry to a StringVar and auto-save on focus-out."""
        e = ctk.CTkEntry(row, textvariable=self._vars[key], width=width, height=28)
        e.pack(side="left")
        e.bind("<FocusOut>", lambda _: self._save_all())
        e.bind("<Return>",   lambda _: self._save_all())

    # ------------------------------------------------------------------
    # Preset management
    # ------------------------------------------------------------------

    def _save_preset(self) -> None:
        """Save the current package textbox contents as a named preset."""
        from tkinter import messagebox as _mb
        name = self._preset_name_var.get().strip()
        if not name:
            logger.warning("Preset name is empty — skipping save.")
            return
        raw = self._preset_pkg_box.get("0.0", "end").strip()
        pkgs = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        if not pkgs:
            logger.warning("No packages entered — preset not saved.")
            return

        presets: List[dict] = settings.get("presets", [])
        # Confirm before overwriting an existing preset with the same name.
        existing = any(p.get("name") == name for p in presets)
        if existing:
            confirmed = _mb.askyesno(
                title=t("dlg_overwrite_preset"),
                message=t("dlg_overwrite_preset_msg", name=name),
            )
            if not confirmed:
                logger.debug("Preset overwrite cancelled by user for '%s'.", name)
                return
        presets = [p for p in presets if p.get("name") != name]
        presets.append({"name": name, "packages": pkgs})
        settings.set("presets", presets)
        self._refresh_preset_list()
        if self._on_preset_saved:
            self._on_preset_saved()
        logger.info("Preset '%s' saved (%d packages).", name, len(pkgs))

    def _load_preset(self, preset: dict) -> None:
        """Load a preset into the textbox and call the parent callback."""
        pkgs = preset.get("packages", [])
        self._preset_pkg_box.delete("0.0", "end")
        self._preset_pkg_box.insert("0.0", "\n".join(pkgs))
        if self._on_preset_load:
            self._on_preset_load(pkgs)
        logger.info("Preset '%s' loaded.", preset.get("name"))

    def _delete_preset(self, name: str) -> None:
        """Remove a preset by name."""
        presets = [p for p in settings.get("presets", []) if p.get("name") != name]
        settings.set("presets", presets)
        self._refresh_preset_list()
        logger.info("Preset '%s' deleted.", name)

    def _refresh_preset_list(self) -> None:
        """Redraw the saved presets list."""
        for w in self._preset_list_frame.winfo_children():
            w.destroy()
        presets: List[dict] = settings.get("presets", [])
        if not presets:
            ctk.CTkLabel(self._preset_list_frame, text=t("label_no_presets"),
                         text_color="#555", font=ctk.CTkFont(size=11)).pack(padx=6, pady=6)
            return
        for preset in presets:
            name = preset.get("name", "?")
            pkg_count = len(preset.get("packages", []))
            row = ctk.CTkFrame(self._preset_list_frame, fg_color="transparent")
            row.pack(fill="x", padx=4, pady=2)
            ctk.CTkLabel(row, text=f"{name}  ({pkg_count} {t('label_pkgs')})",
                         font=ctk.CTkFont(size=11), anchor="w").pack(
                side="left", fill="x", expand=True)
            ctk.CTkButton(row, text=t("btn_load"), width=50, height=22,
                          corner_radius=4,
                          command=lambda p=preset: self._load_preset(p)).pack(side="right", padx=2)
            ctk.CTkButton(row, text="✕", width=28, height=22,
                          corner_radius=4, fg_color="#5d1f1a", hover_color="#922b21",
                          command=lambda n=name: self._delete_preset(n)).pack(side="right")

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def _on_language_change(self, lang: str) -> None:
        """Persist new language selection and notify user."""
        from droidperf.i18n import set_language
        set_language(lang)
        self._save_all()
        logger.info("Language set to '%s' (restart required for full effect).", lang)

    def _save_all(self) -> None:
        """Persist all widget values to settings.json."""
        int_keys = {"default_interval", "alert_ram_kb", "alert_cpu_pct",
                    "alert_temp_c", "alert_batt_drop", "report_retention_days",
                    "schedule_duration_min"}
        float_keys = {"spike_std_multiplier"}
        bool_keys = {"os_notifications", "minimize_to_tray", "schedule_enabled", "crash_screenshots"}

        for key, var in self._vars.items():
            raw = var.get()
            if key in int_keys:
                try:
                    settings.set(key, int(raw))
                except ValueError:
                    pass
            elif key in float_keys:
                try:
                    settings.set(key, float(raw))
                except ValueError:
                    pass
            elif key in bool_keys:
                settings.set(key, bool(raw))
            else:
                settings.set(key, raw)
        logger.debug("Settings saved from SettingsPanel.")
        if self._on_settings_saved:
            self._on_settings_saved()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_presets(self) -> List[dict]:
        """Return all saved presets as a list of dicts."""
        return settings.get("presets", [])

    def load_preset(self, name: str) -> List[str]:
        """
        Return the package list for the named preset.

        Args:
            name (str): Preset name.

        Returns:
            List[str]: Package names, or empty list if not found.
        """
        for p in settings.get("presets", []):
            if p.get("name") == name:
                return p.get("packages", [])
        return []
