"""
droidperf/session_compare.py
----------------------------
Session comparison engine for the ADB Telemetry & Health Monitor.

Reads two telemetry CSV files and produces side-by-side Matplotlib charts
(embedded as base64 PNGs) plus a standalone HTML comparison report.

Metrics compared:
  - RAM PSS (KB)       per package
  - CPU Total (%)      per package
  - Battery Level (%)  device-level
  - Battery Temp (°C)  device-level

Public API:
    load_csv_records(csv_path)       -> List[Dict]
    generate_comparison_charts(a, b) -> Dict[str, Optional[str]]
    generate_comparison_html(a_records, b_records,
                             a_label, b_label,
                             output_path)
"""

import base64
import csv
import io
import logging
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

try:
    import matplotlib.dates as mdates
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    _MPL_AVAILABLE = True
except ImportError:
    _MPL_AVAILABLE = False
    logger.warning("Matplotlib not found — comparison charts will be unavailable.")

try:
    from jinja2 import BaseLoader, Environment
    _JINJA2_AVAILABLE = True
except ImportError:
    _JINJA2_AVAILABLE = False
    logger.warning("Jinja2 not found — comparison HTML report will be unavailable.")

# ---------------------------------------------------------------------------
# Embedded HTML template for the comparison report
# ---------------------------------------------------------------------------

_COMPARE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Session Comparison — {{ label_a }} vs {{ label_b }}</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; }
    body   { font-family: Arial, sans-serif; margin: 0; background: #f0f2f5; color: #333; }
    header { background: #1a252f; color: #fff; padding: 1.2em 2em; }
    header h1 { margin: 0 0 .3em; font-size: 1.4em; }
    header p  { margin: 0; font-size: .85em; opacity: .8; }
    main   { padding: 1.5em 2em; }
    h2     { color: #34495e; border-bottom: 2px solid #bdc3c7;
             padding-bottom: 4px; margin-top: 1.5em; }
    /* Legend */
    .legend { display: flex; gap: 1.5em; margin-bottom: 1em; flex-wrap: wrap; }
    .legend-item { display: flex; align-items: center; gap: .5em; font-size: .9em; }
    .legend-swatch { width: 28px; height: 6px; border-radius: 3px; }
    .swatch-a { background: #2980b9; }
    .swatch-b { background: #e67e22; }
    /* Summary comparison table */
    table  { border-collapse: collapse; width: 100%; margin-bottom: 1.5em;
             background: #fff; box-shadow: 0 1px 3px rgba(0,0,0,.1);
             border-radius: 6px; overflow: hidden; }
    th     { background: #2c3e50; color: #fff; padding: 9px 14px;
             text-align: left; font-size: .85em; }
    th.a   { background: #2471a3; }
    th.b   { background: #ca6f1e; }
    td     { padding: 7px 14px; border-bottom: 1px solid #ecf0f1; font-size: .84em; }
    tr:last-child td { border-bottom: none; }
    tr:hover td { background: #fef9f0; }
    td.better { color: #1e8449; font-weight: bold; }
    td.worse  { color: #c0392b; font-weight: bold; }
    /* Charts */
    .charts { display: grid; grid-template-columns: 1fr 1fr; gap: 1em; margin-bottom: 1.5em; }
    .chart-box { background: #fff; border-radius: 6px;
                 box-shadow: 0 1px 3px rgba(0,0,0,.1); padding: .5em; }
    .chart-box img { width: 100%; height: auto; display: block; }
    .no-chart { color: #aaa; font-size: .85em; padding: 1em; text-align: center; }
    /* Tabs */
    .tabs       { display: flex; gap: 4px; margin-bottom: 1.5em; flex-wrap: wrap; }
    .tab-btn    { padding: .55em 1.3em; border: none; border-radius: 6px 6px 0 0;
                  background: #dde1e7; color: #555; cursor: pointer; font-size: .9em;
                  font-weight: 600; transition: background .15s; }
    .tab-btn:hover   { background: #c8cdd6; }
    .tab-btn.active  { background: #2c3e50; color: #fff; }
    .tab-panel       { display: none; }
    .tab-panel.active { display: block; }
    .tag-a { display: inline-block; background:#2471a3; color:#fff;
             border-radius:3px; padding:1px 6px; font-size:.75em; }
    .tag-b { display: inline-block; background:#ca6f1e; color:#fff;
             border-radius:3px; padding:1px 6px; font-size:.75em; }
  </style>
</head>
<body>
<header>
  <h1>Session Comparison Report</h1>
  <p>
    <span style="color:#6bb7e0">&#9632;</span> <strong>{{ label_a }}</strong>
    &nbsp;vs&nbsp;
    <span style="color:#f0a040">&#9632;</span> <strong>{{ label_b }}</strong>
    &nbsp;|&nbsp; Generated: {{ generated_at }}
  </p>
</header>
<main>
  <div class="tabs">
    <button class="tab-btn active" onclick="showTab('charts', this)">Charts</button>
    <button class="tab-btn"        onclick="showTab('summary', this)">Summary</button>
  </div>

  <!-- CHARTS -->
  <div id="tab-charts" class="tab-panel active">
    <h2>Metric Comparison Charts</h2>
    <div class="legend">
      <div class="legend-item">
        <div class="legend-swatch swatch-a"></div> <span>{{ label_a }}</span>
      </div>
      <div class="legend-item">
        <div class="legend-swatch swatch-b"></div> <span>{{ label_b }}</span>
      </div>
    </div>
    <div class="charts">
      {% for key, title in [("ram","RAM PSS (KB)"), ("cpu","CPU (%)"),
                             ("batt_level","Battery Level (%)"), ("batt_temp","Battery Temp (°C)")] %}
      <div class="chart-box">
        {% if charts[key] %}
          <img src="data:image/png;base64,{{ charts[key] }}" alt="{{ title }} chart">
        {% else %}
          <p class="no-chart">{{ title }} — no data</p>
        {% endif %}
      </div>
      {% endfor %}
    </div>
  </div>

  <!-- SUMMARY -->
  <div id="tab-summary" class="tab-panel">
    <h2>Per-Package RAM &amp; CPU Summary</h2>
    <table>
      <tr>
        <th>Package</th>
        <th class="a">{{ label_a }} — Avg RAM (KB)</th>
        <th class="a">{{ label_a }} — Peak RAM (KB)</th>
        <th class="a">{{ label_a }} — Avg CPU (%)</th>
        <th class="b">{{ label_b }} — Avg RAM (KB)</th>
        <th class="b">{{ label_b }} — Peak RAM (KB)</th>
        <th class="b">{{ label_b }} — Avg CPU (%)</th>
      </tr>
      {% for row in pkg_table %}
      <tr>
        <td>{{ row.package }}</td>
        <td {% if row.a_avg_ram is not none and row.b_avg_ram is not none %}
              class="{{ 'better' if row.a_avg_ram <= row.b_avg_ram else 'worse' }}"
            {% endif %}>
          {{ row.a_avg_ram if row.a_avg_ram is not none else '—' }}
        </td>
        <td>{{ row.a_peak_ram if row.a_peak_ram is not none else '—' }}</td>
        <td {% if row.a_avg_cpu is not none and row.b_avg_cpu is not none %}
              class="{{ 'better' if row.a_avg_cpu <= row.b_avg_cpu else 'worse' }}"
            {% endif %}>
          {{ row.a_avg_cpu if row.a_avg_cpu is not none else '—' }}
        </td>
        <td {% if row.a_avg_ram is not none and row.b_avg_ram is not none %}
              class="{{ 'better' if row.b_avg_ram <= row.a_avg_ram else 'worse' }}"
            {% endif %}>
          {{ row.b_avg_ram if row.b_avg_ram is not none else '—' }}
        </td>
        <td>{{ row.b_peak_ram if row.b_peak_ram is not none else '—' }}</td>
        <td {% if row.a_avg_cpu is not none and row.b_avg_cpu is not none %}
              class="{{ 'better' if row.b_avg_cpu <= row.a_avg_cpu else 'worse' }}"
            {% endif %}>
          {{ row.b_avg_cpu if row.b_avg_cpu is not none else '—' }}
        </td>
      </tr>
      {% endfor %}
    </table>

    <h2>Device-Level Metrics</h2>
    <table>
      <tr>
        <th>Metric</th>
        <th class="a">{{ label_a }}</th>
        <th class="b">{{ label_b }}</th>
      </tr>
      {% for row in device_table %}
      <tr>
        <td>{{ row.metric }}</td>
        <td>{{ row.a_val }}</td>
        <td>{{ row.b_val }}</td>
      </tr>
      {% endfor %}
    </table>
  </div>
</main>
<script>
function showTab(name, btn) {
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  btn.classList.add('active');
}
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# CSV loading
# ---------------------------------------------------------------------------

def load_csv_records(csv_path: Path) -> List[Dict[str, Any]]:
    """
    Read a telemetry CSV file and return a list of record dicts.

    Numeric fields are cast to ``float`` where possible; all others remain
    strings.  Missing or empty values are stored as ``None``.

    Args:
        csv_path (Path): Path to the telemetry CSV file.

    Returns:
        List[Dict]: Parsed records, one dict per row.

    Raises:
        OSError: If the file cannot be opened.
        csv.Error: If the file is not valid CSV.
    """
    numeric_fields = {
        "ram_pss_kb", "cpu_total_pct", "cpu_user_pct", "cpu_load_1m",
        "batt_level", "batt_temp_c", "batt_voltage_mv",
        "net_rx_kb", "net_tx_kb", "disk_read_kb", "disk_write_kb",
        "thread_count", "fd_count",
    }
    records: List[Dict[str, Any]] = []
    try:
        with open(csv_path, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                parsed: Dict[str, Any] = {}
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
        logger.info("Loaded %d records from '%s'.", len(records), csv_path)
    except (OSError, csv.Error) as exc:
        logger.error("Failed to load CSV '%s': %s", csv_path, exc)
        raise
    return records


# ---------------------------------------------------------------------------
# Internal helpers — statistics
# ---------------------------------------------------------------------------

def _pkg_stats(records: List[Dict]) -> Dict[str, Dict]:
    """
    Compute per-package RAM and CPU statistics.

    Args:
        records: Telemetry record list.

    Returns:
        Dict keyed by package name, each value having keys:
        ``avg_ram``, ``peak_ram``, ``avg_cpu``, ``peak_cpu``,
        ``timestamps``, ``ram_series``, ``cpu_series``.
    """
    buckets: Dict[str, Dict] = defaultdict(lambda: {
        "ram": [], "cpu": [], "timestamps": [],
    })
    for r in records:
        pkg = r.get("package") or "unknown"
        ts_str = r.get("timestamp")
        try:
            ts = datetime.fromisoformat(ts_str) if ts_str else None
        except ValueError:
            ts = None
        if r.get("ram_pss_kb") is not None:
            buckets[pkg]["ram"].append(r["ram_pss_kb"])
        if r.get("cpu_total_pct") is not None:
            buckets[pkg]["cpu"].append(r["cpu_total_pct"])
        if ts is not None:
            buckets[pkg]["timestamps"].append(ts)

    result = {}
    for pkg, data in buckets.items():
        result[pkg] = {
            "avg_ram":  round(sum(data["ram"]) / len(data["ram"])) if data["ram"] else None,
            "peak_ram": round(max(data["ram"])) if data["ram"] else None,
            "avg_cpu":  round(sum(data["cpu"]) / len(data["cpu"]), 1) if data["cpu"] else None,
            "peak_cpu": round(max(data["cpu"]), 1) if data["cpu"] else None,
            "timestamps": data["timestamps"],
            "ram_series": data["ram"],
            "cpu_series": data["cpu"],
        }
    return result


def _device_series(
    records: List[Dict],
    field: str,
) -> Tuple[List[datetime], List[float]]:
    """
    Extract a device-level time series for a scalar metric field.

    Args:
        records: Telemetry records.
        field:   CSV column name (e.g. ``"batt_level"``).

    Returns:
        Tuple of (timestamps, values) with ``None`` entries excluded.
    """
    timestamps, values = [], []
    for r in records:
        val = r.get(field)
        ts_str = r.get("timestamp")
        if val is None or ts_str is None:
            continue
        try:
            ts = datetime.fromisoformat(ts_str)
        except ValueError:
            continue
        timestamps.append(ts)
        values.append(val)
    return timestamps, values


# ---------------------------------------------------------------------------
# Chart generation
# ---------------------------------------------------------------------------

def _fig_to_b64(fig) -> str:
    """Render a Matplotlib figure to a base64-encoded PNG string.

    Uses FigureCanvasAgg directly — thread-safe, no pyplot / GUI backend needed.
    """
    canvas = FigureCanvasAgg(fig)
    buf = io.BytesIO()
    canvas.print_figure(buf, format="png", dpi=120, bbox_inches="tight")
    buf.seek(0)
    return base64.b64encode(buf.read()).decode("ascii")


def _make_overlay_chart(
    a_ts: List[datetime],
    a_vals: List[float],
    b_ts: List[datetime],
    b_vals: List[float],
    label_a: str,
    label_b: str,
    title: str,
    ylabel: str,
) -> Optional[str]:
    """
    Build a single overlaid line chart for two series and return base64 PNG.

    Args:
        a_ts, a_vals: Timestamps and values for session A.
        b_ts, b_vals: Timestamps and values for session B.
        label_a:      Legend label for session A.
        label_b:      Legend label for session B.
        title:        Chart title.
        ylabel:       Y-axis label.

    Returns:
        Base64 PNG string, or ``None`` if both series are empty.
    """
    if not a_vals and not b_vals:
        return None

    fig = Figure(figsize=(7, 3.2))
    fig.patch.set_facecolor("#f8f9fa")
    ax = fig.add_subplot(111)
    ax.set_facecolor("#fdfdfd")

    color_a = "#2980b9"
    color_b = "#e67e22"

    if a_ts and a_vals:
        ax.plot(a_ts, a_vals, color=color_a, linewidth=1.8,
                label=label_a, alpha=0.9)
    if b_ts and b_vals:
        ax.plot(b_ts, b_vals, color=color_b, linewidth=1.8,
                label=label_b, alpha=0.9, linestyle="--")

    ax.set_title(title, fontsize=11, fontweight="bold", pad=8)
    ax.set_ylabel(ylabel, fontsize=9)
    ax.tick_params(axis="both", labelsize=8)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))
    fig.autofmt_xdate(rotation=30, ha="right")
    ax.grid(True, alpha=0.25, linestyle=":")
    ax.legend(fontsize=8, loc="upper right")
    fig.tight_layout()

    return _fig_to_b64(fig)


def generate_comparison_charts(
    a_records: List[Dict],
    b_records: List[Dict],
    label_a: str = "Session A",
    label_b: str = "Session B",
) -> Dict[str, Optional[str]]:
    """
    Produce overlaid comparison charts for RAM, CPU, battery level and temp.

    When multiple packages are present, the chart plots the *first* package
    found in each session that appears in both (for RAM/CPU), and device-level
    data for battery metrics.

    Args:
        a_records: Records from session A.
        b_records: Records from session B.
        label_a:   Legend label for A.
        label_b:   Legend label for B.

    Returns:
        Dict with keys ``"ram"``, ``"cpu"``, ``"batt_level"``, ``"batt_temp"``,
        each mapped to a base64 PNG string or ``None``.
    """
    charts: Dict[str, Optional[str]] = {
        "ram": None, "cpu": None, "batt_level": None, "batt_temp": None,
    }

    if not _MPL_AVAILABLE:
        logger.warning("Matplotlib unavailable — skipping comparison charts.")
        return charts

    stats_a = _pkg_stats(a_records)
    stats_b = _pkg_stats(b_records)

    # ── RAM chart: overlay all shared packages; fall back to any package ──
    all_pkgs = sorted(set(stats_a) | set(stats_b))

    fig_ram = Figure(figsize=(7, 3.2))
    fig_ram.patch.set_facecolor("#f8f9fa")
    ax_ram = fig_ram.add_subplot(111)
    ax_ram.set_facecolor("#fdfdfd")

    fig_cpu = Figure(figsize=(7, 3.2))
    fig_cpu.patch.set_facecolor("#f8f9fa")
    ax_cpu = fig_cpu.add_subplot(111)
    ax_cpu.set_facecolor("#fdfdfd")

    palette_a = ["#2980b9", "#1a5276", "#5dade2"]
    palette_b = ["#e67e22", "#784212", "#f0a040"]

    has_ram = False
    has_cpu = False

    for idx, pkg in enumerate(all_pkgs):
        ca = palette_a[idx % len(palette_a)]
        cb = palette_b[idx % len(palette_b)]
        short = pkg.split(".")[-1][:20]

        if pkg in stats_a and stats_a[pkg]["ram_series"]:
            ax_ram.plot(
                stats_a[pkg]["timestamps"], stats_a[pkg]["ram_series"],
                color=ca, linewidth=1.6,
                label=f"{short} [{label_a}]", alpha=0.9,
            )
            has_ram = True
        if pkg in stats_b and stats_b[pkg]["ram_series"]:
            ax_ram.plot(
                stats_b[pkg]["timestamps"], stats_b[pkg]["ram_series"],
                color=cb, linewidth=1.6,
                label=f"{short} [{label_b}]", alpha=0.9, linestyle="--",
            )
            has_ram = True
        if pkg in stats_a and stats_a[pkg]["cpu_series"]:
            ax_cpu.plot(
                stats_a[pkg]["timestamps"], stats_a[pkg]["cpu_series"],
                color=ca, linewidth=1.6,
                label=f"{short} [{label_a}]", alpha=0.9,
            )
            has_cpu = True
        if pkg in stats_b and stats_b[pkg]["cpu_series"]:
            ax_cpu.plot(
                stats_b[pkg]["timestamps"], stats_b[pkg]["cpu_series"],
                color=cb, linewidth=1.6,
                label=f"{short} [{label_b}]", alpha=0.9, linestyle="--",
            )
            has_cpu = True

    for ax, title, ylabel, has_data in [
        (ax_ram, "RAM PSS Comparison", "RAM PSS (KB)", has_ram),
        (ax_cpu, "CPU Usage Comparison", "CPU (%)", has_cpu),
    ]:
        ax.set_title(title, fontsize=11, fontweight="bold", pad=8)
        ax.set_ylabel(ylabel, fontsize=9)
        ax.tick_params(axis="both", labelsize=8)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))
        ax.grid(True, alpha=0.25, linestyle=":")
        if has_data:
            ax.legend(fontsize=7, loc="upper right", ncol=2)

    fig_ram.autofmt_xdate(rotation=30, ha="right")
    fig_cpu.autofmt_xdate(rotation=30, ha="right")
    fig_ram.tight_layout()
    fig_cpu.tight_layout()

    if has_ram:
        charts["ram"] = _fig_to_b64(fig_ram)

    if has_cpu:
        charts["cpu"] = _fig_to_b64(fig_cpu)

    # ── Battery level chart ──────────────────────────────────────────────
    a_bl_ts, a_bl_vals = _device_series(a_records, "batt_level")
    b_bl_ts, b_bl_vals = _device_series(b_records, "batt_level")
    charts["batt_level"] = _make_overlay_chart(
        a_bl_ts, a_bl_vals, b_bl_ts, b_bl_vals,
        label_a, label_b, "Battery Level Comparison", "Battery (%)",
    )

    # ── Battery temp chart ───────────────────────────────────────────────
    a_bt_ts, a_bt_vals = _device_series(a_records, "batt_temp_c")
    b_bt_ts, b_bt_vals = _device_series(b_records, "batt_temp_c")
    charts["batt_temp"] = _make_overlay_chart(
        a_bt_ts, a_bt_vals, b_bt_ts, b_bt_vals,
        label_a, label_b, "Battery Temp Comparison", "Temp (°C)",
    )

    return charts


# ---------------------------------------------------------------------------
# Summary table builders
# ---------------------------------------------------------------------------

def _build_pkg_table(
    stats_a: Dict[str, Dict],
    stats_b: Dict[str, Dict],
) -> List[Dict]:
    """
    Build the per-package comparison table rows.

    Args:
        stats_a: Output of ``_pkg_stats`` for session A.
        stats_b: Output of ``_pkg_stats`` for session B.

    Returns:
        List of row dicts with keys:
        ``package``, ``a_avg_ram``, ``a_peak_ram``, ``a_avg_cpu``,
        ``b_avg_ram``, ``b_peak_ram``, ``b_avg_cpu``.
    """
    all_pkgs = sorted(set(stats_a) | set(stats_b))
    rows = []
    for pkg in all_pkgs:
        a = stats_a.get(pkg, {})
        b = stats_b.get(pkg, {})
        rows.append({
            "package":    pkg,
            "a_avg_ram":  a.get("avg_ram"),
            "a_peak_ram": a.get("peak_ram"),
            "a_avg_cpu":  a.get("avg_cpu"),
            "b_avg_ram":  b.get("avg_ram"),
            "b_peak_ram": b.get("peak_ram"),
            "b_avg_cpu":  b.get("avg_cpu"),
        })
    return rows


def _build_device_table(
    a_records: List[Dict],
    b_records: List[Dict],
) -> List[Dict]:
    """
    Build the device-level battery metrics comparison rows.

    Args:
        a_records: Records from session A.
        b_records: Records from session B.

    Returns:
        List of row dicts with keys ``metric``, ``a_val``, ``b_val``.
    """
    def _stat(records, field, label):
        vals = [r[field] for r in records if r.get(field) is not None]
        if not vals:
            return "—"
        return f"min {min(vals):.1f}  avg {sum(vals)/len(vals):.1f}  max {max(vals):.1f}"

    def _drain(records):
        vals = [r["batt_level"] for r in records if r.get("batt_level") is not None]
        if len(vals) < 2:
            return "—"
        return f"{vals[0]:.0f}% → {vals[-1]:.0f}%  (Δ {vals[0]-vals[-1]:+.0f}%)"

    rows = [
        {
            "metric": "Battery Level range",
            "a_val": _drain(a_records),
            "b_val": _drain(b_records),
        },
        {
            "metric": "Battery Temp (°C) — min/avg/max",
            "a_val": _stat(a_records, "batt_temp_c", "Temp"),
            "b_val": _stat(b_records, "batt_temp_c", "Temp"),
        },
        {
            "metric": "Sample count",
            "a_val": str(len(a_records)),
            "b_val": str(len(b_records)),
        },
    ]
    return rows


# ---------------------------------------------------------------------------
# Public API — HTML report generator
# ---------------------------------------------------------------------------

def generate_comparison_html(
    a_records: List[Dict],
    b_records: List[Dict],
    label_a: str,
    label_b: str,
    output_path: Path,
) -> None:
    """
    Generate a standalone HTML session-comparison report.

    Args:
        a_records (List[Dict]): Telemetry records for session A.
        b_records (List[Dict]): Telemetry records for session B.
        label_a (str):          Human-readable label for session A.
        label_b (str):          Human-readable label for session B.
        output_path (Path):     Destination HTML file path.

    Raises:
        RuntimeError: If Jinja2 is not installed.
    """
    if not _JINJA2_AVAILABLE:
        raise RuntimeError(
            "Jinja2 is not installed. Run `pip install jinja2`. "
            "Cannot generate comparison HTML report."
        )

    charts = generate_comparison_charts(a_records, b_records, label_a, label_b)
    stats_a = _pkg_stats(a_records)
    stats_b = _pkg_stats(b_records)
    pkg_table = _build_pkg_table(stats_a, stats_b)
    device_table = _build_device_table(a_records, b_records)

    env = Environment(loader=BaseLoader(), autoescape=False)
    template = env.from_string(_COMPARE_TEMPLATE)
    html = template.render(
        label_a=label_a,
        label_b=label_b,
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        charts=charts,
        pkg_table=pkg_table,
        device_table=device_table,
    )

    try:
        output_path.write_text(html, encoding="utf-8")
        logger.info(
            "Comparison report saved → '%s'.", output_path
        )
    except OSError as exc:
        logger.error("Failed to write comparison report to '%s': %s", output_path, exc)
        raise
