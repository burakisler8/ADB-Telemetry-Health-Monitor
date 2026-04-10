"""
droidperf/charts.py
-------------------
Generates Matplotlib line charts from telemetry records and returns
them as base64-encoded PNG strings, ready for embedding in HTML reports.

Uses the non-interactive "Agg" backend so no display is required.

When records include a ``package`` key, each package is plotted as a
separate coloured line (using the ``tab10`` palette) so a single chart
can compare multiple packages side by side.  Records without a
``package`` key are treated as a single "device" series for backward
compatibility.

Public API:
    generate_charts(records) -> Dict[str, Optional[str]]
"""

import base64
import io
import logging
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

try:
    import matplotlib.dates as mdates
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    _MATPLOTLIB_AVAILABLE = True
    # Hard-coded tab10 palette — avoids importing pyplot (which is not
    # thread-safe when TkAgg is active in the GUI thread).
    _TAB10_COLORS = [
        "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
        "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf",
    ]
except ImportError:
    _MATPLOTLIB_AVAILABLE = False
    _TAB10_COLORS = []
    logger.warning(
        "Matplotlib not found. Charts will be omitted from the HTML report. "
        "Install with: pip install matplotlib"
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _parse_timestamps(ts_strs: List[str], context: str) -> Optional[List[datetime]]:
    """
    Convert a list of ISO-8601 strings to ``datetime`` objects.

    Args:
        ts_strs (List[str]): Timestamp strings to parse.
        context (str):       Human-readable label used in error messages.

    Returns:
        Optional[List[datetime]]: Parsed datetimes, or ``None`` on failure.
    """
    try:
        return [datetime.fromisoformat(t) for t in ts_strs]
    except ValueError as exc:
        logger.error("Timestamp parse error for chart '%s': %s", context, exc)
        return None


def _apply_ax_style(ax: Any, title: str, ylabel: str) -> None:
    """
    Apply a consistent visual style to a chart axes object.

    Args:
        ax:        Matplotlib ``Axes`` instance to style.
        title (str):  Chart title.
        ylabel (str): Y-axis label text.
    """
    ax.set_title(title, fontsize=11, pad=8)
    ax.set_ylabel(ylabel, fontsize=9)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))
    ax.grid(True, linestyle="--", alpha=0.4)


def _fig_to_base64(fig: Any) -> str:
    """
    Serialise a Matplotlib figure to a base64-encoded PNG string.

    Uses ``FigureCanvasAgg`` directly so no pyplot / GUI backend is needed.
    Safe to call from any thread.

    Args:
        fig: Matplotlib ``Figure`` instance.

    Returns:
        str: Base64-encoded PNG byte string (UTF-8 decoded).
    """
    canvas = FigureCanvasAgg(fig)
    buf = io.BytesIO()
    canvas.print_figure(buf, format="png", dpi=100, bbox_inches="tight")
    buf.seek(0)
    return base64.b64encode(buf.read()).decode("utf-8")


def _group_records_by_package(
    records: List[Dict[str, Any]]
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Partition *records* into per-package buckets.

    When a record has a ``package`` key its value is used as the bucket
    name; otherwise the record is assigned to the ``"device"`` bucket so
    that legacy single-series data continues to work unchanged.

    Args:
        records (List[Dict]): Raw telemetry record list.

    Returns:
        Dict[str, List[Dict]]: Mapping of package name → record list.
    """
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for record in records:
        key = record.get("package") or "device"
        groups[key].append(record)
    return dict(groups)


def _make_multi_line_chart(
    groups: Dict[str, List[Dict[str, Any]]],
    metric_key: str,
    title: str,
    ylabel: str,
) -> Optional[str]:
    """
    Render a multi-line chart (one line per package) and return base64 PNG.

    Each package is assigned a colour from the ``tab10`` palette.  Data
    points where the metric value is ``None`` are skipped so gaps do not
    raise errors.  When only one package exists, the legend is omitted.

    Args:
        groups (Dict[str, List[Dict]]): Per-package record groups.
        metric_key (str): Record field name to plot on the Y axis.
        title (str):  Chart title.
        ylabel (str): Y-axis label.

    Returns:
        Optional[str]: Base64 PNG string, or ``None`` if no plottable data
                       or Matplotlib is unavailable.
    """
    if not _MATPLOTLIB_AVAILABLE:
        return None

    fig = Figure(figsize=(10, 3))
    ax = fig.add_subplot(111)
    has_data = False

    for idx, (pkg_name, pkg_records) in enumerate(groups.items()):
        color = _TAB10_COLORS[idx % len(_TAB10_COLORS)]

        # Filter records where both timestamp and metric value are present.
        paired: List[Tuple[str, float]] = [
            (r["timestamp"], r[metric_key])
            for r in pkg_records
            if r.get(metric_key) is not None and r.get("timestamp")
        ]
        if not paired:
            continue

        ts_strs, vals = zip(*paired)
        dts = _parse_timestamps(list(ts_strs), f"{title}/{pkg_name}")
        if dts is None:
            continue

        ax.plot(
            dts,
            vals,
            color=color,
            linewidth=1.8,
            marker="o",
            markersize=3,
            label=pkg_name,
        )
        ax.fill_between(dts, vals, alpha=0.06, color=color)
        has_data = True

    if not has_data:
        logger.warning("No plottable data for chart '%s'.", title)
        return None

    _apply_ax_style(ax, title, ylabel)

    # Place legend below the chart when multiple packages are present so
    # that items do not overlap the plot area regardless of how many
    # packages are being tracked.
    if len(groups) > 1:
        n_items = len(groups)
        ncols = min(n_items, 4)
        n_rows = (n_items + ncols - 1) // ncols
        # Reserve enough vertical space at the bottom for the legend rows.
        bottom_fraction = min(0.06 + n_rows * 0.08, 0.40)
        handles, labels = ax.get_legend_handles_labels()
        fig.legend(
            handles,
            labels,
            loc="lower center",
            ncol=ncols,
            fontsize=7,
            bbox_to_anchor=(0.5, 0),
            framealpha=0.95,
            borderpad=0.6,
        )
        fig.autofmt_xdate(rotation=30)
        fig.tight_layout(rect=[0, bottom_fraction, 1, 1])
    else:
        fig.autofmt_xdate(rotation=30)
        fig.tight_layout()

    return _fig_to_base64(fig)


def _make_battery_chart(
    groups: Dict[str, List[Dict[str, Any]]],
    metric_key: str,
    title: str,
    ylabel: str,
    color: str,
) -> Optional[str]:
    """
    Render a single-line battery chart using data from the first package group.

    Battery metrics are device-level (not per-package), so only the first
    group's values are used.  Duplicate timestamps within that group are
    preserved as-is.

    Args:
        groups (Dict[str, List[Dict]]): Per-package record groups.
        metric_key (str): Battery field name (e.g. ``"batt_level"``).
        title (str):  Chart title.
        ylabel (str): Y-axis label.
        color (str):  Line colour hex string.

    Returns:
        Optional[str]: Base64 PNG string, or ``None`` when unavailable.
    """
    if not _MATPLOTLIB_AVAILABLE:
        return None
    if not groups:
        return None

    # Use the first package's records as the battery data source.
    first_records = next(iter(groups.values()))

    paired: List[Tuple[str, float]] = [
        (r["timestamp"], r[metric_key])
        for r in first_records
        if r.get(metric_key) is not None and r.get("timestamp")
    ]
    if not paired:
        logger.warning("No plottable data for battery chart '%s'.", title)
        return None

    ts_strs, vals = zip(*paired)
    dts = _parse_timestamps(list(ts_strs), title)
    if dts is None:
        return None

    fig = Figure(figsize=(10, 3))
    ax = fig.add_subplot(111)
    ax.plot(dts, vals, color=color, linewidth=1.8, marker="o", markersize=3)
    ax.fill_between(dts, vals, alpha=0.08, color=color)
    _apply_ax_style(ax, title, ylabel)
    fig.autofmt_xdate(rotation=30)
    fig.tight_layout()
    return _fig_to_base64(fig)


# ---------------------------------------------------------------------------
# Battery attribution chart
# ---------------------------------------------------------------------------

def generate_battery_attribution_chart(
    attribution: Dict[str, float],
    max_entries: int = 20,
) -> Optional[str]:
    """
    Render a horizontal bar chart showing per-app battery consumption.

    Displays each entry as a percentage of the total measured consumption.
    Hardware components (Screen, Cell, etc.) are rendered with a distinct
    colour so users can distinguish app usage from system overhead.

    Args:
        attribution (Dict[str, float]): Mapping of label → mAh consumed,
            as returned by ``collectors.battery_stats.get_battery_attribution``.
        max_entries (int): Maximum number of bars to show (default 20).
            Remaining entries are aggregated as "Others".

    Returns:
        Optional[str]: Base64-encoded PNG string, or ``None`` when
            Matplotlib is unavailable or *attribution* is empty.
    """
    if not _MATPLOTLIB_AVAILABLE or not attribution:
        return None

    # Trim to max_entries; aggregate the rest.
    items = list(attribution.items())[:max_entries]
    total_all = sum(attribution.values())
    shown_total = sum(v for _, v in items)
    if total_all > shown_total:
        items.append(("Others", total_all - shown_total))

    labels = [label for label, _ in items]
    mahs = [mah for _, mah in items]
    total = sum(mahs) or 1.0
    pcts = [mah / total_all * 100 for mah in mahs]

    # Hardware component labels (lower-case for matching).
    _HW = frozenset([
        "screen", "cell", "cell standby", "wifi", "bluetooth",
        "idle", "radio", "sensors", "flashlight", "camera",
        "audio", "video", "phone", "modem",
    ])

    colors = [
        "#95a5a6" if lbl.lower() in _HW or lbl == "Others" else _TAB10_COLORS[i % len(_TAB10_COLORS)]
        for i, lbl in enumerate(labels)
    ]

    n = len(labels)
    fig_height = max(2.5, 0.38 * n + 0.8)
    fig = Figure(figsize=(10, fig_height))
    ax = fig.add_subplot(111)

    y_pos = list(range(n))
    bars = ax.barh(y_pos, pcts, color=colors, edgecolor="white", linewidth=0.5)

    # Annotate each bar with "X.X% (Y.Y mAh)".
    for bar, pct, mah in zip(bars, pcts, mahs):
        ax.text(
            bar.get_width() + 0.3,
            bar.get_y() + bar.get_height() / 2,
            f"{pct:.1f}%  ({mah:.2f} mAh)",
            va="center",
            fontsize=7.5,
        )

    ax.set_yticks(y_pos)
    ax.set_yticklabels(labels, fontsize=8)
    ax.invert_yaxis()  # Highest consumer on top.
    ax.set_xlabel("Share of total session consumption (%)", fontsize=8)
    ax.set_title("Battery Attribution — Per App / Component", fontsize=11, pad=8)
    ax.set_xlim(0, max(pcts) * 1.35)  # Extra room for text labels.
    ax.grid(True, axis="x", linestyle="--", alpha=0.4)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)

    fig.tight_layout()
    return _fig_to_base64(fig)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_charts(records: List[Dict[str, Any]]) -> Dict[str, Optional[str]]:
    """
    Generate all telemetry charts from a list of metric records.

    Produces four charts: RAM, CPU, Battery Level, and Battery Temperature.
    Each chart is a standalone base64 PNG that can be embedded directly in
    an HTML ``<img src="data:image/png;base64,...">`` tag.

    When records contain a ``package`` field, RAM and CPU charts will show
    one line per package using ``tab10`` palette colours.  Battery charts
    always display a single device-level line derived from the first
    package's data.  Records without a ``package`` key are treated as a
    single ``"device"`` series for backward compatibility.

    Args:
        records (List[Dict]): Collected metric snapshot rows from the
                              monitoring loop.

    Returns:
        Dict[str, Optional[str]]: Keys are ``"ram"``, ``"cpu"``,
        ``"batt_level"``, ``"batt_temp"``; values are base64 PNG strings
        or ``None`` when data is missing or Matplotlib is unavailable.
    """
    empty: Dict[str, Optional[str]] = {
        "ram": None,
        "cpu": None,
        "batt_level": None,
        "batt_temp": None,
    }

    if not records:
        logger.warning("No records provided — skipping chart generation.")
        return empty

    groups = _group_records_by_package(records)

    ram_chart = _make_multi_line_chart(groups, "ram_pss_kb", "RAM Usage (PSS)", "KB")

    # Use per-process CPU when available; fall back to system user-space CPU
    # when cpu_total_pct was not collected (e.g. older sessions).
    has_process_cpu = any(r.get("cpu_total_pct") is not None for r in records)
    if has_process_cpu:
        cpu_chart = _make_multi_line_chart(
            groups, "cpu_total_pct", "CPU Usage — Per Process (%)", "%"
        )
    else:
        logger.warning(
            "cpu_total_pct unavailable in records; "
            "falling back to system user-space CPU chart."
        )
        cpu_chart = _make_multi_line_chart(
            groups, "cpu_user_pct", "CPU Usage — System User Space (%)", "%"
        )
    batt_level_chart = _make_battery_chart(
        groups, "batt_level", "Battery Level", "%", "#27ae60"
    )
    batt_temp_chart = _make_battery_chart(
        groups, "batt_temp_c", "Battery Temperature", "°C", "#e67e22"
    )

    charts: Dict[str, Optional[str]] = {
        "ram": ram_chart,
        "cpu": cpu_chart,
        "batt_level": batt_level_chart,
        "batt_temp": batt_temp_chart,
    }

    for name, data in charts.items():
        if data:
            logger.debug("Chart '%s' generated successfully.", name)
        else:
            logger.warning("Chart '%s' could not be generated.", name)

    return charts
