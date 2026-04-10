"""
gui/widgets/screenshots_panel.py
---------------------------------
Crash Screenshots panel — displayed in the "Crashes" tab.

Scans the configured output directory for PNG files whose names start with
``crash_`` (produced by ``LogcatWatcher._capture_screenshot``).  Each
screenshot is shown as a thumbnail row with the timestamp, a full-size
viewer button, and a delete button.

Public API:
    ScreenshotsPanel(master, **kwargs)
    ScreenshotsPanel.refresh()
"""

import logging
import os
import subprocess
import sys
from pathlib import Path
from tkinter import messagebox
from typing import List

import customtkinter as ctk

from droidperf.i18n import t
from droidperf.settings_manager import settings

logger = logging.getLogger(__name__)

_PAD = dict(padx=14, pady=(4, 2))
_HEAD_PAD = dict(padx=14, pady=(12, 2))

# Maximum number of screenshot rows rendered at once.
# Rendering thousands of CTk widgets in a single pass freezes the UI.
_MAX_DISPLAY = 100


def _open_file(path: Path) -> None:
    """Open *path* with the OS default application."""
    try:
        if sys.platform == "win32":
            os.startfile(str(path))
        elif sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=False)
        else:
            subprocess.run(["xdg-open", str(path)], check=False)
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("Could not open file '%s': %s", path, exc)


class ScreenshotsPanel(ctk.CTkScrollableFrame):
    """
    Scrollable panel that lists crash screenshots taken during monitoring.

    Screenshots are PNG files whose names match ``crash_*.png`` inside the
    configured ``output_dir``.  Each row shows the filename (timestamp),
    an Open button, and a Delete button.

    Args:
        master: Parent widget (Crashes tab frame).
        **kwargs: Forwarded to ``ctk.CTkScrollableFrame.__init__``.
    """

    def __init__(self, master, **kwargs) -> None:
        kwargs.setdefault("label_text", "")
        kwargs.setdefault("fg_color", "transparent")
        super().__init__(master, **kwargs)

        self._build_toolbar()
        self._content_frame = ctk.CTkFrame(self, fg_color="transparent")
        self._content_frame.pack(fill="both", expand=True)

        self.refresh()
        logger.debug("ScreenshotsPanel initialised.")

    # ------------------------------------------------------------------
    # Toolbar
    # ------------------------------------------------------------------

    def _build_toolbar(self) -> None:
        """Top bar with title and Refresh button."""
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(fill="x", padx=14, pady=(10, 4))

        ctk.CTkLabel(
            bar,
            text=t("label_crash_screenshots"),
            font=ctk.CTkFont(size=14, weight="bold"),
            anchor="w",
        ).pack(side="left")

        ctk.CTkButton(
            bar,
            text=t("btn_refresh"),
            width=90,
            height=28,
            corner_radius=6,
            fg_color="#2980b9",
            hover_color="#1a6a9a",
            command=self.refresh,
        ).pack(side="right")

        ctk.CTkButton(
            bar,
            text=t("btn_clear_all"),
            width=90,
            height=28,
            corner_radius=6,
            fg_color="#5d1f1a",
            hover_color="#922b21",
            command=self._delete_all,
        ).pack(side="right", padx=(0, 6))

        ctk.CTkLabel(
            bar,
            text=t("label_crash_auto_capture"),
            font=ctk.CTkFont(size=10),
            text_color="#7f8c8d",
            anchor="w",
        ).pack(side="left", padx=(12, 0))

        # Divider
        ctk.CTkFrame(self, height=1, fg_color="#2d2d2d").pack(
            fill="x", padx=14, pady=(0, 6)
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        """Rescan the output directory and redraw the screenshot list."""
        for widget in self._content_frame.winfo_children():
            widget.destroy()

        screenshots = self._find_screenshots()

        if not screenshots:
            ctk.CTkLabel(
                self._content_frame,
                text=t("label_no_screenshots"),
                font=ctk.CTkFont(size=12),
                text_color="#555577",
                justify="center",
            ).pack(pady=40)
            return

        total = len(screenshots)
        display = screenshots[:_MAX_DISPLAY]

        count_text = (
            t("label_screenshots_count", count=total)
            if total <= _MAX_DISPLAY
            else t("label_screenshots_showing", shown=_MAX_DISPLAY, total=total)
        )
        ctk.CTkLabel(
            self._content_frame,
            text=count_text,
            font=ctk.CTkFont(size=11),
            text_color="#888888",
            anchor="w",
        ).pack(fill="x", padx=14, pady=(0, 6))

        for path in display:
            self._build_screenshot_row(path)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _find_screenshots(self) -> List[Path]:
        """
        Return crash PNG files sorted newest-first.

        Searches the ``output_dir`` from settings (defaults to ``reports``).
        """
        output_dir = Path(settings.get("output_dir", "reports"))
        if not output_dir.exists():
            return []
        pngs = sorted(
            output_dir.glob("crash_*.png"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return pngs

    def _build_screenshot_row(self, path: Path) -> None:
        """
        Render one row for a crash screenshot file.

        Args:
            path (Path): Absolute or relative path to the PNG file.
        """
        row = ctk.CTkFrame(
            self._content_frame,
            corner_radius=8,
            fg_color="#1a1a2e",
        )
        row.pack(fill="x", padx=14, pady=3)

        # Icon + filename
        info_frame = ctk.CTkFrame(row, fg_color="transparent")
        info_frame.pack(side="left", fill="x", expand=True, padx=10, pady=8)

        ctk.CTkLabel(
            info_frame,
            text="📸",
            font=ctk.CTkFont(size=18),
            width=28,
        ).pack(side="left")

        # Parse human-readable timestamp from filename: crash_2024-01-15T10-30-00.png
        display_name = path.stem.replace("crash_", "").replace("-", ":", 2)
        file_size_kb = path.stat().st_size // 1024

        text_frame = ctk.CTkFrame(info_frame, fg_color="transparent")
        text_frame.pack(side="left", padx=(8, 0))

        ctk.CTkLabel(
            text_frame,
            text=display_name,
            font=ctk.CTkFont(size=12, weight="bold"),
            anchor="w",
        ).pack(anchor="w")

        ctk.CTkLabel(
            text_frame,
            text=f"{path.name}  ·  {file_size_kb} KB",
            font=ctk.CTkFont(size=10),
            text_color="#7f8c8d",
            anchor="w",
        ).pack(anchor="w")

        # Buttons
        btn_frame = ctk.CTkFrame(row, fg_color="transparent")
        btn_frame.pack(side="right", padx=10, pady=8)

        ctk.CTkButton(
            btn_frame,
            text=t("btn_open"),
            width=60,
            height=26,
            corner_radius=6,
            fg_color="#1e5799",
            hover_color="#154e7a",
            command=lambda p=path: _open_file(p),
        ).pack(side="left", padx=(0, 4))

        ctk.CTkButton(
            btn_frame,
            text="✕",
            width=32,
            height=26,
            corner_radius=6,
            fg_color="#5d1f1a",
            hover_color="#922b21",
            command=lambda p=path: self._delete_screenshot(p),
        ).pack(side="left")

    def _delete_all(self) -> None:
        """Delete all crash screenshots after user confirmation."""
        screenshots = self._find_screenshots()
        if not screenshots:
            return
        confirmed = messagebox.askyesno(
            title=t("dlg_clear_screenshots"),
            message=t("dlg_clear_screenshots_msg", count=len(screenshots)),
        )
        if not confirmed:
            return
        failed = 0
        for path in screenshots:
            try:
                path.unlink()
            except OSError as exc:
                logger.error("Could not delete '%s': %s", path.name, exc)
                failed += 1
        deleted = len(screenshots) - failed
        logger.info("Deleted %d crash screenshot(s).", deleted)
        if failed:
            messagebox.showwarning(
                title=t("dlg_partial_delete"),
                message=t("dlg_partial_delete_msg", deleted=deleted, failed=failed),
            )
        self.refresh()

    def _delete_screenshot(self, path: Path) -> None:
        """
        Delete a crash screenshot after user confirmation.

        Args:
            path (Path): Path to the PNG file to remove.
        """
        confirmed = messagebox.askyesno(
            title=t("dlg_delete_screenshot"),
            message=t("dlg_delete_screenshot_msg", name=path.name),
        )
        if not confirmed:
            return
        try:
            path.unlink()
            logger.info("Crash screenshot deleted: '%s'.", path.name)
        except OSError as exc:
            logger.error("Could not delete '%s': %s", path.name, exc)
            messagebox.showerror(
                title=t("dlg_delete_error"),
                message=f"Could not delete file:\n{exc}",
            )
        self.refresh()
