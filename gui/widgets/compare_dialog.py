"""
gui/widgets/compare_dialog.py
------------------------------
Session comparison dialog for the ADB Telemetry & Health Monitor GUI.

Opens a modal-style top-level window that lets the user select two
telemetry CSV files (Session A and Session B), assign human-readable
labels, and generate a standalone HTML comparison report which is
immediately opened in the default web browser.

Public API:
    CompareDialog(master, reports_dir, prefill_csv=None)
"""

import logging
import webbrowser
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Optional

import customtkinter as ctk

from droidperf.i18n import t

logger = logging.getLogger(__name__)


class CompareDialog(ctk.CTkToplevel):
    """
    Top-level dialog for selecting and comparing two telemetry sessions.

    Args:
        master:
            Parent widget (typically the main App window).
        reports_dir (Path):
            Directory where the generated comparison HTML will be saved.
        prefill_csv (Optional[Path]):
            If provided, Session A's CSV path is pre-populated with this
            value (used when the user clicks "Compare" on a specific row
            in ReportPanel).
    """

    def __init__(
        self,
        master,
        reports_dir: Path = Path("reports"),
        prefill_csv: Optional[Path] = None,
    ) -> None:
        super().__init__(master)

        self._reports_dir = Path(reports_dir)
        self._csv_a: Optional[Path] = prefill_csv
        self._csv_b: Optional[Path] = None

        self.title(t("compare_title"))
        self.resizable(False, False)
        self.grab_set()  # Make dialog modal

        self._build_ui()

        # Center over parent
        self.update_idletasks()
        px = master.winfo_rootx() + (master.winfo_width() - self.winfo_width()) // 2
        py = master.winfo_rooty() + (master.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{px}+{py}")

        logger.debug("CompareDialog opened (prefill_csv='%s').", prefill_csv)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        """Build all widgets inside the dialog."""
        pad = {"padx": 16, "pady": 6}

        ctk.CTkLabel(
            self,
            text=t("compare_subtitle"),
            font=ctk.CTkFont(size=15, weight="bold"),
        ).pack(pady=(16, 4))

        ctk.CTkLabel(
            self,
            text=t("compare_description"),
            font=ctk.CTkFont(size=11),
            text_color="#888888",
            justify="center",
        ).pack(pady=(0, 12))

        # ── Session A ──────────────────────────────────────────────────
        self._build_session_row(
            label=t("compare_session_a"),
            color="#2471a3",
            get_path=lambda: self._csv_a,
            set_path=self._set_csv_a,
            get_label_var=lambda: self._label_a_var,
            session_key="a",
        )

        # ── Session B ──────────────────────────────────────────────────
        self._build_session_row(
            label=t("compare_session_b"),
            color="#ca6f1e",
            get_path=lambda: self._csv_b,
            set_path=self._set_csv_b,
            get_label_var=lambda: self._label_b_var,
            session_key="b",
        )

        # ── Generate button ────────────────────────────────────────────
        ctk.CTkButton(
            self,
            text=t("compare_generate"),
            height=36,
            corner_radius=8,
            command=self._on_generate,
        ).pack(fill="x", padx=16, pady=(12, 6))

        ctk.CTkButton(
            self,
            text=t("btn_cancel"),
            height=32,
            corner_radius=8,
            fg_color="transparent",
            border_width=1,
            text_color=("gray30", "gray70"),
            command=self.destroy,
        ).pack(fill="x", padx=16, pady=(0, 16))

    def _build_session_row(
        self,
        label: str,
        color: str,
        get_path,
        set_path,
        get_label_var,
        session_key: str,
    ) -> None:
        """
        Build the UI group for one session (label field + CSV picker).

        Args:
            label:         Display label (e.g. "Session A").
            color:         Accent colour for the header label.
            get_path:      Callable that returns the current Path or None.
            set_path:      Callable that accepts the newly chosen Path.
            get_label_var: Callable that returns the StringVar for the label.
            session_key:   "a" or "b" to identify which session this row is for.
        """
        frame = ctk.CTkFrame(self, corner_radius=8)
        frame.pack(fill="x", padx=16, pady=6)

        ctk.CTkLabel(
            frame,
            text=f"● {label}",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=color,
        ).pack(anchor="w", padx=10, pady=(8, 2))

        # Custom label entry
        label_row = ctk.CTkFrame(frame, fg_color="transparent")
        label_row.pack(fill="x", padx=10, pady=(2, 4))
        ctk.CTkLabel(label_row, text=f"{t('compare_label')}:", width=50, anchor="w").pack(side="left")

        if session_key == "a":
            self._label_a_var = ctk.StringVar(value=label)
            var = self._label_a_var
        else:
            self._label_b_var = ctk.StringVar(value=label)
            var = self._label_b_var

        ctk.CTkEntry(
            label_row,
            textvariable=var,
            placeholder_text="e.g. v1.2 before update",
            height=28,
            corner_radius=6,
        ).pack(side="left", fill="x", expand=True, padx=(6, 0))

        # CSV path row
        path_row = ctk.CTkFrame(frame, fg_color="transparent")
        path_row.pack(fill="x", padx=10, pady=(0, 8))

        # Path display label (dynamically updated)
        if session_key == "a":
            self._path_a_label = ctk.CTkLabel(
                path_row,
                text=self._csv_a.name if self._csv_a else t("compare_no_file"),
                anchor="w",
                font=ctk.CTkFont(size=10),
                text_color="#aaaaaa",
            )
            path_lbl = self._path_a_label
        else:
            self._path_b_label = ctk.CTkLabel(
                path_row,
                text=t("compare_no_file"),
                anchor="w",
                font=ctk.CTkFont(size=10),
                text_color="#aaaaaa",
            )
            path_lbl = self._path_b_label

        path_lbl.pack(side="left", fill="x", expand=True)

        ctk.CTkButton(
            path_row,
            text=t("compare_browse"),
            width=80,
            height=28,
            corner_radius=6,
            command=lambda sp=set_path, pl=path_lbl: self._browse_csv(sp, pl),
        ).pack(side="right")

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------

    def _set_csv_a(self, path: Path) -> None:
        """Store the chosen CSV path for session A."""
        self._csv_a = path

    def _set_csv_b(self, path: Path) -> None:
        """Store the chosen CSV path for session B."""
        self._csv_b = path

    def _browse_csv(self, set_path, path_label: ctk.CTkLabel) -> None:
        """
        Open a file-chooser dialog for a CSV file.

        Updates *path_label* text and calls *set_path* with the chosen
        ``Path`` when the user confirms.

        Args:
            set_path:    Callable to store the chosen path.
            path_label:  Label widget to update with the filename.
        """
        chosen = filedialog.askopenfilename(
            title="Select Telemetry CSV",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if not chosen:
            return
        p = Path(chosen)
        set_path(p)
        path_label.configure(text=p.name, text_color=("gray20", "gray85"))
        logger.debug("CSV selected: '%s'.", p)

    def _on_generate(self) -> None:
        """
        Validate inputs, run the comparison engine, and open the result.

        Shows an error dialog if either CSV is missing or the engine raises.
        """
        if not self._csv_a or not self._csv_b:
            messagebox.showerror(
                parent=self,
                title=t("dlg_missing_selection"),
                message=t("dlg_missing_selection_msg"),
            )
            return

        label_a = self._label_a_var.get().strip() or t("compare_session_a")
        label_b = self._label_b_var.get().strip() or t("compare_session_b")

        try:
            from droidperf.session_compare import (
                generate_comparison_html,
                load_csv_records,
            )
        except ImportError as exc:
            messagebox.showerror(
                parent=self,
                title=t("dlg_import_error"),
                message=f"session_compare module could not be loaded:\n{exc}",
            )
            return

        try:
            a_records = load_csv_records(self._csv_a)
            b_records = load_csv_records(self._csv_b)
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("Failed to load CSV files: %s", exc)
            messagebox.showerror(
                parent=self,
                title=t("dlg_csv_load_error"),
                message=f"Could not read one of the CSV files:\n{exc}",
            )
            return

        if not a_records or not b_records:
            empty = label_a if not a_records else label_b
            messagebox.showerror(
                parent=self,
                title=t("dlg_empty_data"),
                message=f"'{empty}' CSV file is empty or could not be read. Cannot compare.",
            )
            return

        # Build output path
        self._reports_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = self._reports_dir / f"compare_{ts}.html"

        try:
            generate_comparison_html(
                a_records, b_records, label_a, label_b, out_path
            )
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("Comparison report generation failed: %s", exc)
            messagebox.showerror(
                parent=self,
                title=t("dlg_generation_error"),
                message=f"Failed to generate comparison report:\n{exc}",
            )
            return

        # Open report in browser
        try:
            webbrowser.open(out_path.resolve().as_uri())
            logger.info("Comparison report opened: '%s'.", out_path.name)
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("Could not open browser: %s", exc)

        messagebox.showinfo(
            parent=self,
            title=t("dlg_report_ready"),
            message=t("dlg_report_ready_msg", name=out_path.name),
        )
        self.destroy()
