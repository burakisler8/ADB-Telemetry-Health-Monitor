"""
gui/widgets/chart_panel.py
---------------------------
Live-updating embedded Matplotlib chart panel for the Dashboard tab.

Displays four real-time charts arranged in a 2 × 2 grid:
  - RAM PSS (KB)         — one line per package
  - CPU %                — one line per package
  - Battery Level (%)    — device-level single line
  - Battery Temp (°C)    — device-level single line

Design goals:
  - Seamless dark theme: figure and axes backgrounds match the app palette.
  - Readable typography: axis labels, tick marks, and titles sized for
    comfortable reading at 1280 × 800.
  - Breathing room: enough subplot spacing that titles and tick labels
    never overlap the plot area.
  - A rolling window of 60 data points per series is maintained so the
    charts stay legible during long sessions.

Public API:
    ChartPanel(master, **kwargs)
    ChartPanel.update(rows)
    ChartPanel.clear()
"""

import logging
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional

import customtkinter as ctk

from droidperf.i18n import t

logger = logging.getLogger(__name__)

try:
    import matplotlib
    matplotlib.use("TkAgg")
    # Pin font family upfront so matplotlib skips the expensive font-search
    # step when rendering non-ASCII (e.g. Turkish) axis titles/labels.
    matplotlib.rcParams["font.family"] = "DejaVu Sans"
    matplotlib.rcParams["font.sans-serif"] = ["DejaVu Sans"]
    # Suppress verbose font-manager debug noise.
    logging.getLogger("matplotlib.font_manager").setLevel(logging.WARNING)
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    from matplotlib.figure import Figure

    _MATPLOTLIB_AVAILABLE = True
    _COLORS = [
        "#4fc3f7",  # light blue
        "#ef5350",  # red
        "#66bb6a",  # green
        "#ffa726",  # orange
        "#ab47bc",  # purple
        "#26c6da",  # cyan
        "#d4e157",  # lime
        "#ec407a",  # pink
        "#42a5f5",  # blue
        "#ff7043",  # deep orange
    ]
except ImportError:
    _MATPLOTLIB_AVAILABLE = False
    _COLORS = []
    logger.warning("Matplotlib not available. Install with: pip install matplotlib")

_ROLLING_WINDOW = 60
# Minimum ms between successive chart redraws (debounce).
_REDRAW_DELAY_MS = 600

# ── Palette (must match CustomTkinter dark theme) ──────────────────────────
_APP_BG    = "#212121"   # outer frame / app background
_FIG_BG    = "#1a1a1a"   # figure background
_AX_BG     = "#242424"   # individual axes background
_GRID_CLR  = "#3a3a3a"   # subtle grid lines
_TEXT_CLR  = "#c0c0c0"   # axis labels, tick labels
_TITLE_CLR = "#e0e0e0"   # subplot titles
_SPINE_CLR = "#3a3a3a"   # axes border lines


class ChartPanel(ctk.CTkFrame):
    """
    Embedded live chart panel showing RAM, CPU, and battery metrics.

    Call ``update(rows)`` after each monitoring cycle to append new data
    points; the charts are redrawn immediately via ``draw_idle()``.
    Call ``clear()`` before starting a new session to reset all series.

    Args:
        master: Parent widget.
        **kwargs: Forwarded to ``ctk.CTkFrame.__init__``.
    """

    def __init__(self, master: Any, **kwargs) -> None:
        # Remove the default frame border so matplotlib canvas fills edge-to-edge.
        kwargs.setdefault("fg_color", _APP_BG)
        kwargs.setdefault("corner_radius", 0)
        super().__init__(master, **kwargs)

        # _data[series_key] = {timestamps, ram, cpu, batt_level, batt_temp}
        self._data: Dict[str, Dict[str, list]] = {}

        # Debounce: schedule at most one redraw per _REDRAW_DELAY_MS window.
        self._redraw_pending: bool = False
        # Cache the last set of series keys to detect when a full rebuild is needed.
        self._last_series_keys: List[str] = []

        if _MATPLOTLIB_AVAILABLE:
            self._build_charts()
        else:
            ctk.CTkLabel(
                self,
                text=t("chart_matplotlib_missing"),
                text_color="#e74c3c",
                font=ctk.CTkFont(size=13),
            ).pack(expand=True)

        logger.debug("ChartPanel initialised.")

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_charts(self) -> None:
        """Create the 2 × 2 Matplotlib figure and embed the TkAgg canvas."""
        self.fig = Figure(facecolor=_FIG_BG)

        # Use a tight GridSpec so spacing can be controlled precisely.
        from matplotlib.gridspec import GridSpec
        gs = GridSpec(
            2, 2,
            figure=self.fig,
            left=0.07, right=0.97,
            top=0.93, bottom=0.08,
            hspace=0.48, wspace=0.32,
        )

        self._ax_ram        = self.fig.add_subplot(gs[0, 0])
        self._ax_cpu        = self.fig.add_subplot(gs[0, 1])
        self._ax_batt_level = self.fig.add_subplot(gs[1, 0])
        self._ax_batt_temp  = self.fig.add_subplot(gs[1, 1])

        for ax, title, ylabel in [
            (self._ax_ram,        t("chart_ram_usage"),        t("chart_pss_kb")),
            (self._ax_cpu,        t("chart_cpu_usage"),        "%"),
            (self._ax_batt_level, t("chart_battery_level"),    "%"),
            (self._ax_batt_temp,  t("chart_battery_temp"),     "°C"),
        ]:
            self._style_ax(ax, title, ylabel)

        self.canvas = FigureCanvasTkAgg(self.fig, master=self)
        self.canvas.draw()
        self.canvas.get_tk_widget().pack(fill="both", expand=True, padx=0, pady=0)

    def _style_ax(self, ax: Any, title: str, ylabel: str) -> None:
        """Apply the dark theme to a single axes object."""
        ax.set_facecolor(_AX_BG)

        ax.set_title(title, color=_TITLE_CLR, fontsize=10, fontweight="bold", pad=6)
        ax.set_ylabel(ylabel, color=_TEXT_CLR, fontsize=8, labelpad=4)

        ax.tick_params(colors=_TEXT_CLR, labelsize=7, length=3, width=0.6)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))

        ax.grid(True, linestyle="--", linewidth=0.5, alpha=0.6, color=_GRID_CLR)

        for spine in ax.spines.values():
            spine.set_edgecolor(_SPINE_CLR)
            spine.set_linewidth(0.8)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update(self, rows: List[Dict]) -> None:
        """
        Append new telemetry rows and redraw all charts.

        Args:
            rows: Record dicts from one monitoring cycle.  Each dict must
                  contain a ``timestamp`` ISO-8601 key plus any of
                  ``package``, ``device_id``, ``ram_pss_kb``,
                  ``cpu_total_pct``, ``batt_level``, ``batt_temp_c``.
        """
        if not _MATPLOTLIB_AVAILABLE:
            return

        for row in rows:
            pkg       = row.get("package") or "device"
            device_id = row.get("device_id", "")
            key       = f"{device_id}/{pkg}" if device_id else pkg

            if key not in self._data:
                self._data[key] = defaultdict(list)

            bucket = self._data[key]

            try:
                ts = datetime.fromisoformat(row["timestamp"])
            except (KeyError, ValueError) as exc:
                logger.warning("Skipping row with invalid timestamp: %s", exc)
                continue

            bucket["timestamps"].append(ts)
            bucket["ram"].append(row.get("ram_pss_kb"))
            bucket["cpu"].append(row.get("cpu_total_pct"))
            bucket["batt_level"].append(row.get("batt_level"))
            bucket["batt_temp"].append(row.get("batt_temp_c"))

            # Rolling window
            if len(bucket["timestamps"]) > _ROLLING_WINDOW:
                for k in bucket:
                    bucket[k] = bucket[k][-_ROLLING_WINDOW:]

        self._schedule_redraw()

    def clear(self) -> None:
        """Discard all data and redraw empty charts."""
        self._data.clear()
        self._last_series_keys = []
        if _MATPLOTLIB_AVAILABLE:
            self._redraw()
        logger.debug("ChartPanel cleared.")

    def _schedule_redraw(self) -> None:
        """Debounce: schedule one redraw after _REDRAW_DELAY_MS if not already queued."""
        if not self._redraw_pending:
            self._redraw_pending = True
            self.after(_REDRAW_DELAY_MS, self._deferred_redraw)

    def _deferred_redraw(self) -> None:
        """Execute the deferred redraw and clear the pending flag."""
        self._redraw_pending = False
        self._redraw()

    # ------------------------------------------------------------------
    # Internal rendering
    # ------------------------------------------------------------------

    def _redraw(self) -> None:
        """Redraw all four subplots from the current data store.

        Skips the expensive ``ax.cla()`` + restyle cycle when the set of
        monitored series has not changed since the last draw — this
        eliminates flicker during steady-state monitoring.
        """
        if not _MATPLOTLIB_AVAILABLE:
            return

        axes_meta = {
            self._ax_ram:        (t("chart_ram_usage"),     t("chart_pss_kb")),
            self._ax_cpu:        (t("chart_cpu_usage"),     "%"),
            self._ax_batt_level: (t("chart_battery_level"), "%"),
            self._ax_batt_temp:  (t("chart_battery_temp"),  "°C"),
        }

        current_keys = sorted(self._data.keys())
        series_changed = current_keys != self._last_series_keys
        self._last_series_keys = current_keys

        # Only clear+restyle axes when the series set changes to avoid flicker.
        if series_changed:
            for ax, (title, ylabel) in axes_meta.items():
                ax.cla()
                self._style_ax(ax, title, ylabel)
        else:
            for ax in axes_meta:
                # Remove only the plotted artists, keep styling (spines, grid).
                for artist in list(ax.lines) + list(ax.collections):
                    artist.remove()

        if not self._data:
            for ax in axes_meta:
                ax.text(
                    0.5, 0.5, t("chart_no_data"),
                    transform=ax.transAxes,
                    ha="center", va="center",
                    color="#555555", fontsize=10,
                )
            self.canvas.draw_idle()
            return

        keys = list(self._data.keys())
        show_legend = len(keys) > 1

        # ── RAM & CPU: one line per series ───────────────────────────
        for idx, key in enumerate(keys):
            color  = _COLORS[idx % len(_COLORS)]
            bucket = self._data[key]
            ts     = bucket["timestamps"]
            label  = self._short_label(key)

            self._plot_series(self._ax_ram, ts, bucket["ram"],  label, color, show_legend)
            self._plot_series(self._ax_cpu, ts, bucket["cpu"],  label, color, show_legend)

        # ── Battery: device-level, first series only ─────────────────
        first = self._data[keys[0]]
        ts    = first["timestamps"]
        self._plot_series(self._ax_batt_level, ts, first["batt_level"],
                          "level", _COLORS[2], False)
        self._plot_series(self._ax_batt_temp,  ts, first["batt_temp"],
                          "temp",  _COLORS[3], False)

        # ── Legend: only when ≤ 5 packages, placed inside the axes ─────
        # With many packages the legend overflows into the bottom charts.
        # The Ranking panel already colour-codes all packages, so skipping
        # the legend for large sets is safe and keeps the layout clean.
        if show_legend and len(keys) <= 5:
            for ax in (self._ax_ram, self._ax_cpu):
                ax.legend(
                    fontsize=7,
                    loc="upper right",
                    framealpha=0.25,
                    edgecolor=_SPINE_CLR,
                    labelcolor=_TEXT_CLR,
                    borderpad=0.4,
                    handlelength=1.2,
                )

        try:
            for ax in (self._ax_ram, self._ax_cpu,
                       self._ax_batt_level, self._ax_batt_temp):
                plt.setp(ax.get_xticklabels(), rotation=20, ha="right")
        except Exception as exc:  # pylint: disable=broad-except
            logger.debug("xticklabel rotation error (non-critical): %s", exc)

        self.canvas.draw_idle()

    @staticmethod
    def _short_label(key: str) -> str:
        """Return a compact legend label from a device/package key."""
        if "/" in key:
            dev_part, pkg_part = key.split("/", 1)
            short_dev = dev_part.split(":")[-1] if ":" in dev_part else dev_part[-6:]
            return f"{short_dev}/{pkg_part.split('.')[-1]}"
        return key.split(".")[-1]

    @staticmethod
    def _plot_series(
        ax: Any,
        timestamps: List[datetime],
        values: List[Optional[float]],
        label: str,
        color: str,
        add_label: bool,
    ) -> None:
        """
        Plot a time series on *ax*, skipping None values.

        Args:
            ax:         Target Axes.
            timestamps: X-axis datetime list aligned to *values*.
            values:     Y-axis values (None entries are skipped).
            label:      Legend label string.
            color:      Matplotlib colour spec.
            add_label:  Whether to attach a legend label.
        """
        paired = [(t, v) for t, v in zip(timestamps, values) if v is not None]
        if not paired:
            return
        ts_f, vals_f = zip(*paired)

        kwargs: Dict[str, Any] = {
            "color":     color,
            "linewidth": 1.8,
            "marker":    "o",
            "markersize": 2.5,
            "solid_capstyle": "round",
        }
        if add_label:
            kwargs["label"] = label

        ax.plot(ts_f, vals_f, **kwargs)
        ax.fill_between(ts_f, vals_f, alpha=0.07, color=color)
