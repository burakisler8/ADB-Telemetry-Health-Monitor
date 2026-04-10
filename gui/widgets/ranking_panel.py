"""
gui/widgets/ranking_panel.py
-----------------------------
Live CPU-impact ranking widget pinned to the bottom of the left sidebar.

Ranks all monitored packages by their average CPU percentage and displays
the top-N as colour-coded rows with a progress bar.  Redrawn every cycle.

Color coding:
    Red    >= 20 % CPU  — high impact
    Orange >= 10 % CPU  — medium impact
    Yellow >=  5 % CPU  — low-medium impact
    Green  <   5 % CPU  — minimal impact

Public API:
    RankingPanel(master, **kwargs)
    RankingPanel.update(rows)
    RankingPanel.clear()
"""

import logging
from collections import defaultdict
from typing import Any, Dict, List, Tuple

import customtkinter as ctk

from droidperf.i18n import t

logger = logging.getLogger(__name__)

_MAX_VISIBLE = 6


def _impact_color(cpu_pct: float) -> str:
    """Return a hex colour string reflecting the CPU impact level."""
    if cpu_pct >= 20:
        return "#e74c3c"
    if cpu_pct >= 10:
        return "#e67e22"
    if cpu_pct >= 5:
        return "#f1c40f"
    return "#27ae60"


class RankingPanel(ctk.CTkFrame):
    """
    Compact vertical ranking list ordered by average CPU %.

    Sits at the bottom of the left sidebar beneath the ControlPanel.
    Accumulates CPU samples per package across all monitoring cycles and
    redraws on every new snapshot.

    Args:
        master: Parent widget.
        **kwargs: Forwarded to ``ctk.CTkFrame.__init__``.
    """

    def __init__(self, master: Any, **kwargs) -> None:
        super().__init__(master, **kwargs)
        # key → list of cpu_total_pct values
        self._stats: Dict[str, List[float]] = defaultdict(list)
        self._build_ui()
        logger.debug("RankingPanel initialised.")

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        """Build the section header and scrollable package list."""
        # Top divider
        ctk.CTkFrame(self, height=1, fg_color="#2d2d2d").pack(fill="x")

        # Header row
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=14, pady=(8, 4))

        ctk.CTkLabel(
            header,
            text=t("label_cpu_impact"),
            font=ctk.CTkFont(size=10, weight="bold"),
            text_color="#7f8c8d",
            anchor="w",
        ).pack(side="left")

        ctk.CTkLabel(
            header,
            text=t("label_avg_pct"),
            font=ctk.CTkFont(size=10),
            text_color="#555555",
            anchor="e",
        ).pack(side="right")

        # Scrollable list
        self._list_frame = ctk.CTkScrollableFrame(
            self,
            height=130,
            fg_color="transparent",
            label_text="",
            scrollbar_button_color="#2d2d2d",
            scrollbar_button_hover_color="#444444",
        )
        self._list_frame.pack(fill="x", padx=8, pady=(0, 8))
        self._list_frame.grid_columnconfigure(1, weight=1)

        self._show_placeholder()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update(self, rows: List[Dict[str, Any]]) -> None:
        """
        Ingest a batch of metric rows and refresh the ranking.

        Only rows with a non-None ``cpu_total_pct`` value contribute.

        Args:
            rows (List[Dict]): Record dicts from one monitoring cycle.
        """
        changed = False
        for row in rows:
            pkg = row.get("package") or "unknown"
            device_id = row.get("device_id", "")
            key = f"{device_id}/{pkg}" if device_id else pkg
            cpu = row.get("cpu_total_pct")
            if cpu is not None:
                self._stats[key].append(float(cpu))
                changed = True

        if changed:
            self._refresh()

    def clear(self) -> None:
        """Reset all statistics and redraw the empty list."""
        self._stats.clear()
        self._refresh()
        logger.debug("RankingPanel cleared.")

    # ------------------------------------------------------------------
    # Internal rendering
    # ------------------------------------------------------------------

    def _show_placeholder(self) -> None:
        """Muted hint shown before any data arrives."""
        ctk.CTkLabel(
            self._list_frame,
            text=t("label_waiting_data"),
            text_color="#555555",
            font=ctk.CTkFont(size=11),
        ).grid(row=0, column=0, columnspan=3, pady=14)

    def _ranked_packages(self) -> List[Tuple[str, float]]:
        """Return packages sorted by average CPU descending, capped at _MAX_VISIBLE."""
        pairs = [
            (pkg, sum(vals) / len(vals))
            for pkg, vals in self._stats.items()
            if vals
        ]
        return sorted(pairs, key=lambda x: x[1], reverse=True)[:_MAX_VISIBLE]

    def _refresh(self) -> None:
        """Recalculate averages and redraw all rows."""
        for child in self._list_frame.winfo_children():
            child.destroy()

        ranked = self._ranked_packages()
        if not ranked:
            self._show_placeholder()
            return

        max_cpu = ranked[0][1] or 1.0
        for rank, (pkg, avg_cpu) in enumerate(ranked, start=1):
            self._add_row(rank, pkg, avg_cpu, max_cpu)

    def _add_row(self, rank: int, pkg: str, avg_cpu: float, max_cpu: float) -> None:
        """
        Render a single ranked row: badge · name · percent · bar.

        Args:
            rank (int):      1-based position.
            pkg (str):       Key (may include device prefix).
            avg_cpu (float): Average CPU % for this entry.
            max_cpu (float): Highest avg CPU (scales progress bar to 100 %).
        """
        color = _impact_color(avg_cpu)
        row = rank - 1  # zero-based grid index

        # Compact display name
        if "/" in pkg:
            dev_part, pkg_part = pkg.split("/", 1)
            short_dev = dev_part.split(":")[-1] if ":" in dev_part else dev_part[-6:]
            short_name = f"{short_dev}/{pkg_part.split('.')[-1]}"
        else:
            short_name = pkg.split(".")[-1]

        # Rank badge
        ctk.CTkLabel(
            self._list_frame,
            text=f"#{rank}",
            width=24,
            font=ctk.CTkFont(weight="bold", size=10),
            text_color=color,
            anchor="center",
        ).grid(row=row * 2, column=0, padx=(2, 4), sticky="w")

        # Package short name
        ctk.CTkLabel(
            self._list_frame,
            text=short_name,
            font=ctk.CTkFont(size=11),
            anchor="w",
            text_color="#d0d0d0",
        ).grid(row=row * 2, column=1, sticky="ew")

        # CPU %
        ctk.CTkLabel(
            self._list_frame,
            text=f"{avg_cpu:.1f}%",
            width=40,
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=color,
            anchor="e",
        ).grid(row=row * 2, column=2, padx=(4, 2), sticky="e")

        # Progress bar
        bar = ctk.CTkProgressBar(
            self._list_frame,
            height=3,
            progress_color=color,
            fg_color="#2d2d2d",
        )
        bar.set(min(avg_cpu / max(max_cpu, 0.01), 1.0))
        bar.grid(row=row * 2 + 1, column=0, columnspan=3,
                 sticky="ew", padx=2, pady=(0, 4))
