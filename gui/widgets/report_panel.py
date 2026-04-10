"""
gui/widgets/report_panel.py
-----------------------------
Reports browser panel for the ADB Telemetry & Health Monitor GUI.

Displays a scrollable list of previously generated HTML reports found
in the configured output directory.  Each entry shows enriched metadata
(device, packages, record count) loaded from the sidecar ``.meta.json``
file when available.

A search bar at the top filters the visible list in real time by report
name, device ID, or package label.

Public API:
    ReportPanel(master, reports_dir, **kwargs)
"""

import json
import logging
import webbrowser
from pathlib import Path
from tkinter import messagebox
from typing import Dict, List, Optional

import customtkinter as ctk

from droidperf.i18n import t
from gui.widgets.compare_dialog import CompareDialog

logger = logging.getLogger(__name__)


def _load_meta(html_path: Path) -> Optional[Dict]:
    """
    Load the sidecar ``.meta.json`` for *html_path*.

    Args:
        html_path (Path): Path to the HTML report file.

    Returns:
        Optional[Dict]: Parsed metadata dict, or ``None`` if unavailable.
    """
    meta_path = html_path.with_suffix(".meta.json")
    if not meta_path.exists():
        return None
    try:
        with open(meta_path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("Could not load meta for '%s': %s", html_path.name, exc)
        return None


class ReportPanel(ctk.CTkFrame):
    """
    Scrollable list of generated HTML telemetry reports with search.

    Scans ``reports_dir`` for ``*.html`` files on initialisation (and
    whenever ``_scan_reports`` / ``_populate`` are called explicitly) and
    presents each file as a labelled row with "Open in Browser" and
    "Delete" buttons.  Deleting a report also removes the matching
    ``telemetry_*.csv`` and ``.meta.json`` files.

    Args:
        master:
            Parent widget.
        reports_dir (Path):
            Directory to scan for HTML reports.  Defaults to
            ``Path("reports")``.
        **kwargs:
            Forwarded to ``ctk.CTkFrame.__init__``.
    """

    def __init__(
        self,
        master,
        reports_dir: Path = Path("reports"),
        **kwargs,
    ) -> None:
        super().__init__(master, **kwargs)

        self._reports_dir = Path(reports_dir)
        self._report_paths: List[Path] = []

        self._build_ui()
        self._scan_reports()
        self._populate()
        logger.debug("ReportPanel initialised with reports_dir='%s'.", self._reports_dir)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        """Construct the top bar, search field, and the scrollable list."""
        # Top bar: title label + refresh button side by side.
        top_bar = ctk.CTkFrame(self, fg_color="transparent")
        top_bar.pack(fill="x", padx=8, pady=(8, 4))

        ctk.CTkLabel(
            top_bar,
            text=t("label_reports_title"),
            font=ctk.CTkFont(weight="bold"),
        ).pack(side="left", padx=4)

        ctk.CTkButton(
            top_bar,
            text=t("btn_refresh"),
            width=90,
            command=self._refresh,
        ).pack(side="right", padx=4)

        # Search / filter bar.
        search_bar = ctk.CTkFrame(self, fg_color="transparent")
        search_bar.pack(fill="x", padx=8, pady=(0, 4))

        ctk.CTkLabel(
            search_bar,
            text="🔍",
            font=ctk.CTkFont(size=13),
            width=24,
        ).pack(side="left")

        self._search_var = ctk.StringVar()
        self._search_var.trace_add("write", lambda *_: self._on_search_change())

        ctk.CTkEntry(
            search_bar,
            textvariable=self._search_var,
            placeholder_text=t("label_filter_placeholder"),
            height=30,
            corner_radius=6,
        ).pack(side="left", fill="x", expand=True, padx=(4, 0))

        # Scrollable frame for report entries.
        self._scroll_frame = ctk.CTkScrollableFrame(self, label_text="")
        self._scroll_frame.pack(fill="both", expand=True, padx=8, pady=(0, 8))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _on_search_change(self) -> None:
        """Re-filter the visible report list when search text changes."""
        self._populate()

    def _scan_reports(self) -> None:
        """
        Scan ``reports_dir`` for HTML files, sorted newest-first by mtime.

        Updates ``self._report_paths`` in place.  Logs a warning if the
        directory does not exist rather than raising.
        """
        if not self._reports_dir.exists():
            logger.warning(
                "Reports directory '%s' does not exist yet.", self._reports_dir
            )
            self._report_paths = []
            return

        try:
            html_files = list(self._reports_dir.glob("*.html"))
            self._report_paths = sorted(
                html_files, key=lambda p: p.stat().st_mtime, reverse=True
            )
            logger.debug(
                "Found %d HTML report(s) in '%s'.",
                len(self._report_paths),
                self._reports_dir,
            )
        except OSError as exc:
            logger.error("Error scanning reports directory: %s", exc)
            self._report_paths = []

    def _populate(self) -> None:
        """
        Clear the scrollable frame and add rows for matching reports.

        Applies the current search filter (case-insensitive substring
        match against filename, device_id, and package_label).
        """
        query = self._search_var.get().strip().lower()

        # Remove all existing child widgets from the scrollable frame.
        for widget in self._scroll_frame.winfo_children():
            widget.destroy()

        filtered = self._apply_filter(self._report_paths, query)

        if not filtered:
            no_result_text = t("label_no_reports_search") if query else t("label_no_reports")
            ctk.CTkLabel(
                self._scroll_frame,
                text=no_result_text,
                text_color="#888888",
            ).pack(padx=8, pady=8, anchor="w")
            return

        for path in filtered:
            self._add_report_row(path)

    def _apply_filter(self, paths: List[Path], query: str) -> List[Path]:
        """
        Return the subset of *paths* that match *query*.

        If *query* is empty all paths are returned unchanged.  Otherwise
        the filename and any available metadata fields are checked.

        Args:
            paths (List[Path]): Full list of report paths.
            query (str):        Lower-cased search string.

        Returns:
            List[Path]: Matching paths (preserves original order).
        """
        if not query:
            return paths

        result = []
        for path in paths:
            haystack = path.name.lower()
            meta = _load_meta(path)
            if meta:
                haystack += " " + meta.get("device_id", "").lower()
                haystack += " " + meta.get("package_label", "").lower()
                haystack += " " + meta.get("name", "").lower()
                haystack += " " + meta.get("notes", "").lower()
                for pkg in meta.get("packages", []):
                    haystack += " " + pkg.lower()
                for tag in meta.get("tags", []):
                    haystack += " " + tag.lower()
            if query in haystack:
                result.append(path)
        return result

    def _add_report_row(self, path: Path) -> None:
        """
        Add a single report row to the scrollable frame.

        Loads sidecar metadata (if available) to display device ID,
        package label, and record count alongside the filename.

        Args:
            path (Path): Absolute path to the HTML report file.
        """
        meta = _load_meta(path)

        row = ctk.CTkFrame(self._scroll_frame, fg_color="#1e1e2e", corner_radius=6)
        row.pack(fill="x", padx=4, pady=3)

        # Left column: filename + metadata subtitle.
        info_col = ctk.CTkFrame(row, fg_color="transparent")
        info_col.pack(side="left", fill="x", expand=True, padx=8, pady=4)

        ctk.CTkLabel(
            info_col,
            text=path.name,
            anchor="w",
            font=ctk.CTkFont(size=12, weight="bold"),
        ).pack(fill="x")

        if meta:
            device = meta.get("device_id", "")
            pkg_label = meta.get("package_label", "")
            count = meta.get("record_count", "?")
            start = (meta.get("session_start") or "")[:19].replace("T", " ")
            custom_name = meta.get("name", "")
            tags = meta.get("tags", [])
            tag_str = ("  [" + ", ".join(tags) + "]") if tags else ""
            label_line = f"{custom_name}  " if custom_name else ""
            subtitle = f"{label_line}{device}  ·  {pkg_label}  ·  {count} {t('label_records')}  ·  {start}{tag_str}"
        else:
            subtitle = path.stem

        ctk.CTkLabel(
            info_col,
            text=subtitle,
            anchor="w",
            font=ctk.CTkFont(size=10),
            text_color="#888888",
        ).pack(fill="x")

        # Right column: action buttons (right-to-left packing order).
        ctk.CTkButton(
            row,
            text=t("btn_delete"),
            width=70,
            height=28,
            fg_color="#c0392b",
            hover_color="#922b21",
            corner_radius=6,
            command=lambda p=path: self._confirm_delete(p),
        ).pack(side="right", padx=(2, 8), pady=6)

        ctk.CTkButton(
            row,
            text=t("btn_compare"),
            width=80,
            height=28,
            fg_color="#7d3c98",
            hover_color="#6c3483",
            corner_radius=6,
            command=lambda p=path: self._open_compare(p),
        ).pack(side="right", padx=2, pady=6)

        ctk.CTkButton(
            row,
            text=t("btn_pdf"),
            width=60,
            height=28,
            fg_color="#1e8449",
            hover_color="#196f3d",
            corner_radius=6,
            command=lambda p=path: self._export_pdf(p),
        ).pack(side="right", padx=2, pady=6)

        ctk.CTkButton(
            row,
            text=t("btn_tag"),
            width=55,
            height=28,
            fg_color="#2e86c1",
            hover_color="#1f618d",
            corner_radius=6,
            command=lambda p=path: self._edit_tags(p),
        ).pack(side="right", padx=2, pady=6)

        ctk.CTkButton(
            row,
            text=t("btn_open"),
            width=80,
            height=28,
            corner_radius=6,
            command=lambda p=path: self._open_report(p),
        ).pack(side="right", padx=2, pady=6)

    def _edit_tags(self, path: Path) -> None:
        """
        Open a small dialog to add or edit the name/tags for a report.

        Reads existing metadata from the sidecar ``.meta.json`` and writes
        the updated values back on save.

        Args:
            path (Path): Path to the HTML report file.
        """
        meta = _load_meta(path) or {}

        dialog = ctk.CTkToplevel(self.winfo_toplevel())
        dialog.title(t("edit_tags_title"))
        dialog.resizable(False, False)
        dialog.grab_set()

        # Center over parent
        dialog.update_idletasks()
        px = self.winfo_toplevel().winfo_rootx() + 80
        py = self.winfo_toplevel().winfo_rooty() + 100
        dialog.geometry(f"+{px}+{py}")

        ctk.CTkLabel(
            dialog,
            text=t("edit_tags_metadata_title"),
            font=ctk.CTkFont(size=14, weight="bold"),
        ).pack(pady=(14, 6), padx=16)

        def _field(label_text: str, default: str) -> ctk.StringVar:
            row = ctk.CTkFrame(dialog, fg_color="transparent")
            row.pack(fill="x", padx=16, pady=3)
            ctk.CTkLabel(row, text=label_text, width=80, anchor="w",
                         font=ctk.CTkFont(size=12)).pack(side="left")
            var = ctk.StringVar(value=default)
            ctk.CTkEntry(row, textvariable=var, width=220,
                         height=28).pack(side="left")
            return var

        name_var = _field(t("edit_tags_name"), meta.get("name") or path.stem)
        tags_var = _field(t("edit_tags_tags"), ", ".join(meta.get("tags") or []))
        notes_var = _field(t("edit_tags_notes"), meta.get("notes") or "")

        def _save() -> None:
            meta["name"] = name_var.get().strip() or path.stem
            raw_tags = tags_var.get()
            meta["tags"] = [tag.strip() for tag in raw_tags.split(",") if tag.strip()]
            meta["notes"] = notes_var.get().strip()

            meta_path = path.with_suffix(".meta.json")
            try:
                import json as _json
                with open(meta_path, "w", encoding="utf-8") as fh:
                    _json.dump(meta, fh, indent=2, ensure_ascii=False)
                logger.info("Metadata saved for '%s'.", path.name)
            except OSError as exc:
                messagebox.showerror(
                    parent=dialog,
                    title=t("dlg_save_error"),
                    message=f"Could not save metadata:\n{exc}",
                )
                return
            dialog.destroy()
            self._refresh()

        ctk.CTkButton(
            dialog, text=t("btn_save"), height=32, corner_radius=8,
            command=_save,
        ).pack(fill="x", padx=16, pady=(10, 4))

        ctk.CTkButton(
            dialog, text=t("btn_cancel"), height=28, corner_radius=8,
            fg_color="transparent", border_width=1,
            text_color=("gray30", "gray70"),
            command=dialog.destroy,
        ).pack(fill="x", padx=16, pady=(0, 14))

    def _open_report(self, path: Path) -> None:
        """
        Open the given HTML report in the system default web browser.

        Args:
            path (Path): Path to the HTML report file.
        """
        try:
            uri = path.resolve().as_uri()
            webbrowser.open(uri)
            logger.info("Opened report in browser: '%s'.", path.name)
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("Failed to open report '%s': %s", path, exc)

    def _open_compare(self, path: Path) -> None:
        """
        Open the session comparison dialog pre-filled with *path*'s CSV.

        Derives the matching telemetry CSV from the HTML report filename
        (``report_YYYYMMDD_HHMMSS.html`` → ``telemetry_YYYYMMDD_HHMMSS.csv``).
        If the CSV is not found the dialog still opens without pre-fill.

        Args:
            path (Path): Path to the HTML report file.
        """
        csv_name = path.name.replace("report_", "telemetry_", 1).replace(".html", ".csv")
        csv_path = self._reports_dir / csv_name
        prefill = csv_path if csv_path.exists() else None

        if not prefill:
            logger.warning(
                "Matching CSV not found for '%s'; dialog will open without pre-fill.",
                path.name,
            )

        CompareDialog(
            master=self.winfo_toplevel(),
            reports_dir=self._reports_dir,
            prefill_csv=prefill,
        )

    def _export_pdf(self, path: Path) -> None:
        """
        Generate a PDF version of the given HTML report.

        Derives device/package metadata from the sidecar ``.meta.json`` and
        reloads raw records from the paired telemetry CSV.  The resulting
        PDF is saved next to the HTML report and opened with the OS default
        PDF viewer.

        Args:
            path (Path): Path to the HTML report file.
        """
        import csv as _csv

        # Locate paired CSV
        csv_name = path.name.replace("report_", "telemetry_", 1).replace(".html", ".csv")
        csv_path = self._reports_dir / csv_name

        if not csv_path.exists():
            messagebox.showerror(
                title=t("dlg_csv_not_found"),
                message=t("dlg_csv_not_found_msg", csv_name=csv_name),
            )
            return

        # Load records from CSV
        numeric_fields = {
            "ram_pss_kb", "cpu_total_pct", "cpu_user_pct", "cpu_load_1m",
            "batt_level", "batt_temp_c", "batt_voltage_mv",
        }
        records = []
        try:
            with open(csv_path, newline="", encoding="utf-8") as fh:
                reader = _csv.DictReader(fh)
                for row in reader:
                    parsed = {}
                    for key, val in row.items():
                        stripped = val.strip() if val else ""
                        if key in numeric_fields:
                            try:
                                parsed[key] = float(stripped) if stripped else None
                            except ValueError:
                                parsed[key] = None
                        else:
                            parsed[key] = stripped or None
                    records.append(parsed)
        except (OSError, _csv.Error) as exc:
            logger.error("Failed to read CSV '%s': %s", csv_path, exc)
            messagebox.showerror(
                title=t("dlg_csv_read_error"),
                message=f"Could not read telemetry data:\n{exc}",
            )
            return

        # Load metadata (includes logcat events written by MonitorEngine)
        meta = _load_meta(path)
        device_id = (meta or {}).get("device_id", "unknown")
        package_name = (meta or {}).get("package_label", "unknown")
        logcat_events = (meta or {}).get("logcat_events", [])

        # Output path: same name, .pdf extension
        pdf_path = path.with_suffix(".pdf")

        try:
            from droidperf.reporter import generate_pdf_report
            generate_pdf_report(
                records=records,
                logcat_events=logcat_events,
                device_id=device_id,
                package_name=package_name,
                output_path=pdf_path,
            )
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("PDF generation failed: %s", exc)
            messagebox.showerror(
                title=t("dlg_pdf_failed"),
                message=f"Could not generate PDF:\n{exc}",
            )
            return

        # Open with system default viewer
        import os
        import subprocess
        import sys
        try:
            if sys.platform == "win32":
                os.startfile(str(pdf_path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(pdf_path)])
            else:
                subprocess.Popen(["xdg-open", str(pdf_path)])
            logger.info("PDF report opened: '%s'.", pdf_path.name)
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("Could not open PDF automatically: %s", exc)

        messagebox.showinfo(
            title=t("dlg_pdf_exported"),
            message=t("dlg_pdf_saved_msg", name=pdf_path.name),
        )

    def _confirm_delete(self, path: Path) -> None:
        """
        Show a confirmation dialog and delete the report if confirmed.

        Also deletes the matching ``telemetry_*.csv`` and ``.meta.json``
        files that share the same timestamp tag.

        Args:
            path (Path): Path to the HTML report file to delete.
        """
        confirmed = messagebox.askyesno(
            title=t("dlg_delete_report"),
            message=t("dlg_delete_report_msg", name=path.name),
        )
        if not confirmed:
            logger.debug("Delete cancelled by user for '%s'.", path.name)
            return

        self._delete_report(path)

    def _delete_report(self, path: Path) -> None:
        """
        Delete the HTML report and its paired CSV + meta.json files.

        Args:
            path (Path): Path to the HTML report file.
        """
        # Delete the HTML report.
        try:
            path.unlink()
            logger.info("Deleted report: '%s'.", path.name)
        except OSError as exc:
            logger.error("Failed to delete report '%s': %s", path.name, exc)
            messagebox.showerror(
                title=t("dlg_delete_failed"),
                message=f"Could not delete {path.name}:\n{exc}",
            )
            return

        # Derive and delete the matching CSV (best-effort).
        csv_name = path.name.replace("report_", "telemetry_", 1).replace(".html", ".csv")
        csv_path = self._reports_dir / csv_name
        if csv_path.exists():
            try:
                csv_path.unlink()
                logger.info("Deleted matching CSV: '%s'.", csv_name)
            except OSError as exc:
                logger.warning("Could not delete matching CSV '%s': %s", csv_name, exc)

        # Delete sidecar metadata (best-effort).
        meta_path = path.with_suffix(".meta.json")
        if meta_path.exists():
            try:
                meta_path.unlink()
                logger.info("Deleted sidecar metadata: '%s'.", meta_path.name)
            except OSError as exc:
                logger.warning("Could not delete meta file '%s': %s", meta_path.name, exc)

        # Refresh the list to reflect the deletion.
        self._refresh()

    def _refresh(self) -> None:
        """Re-scan the reports directory and repopulate the list."""
        self._scan_reports()
        self._populate()
        logger.debug("ReportPanel refreshed.")
