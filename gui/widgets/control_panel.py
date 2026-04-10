"""
gui/widgets/control_panel.py
-----------------------------
Sidebar control panel for the ADB Telemetry & Health Monitor GUI.

Redesigned for Phase 2: compact device list, always-visible Start/Stop
buttons, and a cleaner visual hierarchy.  Multiple devices may be selected
via checkboxes; one MonitorEngine is spawned per selected device in App.

Public API:
    ControlPanel(master, on_start, on_stop, on_refresh_devices, **kwargs)
"""

import logging
from typing import Callable, Dict, List, Optional

import customtkinter as ctk

from droidperf.i18n import t
from droidperf.settings_manager import settings as _app_settings
from gui.widgets.wifi_dialog import WifiAdbDialog

logger = logging.getLogger(__name__)

_PAD_X = 14


def _section_label(parent: ctk.CTkFrame, text: str) -> None:
    """Render a compact uppercase section-header label."""
    ctk.CTkLabel(
        parent,
        text=text,
        font=ctk.CTkFont(size=10, weight="bold"),
        text_color="#7f8c8d",
        anchor="w",
    ).pack(fill="x", padx=_PAD_X, pady=(6, 2))


def _divider(parent: ctk.CTkFrame) -> None:
    """Render a 1-px horizontal divider."""
    ctk.CTkFrame(parent, height=1, fg_color="#2d2d2d").pack(
        fill="x", padx=_PAD_X, pady=(6, 0)
    )


class ControlPanel(ctk.CTkFrame):
    """
    Left-sidebar widget that exposes all monitoring controls.

    Layout (top → bottom):
      ┌── DEVICES ──────────── [Refresh] ──┐
      │  ☑ serial-A   ☑ serial-B           │  compact scrollable, max 72 px
      ├─────────────────────────────────────┤
      │  MODE   ○ Auto-Discover             │
      │         ○ Custom Packages           │
      │         [textbox — only in custom]  │
      ├─────────────────────────────────────┤
      │  INTERVAL   [____5____] s           │
      ├─────────────────────────────────────┤
      │  [ ▶  Start Monitoring           ]  │  green
      │  [ ■  Stop Monitoring            ]  │  red
      ├─────────────────────────────────────┤
      │  ● Ready                            │  status dot + text
      └─────────────────────────────────────┘

    Args:
        master: Parent widget (root ``App`` window).
        on_start (Callable[[dict], None]):
            Invoked with ``get_settings()`` when Start is clicked.
        on_stop (Callable[[], None]):
            Invoked when Stop is clicked.
        on_refresh_devices (Callable[[], None]):
            Invoked when Refresh is clicked.
        **kwargs: Forwarded to ``ctk.CTkFrame.__init__``.
    """

    def __init__(
        self,
        master: ctk.CTk,
        on_start: Callable[[dict], None],
        on_stop: Callable[[], None],
        on_refresh_devices: Callable[[], None],
        on_preset_load: Optional[Callable[[List[str]], None]] = None,
        **kwargs,
    ) -> None:
        super().__init__(master, **kwargs)

        self._on_start = on_start
        self._on_stop = on_stop
        self._on_refresh_devices = on_refresh_devices
        self._on_preset_load = on_preset_load

        # Maps device serial → BooleanVar (checkbox state).
        self._device_vars: Dict[str, ctk.BooleanVar] = {}

        self._build_ui()
        logger.debug("ControlPanel initialised.")

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        """Construct and lay out all child widgets top to bottom."""
        self._build_devices_section()
        _divider(self)
        self._build_mode_section()
        _divider(self)
        self._build_preset_bar()
        _divider(self)
        self._build_interval_section()
        _divider(self)
        self._build_action_buttons()
        self._build_status_bar()

    # ── Devices ────────────────────────────────────────────────────────

    def _build_devices_section(self) -> None:
        """Compact device-list with an inline Refresh button."""
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=_PAD_X, pady=(8, 2))

        ctk.CTkLabel(
            header,
            text=t("label_devices"),
            font=ctk.CTkFont(size=10, weight="bold"),
            text_color="#7f8c8d",
            anchor="w",
        ).pack(side="left")

        ctk.CTkButton(
            header,
            text=t("btn_refresh"),
            width=76,
            height=24,
            font=ctk.CTkFont(size=11),
            corner_radius=6,
            fg_color="#2980b9",
            hover_color="#1a6a9a",
            command=self._on_refresh_devices,
        ).pack(side="right", padx=(4, 0))

        ctk.CTkButton(
            header,
            text=t("btn_wifi"),
            width=72,
            height=24,
            font=ctk.CTkFont(size=11),
            corner_radius=6,
            fg_color="#6c3483",
            hover_color="#4a235a",
            command=self._open_wifi_dialog,
        ).pack(side="right")

        # Outer container enforces the visible height; CTkScrollableFrame
        # does not reliably honour its own height= due to internal scrollbar
        # minimums, so pack_propagate(False) on the wrapper does the job.
        _device_container = ctk.CTkFrame(
            self,
            height=52,
            fg_color="#1a1a2e",
            corner_radius=6,
        )
        _device_container.pack(fill="x", padx=_PAD_X, pady=(0, 4))
        _device_container.pack_propagate(False)

        self._device_list_frame = ctk.CTkScrollableFrame(
            _device_container,
            fg_color="transparent",
            corner_radius=0,
            label_text="",
            scrollbar_button_color="#2d2d4e",
            scrollbar_button_hover_color="#3d3d6e",
        )
        self._device_list_frame.pack(fill="both", expand=True)

        self._show_device_placeholder()

    def _show_device_placeholder(self) -> None:
        """Show a muted hint when no devices have been discovered yet."""
        ctk.CTkLabel(
            self._device_list_frame,
            text=t("label_no_devices"),
            text_color="#555577",
            font=ctk.CTkFont(size=11),
            anchor="w",
        ).pack(anchor="w", padx=6, pady=8)

    # ── Monitoring mode ────────────────────────────────────────────────

    def _build_mode_section(self) -> None:
        """Radio-button pair for Auto-Discover vs Custom Packages."""
        _section_label(self, t("label_mode"))

        self._mode_var = ctk.StringVar(value="auto")

        radio_frame = ctk.CTkFrame(self, fg_color="transparent")
        radio_frame.pack(fill="x", padx=_PAD_X, pady=(0, 4))

        ctk.CTkRadioButton(
            radio_frame,
            text=t("label_auto_discover"),
            variable=self._mode_var,
            value="auto",
            font=ctk.CTkFont(size=12),
            command=self._on_mode_change,
        ).pack(anchor="w", pady=2)

        self._custom_radio = ctk.CTkRadioButton(
            radio_frame,
            text=t("label_custom_packages"),
            variable=self._mode_var,
            value="custom",
            font=ctk.CTkFont(size=12),
            command=self._on_mode_change,
        )
        self._custom_radio.pack(anchor="w", pady=2)

        # Package textbox — hidden until Custom mode is selected.
        self._package_textbox = ctk.CTkTextbox(
            self,
            height=90,
            font=ctk.CTkFont(size=11, family="Consolas"),
            corner_radius=6,
            border_width=1,
            border_color="#2d2d4e",
        )
        self._package_textbox.insert("0.0", "com.example.app\ncom.another.app")
        self._package_textbox_visible = False

    # ── Preset quick-load ──────────────────────────────────────────────

    def _build_preset_bar(self) -> None:
        """Compact row: preset dropdown + Load button for quick access."""
        _section_label(self, t("label_quick_preset"))

        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", padx=_PAD_X, pady=(0, 4))

        self._preset_var = ctk.StringVar(value=t("placeholder_select_preset"))
        self._preset_menu = ctk.CTkOptionMenu(
            row,
            variable=self._preset_var,
            values=self._preset_names(),
            height=28,
            corner_radius=6,
            dynamic_resizing=False,
        )
        self._preset_menu.pack(side="left", fill="x", expand=True, padx=(0, 6))

        ctk.CTkButton(
            row,
            text=t("btn_load"),
            width=52,
            height=28,
            corner_radius=6,
            fg_color="#7d3c98",
            hover_color="#6c3483",
            command=self._load_selected_preset,
        ).pack(side="right")

    def _preset_names(self) -> List[str]:
        """Return current preset names from settings, with a placeholder."""
        presets = _app_settings.get("presets", [])
        names = [p.get("name", "") for p in presets if p.get("name")]
        return names if names else [t("placeholder_no_presets")]

    def _load_selected_preset(self) -> None:
        """
        Load the selected preset's packages into the custom package textbox
        and switch to custom mode.
        """
        name = self._preset_var.get()
        presets = _app_settings.get("presets", [])
        match = next((p for p in presets if p.get("name") == name), None)
        if not match:
            logger.warning("Preset '%s' not found.", name)
            return
        packages: List[str] = match.get("packages", [])
        if not packages:
            return

        self._mode_var.set("custom")
        self._on_mode_change()
        self._package_textbox.delete("0.0", "end")
        self._package_textbox.insert("0.0", "\n".join(packages))

        if self._on_preset_load:
            self._on_preset_load(packages)
        logger.info("Quick-loaded preset '%s' (%d packages).", name, len(packages))

    def refresh_presets(self) -> None:
        """
        Rebuild the preset dropdown with the latest saved presets.

        Call this after saving a new preset in SettingsPanel.
        """
        names = self._preset_names()
        self._preset_menu.configure(values=names)
        if names and names[0] != t("placeholder_no_presets"):
            self._preset_var.set(names[0])
        logger.debug("Preset dropdown refreshed: %s", names)

    # ── Interval ───────────────────────────────────────────────────────

    def _build_interval_section(self) -> None:
        """Compact interval row: label + entry + unit."""
        _section_label(self, t("label_interval"))

        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", padx=_PAD_X, pady=(0, 6))

        saved_interval = str(_app_settings.get("default_interval", 5))
        self._interval_var = ctk.StringVar(value=saved_interval)
        self._interval_save_job: Optional[str] = None
        self._interval_var.trace_add("write", lambda *_: self._debounce_save_interval())

        _interval_entry = ctk.CTkEntry(
            row,
            textvariable=self._interval_var,
            width=70,
            height=30,
            font=ctk.CTkFont(size=13),
            corner_radius=6,
            justify="center",
        )
        _interval_entry.pack(side="left")
        _interval_entry.bind("<FocusOut>", lambda _: self._save_interval())
        _interval_entry.bind("<Return>",   lambda _: self._save_interval())

        ctk.CTkLabel(
            row,
            text=t("label_interval_unit"),
            text_color="#888888",
            font=ctk.CTkFont(size=12),
        ).pack(side="left", padx=(8, 0))

    def _debounce_save_interval(self) -> None:
        """Cancel any pending save and schedule a new one 500 ms later."""
        if self._interval_save_job:
            self.after_cancel(self._interval_save_job)
        self._interval_save_job = self.after(500, self._save_interval)

    def _save_interval(self) -> None:
        """Persist the current interval value to settings."""
        self._interval_save_job = None
        try:
            value = max(1, int(float(self._interval_var.get())))
            _app_settings.set("default_interval", value)
            # Only call set() when the string actually changes to avoid
            # re-triggering the write trace and causing an infinite loop.
            normalized = str(value)
            if self._interval_var.get() != normalized:
                self._interval_var.set(normalized)
            logger.debug("Poll interval saved: %d s.", value)
        except ValueError:
            fallback = str(_app_settings.get("default_interval", 5))
            if self._interval_var.get() != fallback:
                self._interval_var.set(fallback)

    # ── Action buttons ─────────────────────────────────────────────────

    def _build_action_buttons(self) -> None:
        """Start and Stop buttons — always fully visible in the sidebar."""
        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.pack(fill="x", padx=_PAD_X, pady=(6, 4))

        self._start_btn = ctk.CTkButton(
            btn_frame,
            text=t("btn_start"),
            height=38,
            font=ctk.CTkFont(size=13, weight="bold"),
            corner_radius=8,
            fg_color="#1e8449",
            hover_color="#145a32",
            command=self._handle_start,
        )
        self._start_btn.pack(fill="x", pady=(0, 6))

        self._stop_btn = ctk.CTkButton(
            btn_frame,
            text=t("btn_stop"),
            height=38,
            font=ctk.CTkFont(size=13, weight="bold"),
            corner_radius=8,
            fg_color="#922b21",
            hover_color="#641e16",
            state="disabled",
            command=self._on_stop,
        )
        self._stop_btn.pack(fill="x")

    # ── Status bar ─────────────────────────────────────────────────────

    def _build_status_bar(self) -> None:
        """Coloured dot + text status indicator at the bottom."""
        _divider(self)

        status_frame = ctk.CTkFrame(self, fg_color="transparent")
        status_frame.pack(fill="x", padx=_PAD_X, pady=(6, 8))

        self._status_dot = ctk.CTkLabel(
            status_frame,
            text="●",
            font=ctk.CTkFont(size=14),
            text_color="#27ae60",
            width=20,
        )
        self._status_dot.pack(side="left")

        self.status_label = ctk.CTkLabel(
            status_frame,
            text=t("label_ready"),
            text_color="#e0e0e0",
            font=ctk.CTkFont(size=12),
            wraplength=180,
            justify="left",
            anchor="w",
        )
        self.status_label.pack(side="left", padx=(4, 0), fill="x", expand=True)

    # ------------------------------------------------------------------
    # Internal event handlers
    # ------------------------------------------------------------------

    def _open_wifi_dialog(self) -> None:
        """Open the Wi-Fi ADB connection dialog (modal)."""
        WifiAdbDialog(self.winfo_toplevel(), on_connected=self._on_refresh_devices)

    def _on_mode_change(self) -> None:
        """Show or hide the package textbox based on selected mode."""
        if self._mode_var.get() == "custom":
            if not self._package_textbox_visible:
                self._package_textbox.pack(
                    fill="x", padx=_PAD_X, pady=(2, 6),
                    after=self._custom_radio,
                )
                self._package_textbox_visible = True
        else:
            if self._package_textbox_visible:
                self._package_textbox.pack_forget()
                self._package_textbox_visible = False

    def _handle_start(self) -> None:
        """Collect settings and invoke the on_start callback."""
        settings = self.get_settings()
        logger.debug("Start requested: %s", settings)
        self._on_start(settings)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_settings(self) -> dict:
        """
        Return current control-panel values as a settings dictionary.

        Returns:
            dict with keys:
                - ``device_ids``  (List[str]): Checked device serials.
                - ``mode``        (str):        ``"auto"`` or ``"custom"``.
                - ``packages``    (List[str]):  Package list (custom mode).
                - ``interval``    (float):      Polling interval in seconds.
        """
        device_ids = [
            serial for serial, var in self._device_vars.items() if var.get()
        ]

        mode = self._mode_var.get()
        packages: List[str] = []
        if mode == "custom":
            raw = self._package_textbox.get("0.0", "end").strip()
            placeholder_lines = {"com.example.app", "com.another.app"}
            packages = [
                ln.strip()
                for ln in raw.splitlines()
                if ln.strip() and ln.strip() not in placeholder_lines
            ]

        try:
            interval = max(1.0, float(self._interval_var.get()))
        except ValueError:
            interval = 5.0
            logger.warning("Invalid interval; defaulting to 5.0 s.")

        return {
            "device_ids": device_ids,
            "mode": mode,
            "packages": packages,
            "interval": interval,
        }

    def set_running(self, running: bool) -> None:
        """
        Toggle Start / Stop button states.

        Args:
            running (bool): ``True`` → monitoring active; ``False`` → idle.
        """
        if running:
            self._start_btn.configure(state="disabled", fg_color="#145a32")
            self._stop_btn.configure(state="normal", fg_color="#922b21")
        else:
            self._start_btn.configure(state="normal", fg_color="#1e8449")
            self._stop_btn.configure(state="disabled", fg_color="#5d1f1a")

    def set_status(self, text: str, color: str = "#e0e0e0") -> None:
        """
        Update the status indicator dot and text.

        Args:
            text (str):  Status message.
            color (str): Hex colour applied to both the dot and the text.
        """
        self.status_label.configure(text=text, text_color=color)
        self._status_dot.configure(text_color=color)

    def populate_devices(self, devices: List[str]) -> None:
        """
        Rebuild the device checkbox list with discovered serial numbers.

        Previously checked devices retain their state if still present.
        New devices are checked by default when exactly one is found.

        Args:
            devices (List[str]): Serial numbers from
                                 ``adb_manager.get_connected_devices()``.
        """
        previously_checked = {
            s for s, v in self._device_vars.items() if v.get()
        }

        for widget in self._device_list_frame.winfo_children():
            widget.destroy()
        self._device_vars.clear()

        if not devices:
            self._show_device_placeholder()
            return

        for serial in devices:
            checked = serial in previously_checked or (
                len(devices) == 1 and not previously_checked
            )
            var = ctk.BooleanVar(value=checked)
            self._device_vars[serial] = var

            ctk.CTkCheckBox(
                self._device_list_frame,
                text=serial,
                variable=var,
                font=ctk.CTkFont(size=11),
                checkbox_width=16,
                checkbox_height=16,
                corner_radius=4,
            ).pack(anchor="w", padx=6, pady=3)

        logger.debug("Devices populated: %s", devices)
