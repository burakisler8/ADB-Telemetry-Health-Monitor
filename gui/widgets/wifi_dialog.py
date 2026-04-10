"""
gui/widgets/wifi_dialog.py
--------------------------
Modal dialog for connecting to Android devices over Wi-Fi ADB.

Usage:
    WifiAdbDialog(master, on_connected=<callable>)

The dialog lets the user:
  - Type or select from history a "host:port" address.
  - Click Connect  → runs `adb connect <host:port>`.
  - Click Disconnect → runs `adb disconnect <host:port>`.
  - Successful connections are saved to `settings.wifi_adb_history`
    (capped at 10 entries) and shown in the dropdown.

``on_connected`` is called (with no arguments) after a successful
connect so the parent can immediately refresh its device list.
"""

import logging
from typing import Callable, List, Optional

import customtkinter as ctk

from droidperf import adb_manager
from droidperf.i18n import t
from droidperf.settings_manager import settings

logger = logging.getLogger(__name__)

_HISTORY_MAX = 10
_WIN_W, _WIN_H = 420, 290


class WifiAdbDialog(ctk.CTkToplevel):
    """
    Modal top-level window for Wi-Fi ADB connection management.

    Args:
        master: Parent widget (root App window or ControlPanel).
        on_connected (Callable[[], None] | None):
            Invoked after a successful ``adb connect`` so the caller can
            refresh its device list.
    """

    def __init__(
        self,
        master: ctk.CTk,
        on_connected: Optional[Callable[[], None]] = None,
    ) -> None:
        super().__init__(master)

        self._on_connected = on_connected

        self.title(t("wifi_title"))
        self.resizable(False, False)
        self.grab_set()  # modal

        # Centre over the parent window.
        self.after(10, self._centre)

        self._build_ui()
        logger.debug("WifiAdbDialog opened.")

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        """Lay out all widgets inside the dialog."""
        pad = dict(padx=20, pady=6)

        # ── Title ──────────────────────────────────────────────────────
        ctk.CTkLabel(
            self,
            text=t("wifi_connect_title"),
            font=ctk.CTkFont(size=15, weight="bold"),
            text_color="#e0e0e0",
        ).pack(anchor="w", padx=20, pady=(16, 4))

        ctk.CTkFrame(self, height=1, fg_color="#2d2d2d").pack(
            fill="x", padx=20, pady=(0, 10)
        )

        # ── Address row (combobox + history) ───────────────────────────
        ctk.CTkLabel(
            self,
            text=t("wifi_address_label"),
            font=ctk.CTkFont(size=11),
            text_color="#888888",
            anchor="w",
        ).pack(fill="x", padx=20, pady=(0, 2))

        history: List[str] = settings.get("wifi_adb_history", [])

        self._addr_var = ctk.StringVar(value=history[0] if history else "")

        self._addr_combo = ctk.CTkComboBox(
            self,
            variable=self._addr_var,
            values=history if history else [""],
            width=380,
            height=34,
            font=ctk.CTkFont(size=13, family="Consolas"),
            corner_radius=6,
        )
        self._addr_combo.pack(**pad)

        # ── Hint label ─────────────────────────────────────────────────
        ctk.CTkLabel(
            self,
            text=t("wifi_hint"),
            font=ctk.CTkFont(size=10),
            text_color="#555577",
            anchor="w",
        ).pack(fill="x", padx=20, pady=(0, 8))

        # ── Action buttons row ─────────────────────────────────────────
        btn_row = ctk.CTkFrame(self, fg_color="transparent")
        btn_row.pack(fill="x", padx=20, pady=4)

        ctk.CTkButton(
            btn_row,
            text=t("wifi_btn_connect"),
            width=120,
            height=34,
            corner_radius=7,
            fg_color="#1e8449",
            hover_color="#145a32",
            font=ctk.CTkFont(size=13, weight="bold"),
            command=self._handle_connect,
        ).pack(side="left", padx=(0, 8))

        ctk.CTkButton(
            btn_row,
            text=t("wifi_btn_disconnect"),
            width=120,
            height=34,
            corner_radius=7,
            fg_color="#7d3c00",
            hover_color="#5d2d00",
            font=ctk.CTkFont(size=13),
            command=self._handle_disconnect,
        ).pack(side="left")

        ctk.CTkButton(
            btn_row,
            text=t("btn_close"),
            width=80,
            height=34,
            corner_radius=7,
            fg_color="#2d2d2d",
            hover_color="#3d3d3d",
            font=ctk.CTkFont(size=12),
            command=self.destroy,
        ).pack(side="right")

        # ── Status label ───────────────────────────────────────────────
        ctk.CTkFrame(self, height=1, fg_color="#2d2d2d").pack(
            fill="x", padx=20, pady=(10, 0)
        )

        self._status_lbl = ctk.CTkLabel(
            self,
            text="",
            font=ctk.CTkFont(size=11),
            text_color="#888888",
            wraplength=380,
            anchor="w",
            justify="left",
        )
        self._status_lbl.pack(fill="x", padx=20, pady=(6, 14))

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _handle_connect(self) -> None:
        """Run `adb connect` and handle the result."""
        host_port = self._addr_var.get().strip()
        if not host_port:
            self._set_status(t("wifi_msg_enter_address"), "#e74c3c")
            return

        self._set_status(t("wifi_msg_connecting"), "#888888")
        self.update_idletasks()

        success, message = adb_manager.connect_wifi(host_port)

        if success:
            self._save_to_history(host_port)
            self._set_status(f"✓  {message}", "#27ae60")
            if self._on_connected:
                self._on_connected()
        else:
            self._set_status(f"✗  {message}", "#e74c3c")

    def _handle_disconnect(self) -> None:
        """Run `adb disconnect` and handle the result."""
        host_port = self._addr_var.get().strip()
        if not host_port:
            self._set_status(t("wifi_msg_enter_address"), "#e74c3c")
            return

        self._set_status(t("wifi_msg_disconnecting"), "#888888")
        self.update_idletasks()

        success, message = adb_manager.disconnect_wifi(host_port)

        if success:
            self._set_status(f"✓  {message}", "#e67e22")
            if self._on_connected:
                self._on_connected()
        else:
            self._set_status(f"✗  {message}", "#e74c3c")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _set_status(self, text: str, color: str) -> None:
        """Update the status label text and colour."""
        self._status_lbl.configure(text=text, text_color=color)

    def _save_to_history(self, host_port: str) -> None:
        """
        Prepend *host_port* to the persistent history list (max 10 entries).

        Args:
            host_port (str): Successfully connected address.
        """
        history: List[str] = settings.get("wifi_adb_history", [])
        # Remove duplicates, prepend, cap length.
        history = [h for h in history if h != host_port]
        history.insert(0, host_port)
        history = history[:_HISTORY_MAX]
        settings.set("wifi_adb_history", history)
        # Refresh combobox values.
        self._addr_combo.configure(values=history)
        logger.debug("Wi-Fi history updated: %s", history)

    def _centre(self) -> None:
        """Centre the dialog over its parent after Tk has calculated sizes."""
        try:
            pw = self.master.winfo_width()
            ph = self.master.winfo_height()
            px = self.master.winfo_rootx()
            py = self.master.winfo_rooty()
            x = px + (pw - _WIN_W) // 2
            y = py + (ph - _WIN_H) // 2
            self.geometry(f"{_WIN_W}x{_WIN_H}+{x}+{y}")
        except Exception:  # pylint: disable=broad-except
            self.geometry(f"{_WIN_W}x{_WIN_H}")
