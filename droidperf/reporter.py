"""
droidperf/reporter.py
---------------------
Generates CSV, HTML, and PDF telemetry reports from collected metric data.

The HTML report is rendered with Jinja2 using an embedded template and
contains four sections accessible via tab-style navigation:
  1. Insights  — auto-generated plain-language analysis cards
  2. Charts    — embedded Matplotlib base64 PNG charts
  3. Summary   — min/max/avg table per metric
  4. Raw Data  — full sample table
  5. Logcat    — captured crash/ANR events

The PDF report uses reportlab (platypus) and contains the same sections
in a print-ready, single-file format.

Public API:
    save_csv(records, output_path)
    generate_html_report(records, logcat_events, device_id, package_name, output_path)
    generate_pdf_report(records, logcat_events, device_id, package_name, output_path)
"""

import base64
import csv
import io
import logging
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from droidperf.charts import generate_battery_attribution_chart, generate_charts

logger = logging.getLogger(__name__)

try:
    from jinja2 import BaseLoader, Environment

    _JINJA2_AVAILABLE = True
except ImportError:
    _JINJA2_AVAILABLE = False
    logger.warning("Jinja2 not found. HTML report generation will be unavailable.")

try:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import (
        Flowable, HRFlowable, Image, PageBreak, Paragraph,
        SimpleDocTemplate, Spacer, Table, TableStyle,
    )
    _REPORTLAB_AVAILABLE = True
except ImportError:
    _REPORTLAB_AVAILABLE = False
    logger.warning("reportlab not found. PDF report generation will be unavailable.")

# ---------------------------------------------------------------------------
# Embedded Jinja2 HTML template
# ---------------------------------------------------------------------------

_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>ADB Telemetry Report — {{ meta.package }}</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; }
    body   { font-family: Arial, sans-serif; margin: 0; background: #f0f2f5; color: #333; }
    header { background: #2c3e50; color: #fff; padding: 1.2em 2em; }
    header h1 { margin: 0 0 .3em; font-size: 1.5em; }
    header p  { margin: 0; font-size: .85em; opacity: .8; }
    main   { padding: 1.5em 2em; }

    /* ── Tab navigation ── */
    .tabs        { display: flex; gap: 4px; margin-bottom: 1.5em; flex-wrap: wrap; }
    .tab-btn     { padding: .55em 1.3em; border: none; border-radius: 6px 6px 0 0;
                   background: #dde1e7; color: #555; cursor: pointer; font-size: .9em;
                   font-weight: 600; transition: background .15s; }
    .tab-btn:hover   { background: #c8cdd6; }
    .tab-btn.active  { background: #2980b9; color: #fff; }
    .tab-panel       { display: none; }
    .tab-panel.active { display: block; }

    /* ── Insight cards ── */
    .cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
             gap: 1em; margin-bottom: .5em; }
    .card  { background: #fff; border-radius: 8px; padding: 1.1em 1.3em;
             box-shadow: 0 2px 6px rgba(0,0,0,.09); border-left: 5px solid #bdc3c7;
             position: relative; }
    .card.red    { border-left-color: #e74c3c; }
    .card.orange { border-left-color: #e67e22; }
    .card.green  { border-left-color: #27ae60; }
    .card.blue   { border-left-color: #2980b9; }
    .card.gray   { border-left-color: #95a5a6; }
    .card-label  { font-size: .68em; text-transform: uppercase; letter-spacing: .1em;
                   color: #999; margin-bottom: .4em; }
    .card-pkg    { font-size: .8em; font-family: monospace; font-weight: 700;
                   color: #555; margin-bottom: .5em; word-break: break-all; }
    .card-value  { font-size: 1.55em; font-weight: bold; color: #2c3e50; line-height: 1.1; }
    .card-detail { font-size: .76em; color: #aaa; margin-top: .35em; }
    .narrative   { background: #fff; border-radius: 8px; padding: 1.3em 1.5em;
                   box-shadow: 0 2px 6px rgba(0,0,0,.09); line-height: 1.8;
                   font-size: .95em; color: #444; }
    .narrative b { color: #2c3e50; }

    /* ── Charts ── */
    .charts    { display: grid; grid-template-columns: 1fr 1fr; gap: 1em; margin-bottom: 1.5em; }
    .chart-box { background: #fff; border-radius: 6px; box-shadow: 0 1px 3px rgba(0,0,0,.1);
                 padding: .5em; }
    .chart-box img { width: 100%; height: auto; display: block; }
    .no-chart  { color: #aaa; font-size: .85em; padding: 1em; text-align: center; }

    /* ── Tables ── */
    table  { border-collapse: collapse; width: 100%; margin-bottom: 1.5em;
             background: #fff; box-shadow: 0 1px 3px rgba(0,0,0,.1); border-radius: 6px;
             overflow: hidden; }
    th     { background: #2980b9; color: #fff; padding: 9px 14px; text-align: left;
             font-size: .85em; }
    td     { padding: 7px 14px; border-bottom: 1px solid #ecf0f1; font-size: .84em; }
    tr:last-child td { border-bottom: none; }
    tr:hover td { background: #eaf4fb; }
    .warn  { color: #c0392b; font-family: monospace; font-size: .8em; }
    .none  { color: #aaa; font-style: italic; }
    h2     { color: #34495e; border-bottom: 2px solid #bdc3c7; padding-bottom: 4px;
             margin-top: 0; }
    .sortable-th       { cursor: pointer; user-select: none; white-space: nowrap; }
    .sortable-th:hover { background: #1a6fa0; }
    .sort-active       { background: #1d6fa4; }
    .sort-icon         { font-size: .8em; opacity: .7; margin-left: .3em; }
  </style>
</head>
<body>
<header>
  <h1>ADB Telemetry &amp; Health Report</h1>
  <p>
    Device: <strong>{{ meta.device_id }}</strong> &nbsp;|&nbsp;
    Package(s): <strong>{{ meta.package }}</strong> &nbsp;|&nbsp;
    {{ records | length }} samples &nbsp;|&nbsp;
    Generated: {{ meta.generated_at }}
  </p>
</header>
<main>

  <!-- Tab buttons -->
  <div class="tabs">
    <button class="tab-btn active" onclick="showTab('insights', this)">Insights</button>
    <button class="tab-btn"        onclick="showTab('charts',   this)">Charts</button>
    <button class="tab-btn"        onclick="showTab('battery',  this)">
      Battery Usage {% if battery_attribution %}({{ battery_attribution | length }}){% endif %}
    </button>
    <button class="tab-btn"        onclick="showTab('summary',  this)">Summary</button>
    <button class="tab-btn"        onclick="showTab('rawdata',  this)">Raw Data</button>
    <button class="tab-btn"        onclick="showTab('logcat',   this)">
      Logcat {% if logcat_events %}({{ logcat_events | length }}){% endif %}
    </button>
  </div>

  <!-- ════════════════ INSIGHTS TAB ════════════════ -->
  <div id="tab-insights" class="tab-panel active">
    <h2>Session Insights</h2>

    <div class="cards">
      {% for card in insights %}
      <div class="card {{ card.color }}">
        <div class="card-label">{{ card.label }}</div>
        <div class="card-pkg">{{ card.package }}</div>
        <div class="card-value">{{ card.value }}</div>
        {% if card.detail %}<div class="card-detail">{{ card.detail }}</div>{% endif %}
      </div>
      {% endfor %}
    </div>

    <br>
    <div class="narrative">
      {{ narrative }}
    </div>
  </div>

  <!-- ════════════════ CHARTS TAB ════════════════ -->
  <div id="tab-charts" class="tab-panel">
    <h2>Charts</h2>
    <div class="charts">
      {% for key in ["ram", "cpu", "batt_level", "batt_temp"] %}
      <div class="chart-box">
        {% if charts[key] %}
          <img src="data:image/png;base64,{{ charts[key] }}" alt="{{ key }} chart">
        {% else %}
          <p class="no-chart">Chart unavailable (no data or Matplotlib not installed)</p>
        {% endif %}
      </div>
      {% endfor %}
    </div>
  </div>

  <!-- ════════════════ BATTERY ATTRIBUTION TAB ════════════════ -->
  <div id="tab-battery" class="tab-panel">
    <h2>Battery Attribution</h2>

    {% if battery_attribution %}
    <p style="font-size:.85em;color:#666;margin-top:-.5em;">
      Estimated power consumption per app and hardware component during this
      session, derived from <code>dumpsys batterystats</code>.
      Hardware components (Screen, Cell&nbsp;standby, etc.) are shown in grey.
      {% if not batt_stats_reset %}
      <span style="color:#e67e22;">&#9432; Battery stats were not reset at session
      start — figures may include usage from before this session.</span>
      {% endif %}
    </p>

    {% if batt_chart %}
    <div class="chart-box" style="margin-bottom:1.2em;">
      <img src="data:image/png;base64,{{ batt_chart }}" alt="Battery attribution chart">
    </div>
    {% endif %}

    <table>
      <tr>
        <th>App / Component</th>
        <th style="text-align:right;">mAh</th>
        <th style="text-align:right;">Share</th>
        <th>Type</th>
      </tr>
      {% set total_mah = battery_attribution.values() | sum %}
      {% for label, mah in battery_attribution.items() %}
      <tr>
        <td style="font-family:monospace;font-size:.82em;">{{ label }}</td>
        <td style="text-align:right;">{{ "%.2f"|format(mah) }}</td>
        <td style="text-align:right;">
          {% if total_mah > 0 %}
            {{ "%.1f"|format(mah / total_mah * 100) }}%
          {% else %}—{% endif %}
        </td>
        <td style="font-size:.8em;color:#888;">
          {% set lbl_lower = label | lower %}
          {% if lbl_lower in ["screen","cell","cell standby","wifi","bluetooth","idle","radio","sensors","flashlight","camera","audio","video","phone","modem","others"] %}
            Hardware / System
          {% elif label.startswith("uid:") %}
            Unknown UID
          {% else %}
            App
          {% endif %}
        </td>
      </tr>
      {% endfor %}
      <tr style="font-weight:bold;background:#f0f2f5;">
        <td>Total</td>
        <td style="text-align:right;">{{ "%.2f"|format(total_mah) }}</td>
        <td style="text-align:right;">100%</td>
        <td></td>
      </tr>
    </table>

    {% else %}
    <p style="color:#aaa;font-style:italic;">
      Battery attribution data is unavailable for this session.<br>
      This feature requires <code>dumpsys batterystats</code> access on the
      device. Some devices need USB debugging with ADB root enabled, or the
      data may be empty when the session was very short.
    </p>
    {% endif %}
  </div>

  <!-- ════════════════ SUMMARY TAB ════════════════ -->
  <div id="tab-summary" class="tab-panel">
    <h2>Metric Summary (all packages combined)</h2>
    <table>
      <tr><th>Metric</th><th>Min</th><th>Max</th><th>Avg</th></tr>
      {% for row in summary %}
      <tr>
        <td>{{ row.metric }}</td>
        <td>{{ row.min }}</td><td>{{ row.max }}</td><td>{{ row.avg }}</td>
      </tr>
      {% endfor %}
    </table>

    {% if pkg_summary %}
    <h2>Per-Package RAM &amp; CPU Averages</h2>
    <p style="font-size:.82em;color:#888;margin-top:-.5em;">
      Click any column header to re-sort. Default: highest average RAM first.
      {% if not cpu_data_available %}
      &nbsp;&#9432; Per-process CPU data was not available for this session
      (likely because the monitored processes did not appear in <code>top</code>
      output and <code>dumpsys cpuinfo</code> was unavailable).
      System-level CPU metrics are shown in the Summary table above.
      {% endif %}
    </p>
    <table id="pkg-table">
      <tr>
        <th onclick="sortPkgTable(0, 'str')"  class="sortable-th">Package <span class="sort-icon">&#8645;</span></th>
        <th onclick="sortPkgTable(1, 'num')"  class="sortable-th sort-active">Avg RAM PSS (KB) <span class="sort-icon">&#9660;</span></th>
        <th onclick="sortPkgTable(2, 'num')"  class="sortable-th">Peak RAM PSS (KB) <span class="sort-icon">&#8645;</span></th>
        <th onclick="sortPkgTable(3, 'num')"  class="sortable-th">Avg CPU (%) <span class="sort-icon">&#8645;</span></th>
        <th onclick="sortPkgTable(4, 'num')"  class="sortable-th">Peak CPU (%) <span class="sort-icon">&#8645;</span></th>
      </tr>
      {% for row in pkg_summary %}
      <tr>
        <td>{{ row.package }}</td>
        <td>{{ row.avg_ram  if row.avg_ram  is not none else '-' }}</td>
        <td>{{ row.peak_ram if row.peak_ram is not none else '-' }}</td>
        <td>{{ row.avg_cpu  if row.avg_cpu  is not none else '—' }}</td>
        <td>{{ row.peak_cpu if row.peak_cpu is not none else '—' }}</td>
      </tr>
      {% endfor %}
    </table>
    {% endif %}
  </div>

  <!-- ════════════════ RAW DATA TAB ════════════════ -->
  <div id="tab-rawdata" class="tab-panel">
    <h2>Raw Samples (<span id="visible-count">{{ records | length }}</span> / {{ records | length }})</h2>

    <!-- Time range filter -->
    <div id="time-filter" style="background:#fff;border-radius:8px;padding:10px 14px;
         margin-bottom:12px;box-shadow:0 1px 3px rgba(0,0,0,.1);display:flex;
         gap:12px;align-items:center;flex-wrap:wrap;">
      <span style="font-size:.85em;font-weight:600;color:#555;">Time Range:</span>
      <label style="font-size:.82em;color:#666;">
        From&nbsp;
        <input type="datetime-local" id="ts-from" step="1"
               style="font-size:.82em;padding:3px 6px;border:1px solid #ccc;border-radius:4px;">
      </label>
      <label style="font-size:.82em;color:#666;">
        To&nbsp;
        <input type="datetime-local" id="ts-to" step="1"
               style="font-size:.82em;padding:3px 6px;border:1px solid #ccc;border-radius:4px;">
      </label>
      <button onclick="applyTimeFilter()"
              style="padding:4px 14px;font-size:.82em;border:none;border-radius:4px;
                     background:#2980b9;color:#fff;cursor:pointer;">Apply</button>
      <button onclick="clearTimeFilter()"
              style="padding:4px 10px;font-size:.82em;border:1px solid #ccc;
                     border-radius:4px;background:#fff;cursor:pointer;">Clear</button>
    </div>

    <table id="raw-table">
      <tr>
        <th>Timestamp</th><th>Device</th><th>Package</th>
        <th>RAM PSS (KB)</th><th>CPU (%)</th><th>Sys User CPU (%)</th><th>Load 1m</th>
        <th>Battery (%)</th><th>Temp (°C)</th><th>Voltage (mV)</th><th>Status</th>
      </tr>
      {% for r in records %}
      <tr data-ts="{{ r.timestamp }}">
        <td>{{ r.timestamp }}</td>
        <td>{{ r.get('device_id', '-') }}</td>
        <td>{{ r.get('package', '-') }}</td>
        <td>{{ r.ram_pss_kb      if r.ram_pss_kb      is not none else '-' }}</td>
        <td>{{ r.cpu_total_pct   if r.cpu_total_pct   is not none else '—' }}</td>
        <td>{{ r.cpu_user_pct    if r.cpu_user_pct    is not none else '-' }}</td>
        <td>{{ r.cpu_load_1m     if r.cpu_load_1m     is not none else '-' }}</td>
        <td>{{ r.batt_level      if r.batt_level      is not none else '-' }}</td>
        <td>{{ r.batt_temp_c     if r.batt_temp_c     is not none else '-' }}</td>
        <td>{{ r.batt_voltage_mv if r.batt_voltage_mv is not none else '-' }}</td>
        <td>{{ r.batt_status     if r.batt_status     else '-' }}</td>
      </tr>
      {% endfor %}
    </table>
  </div>

  <!-- ════════════════ LOGCAT TAB ════════════════ -->
  <div id="tab-logcat" class="tab-panel">
    {% if logcat_events %}
    <h2>Logcat Events ({{ logcat_events | length }})</h2>
    <table>
      <tr><th>Timestamp</th><th>Log Line</th></tr>
      {% for e in logcat_events %}
      <tr><td>{{ e.timestamp }}</td><td class="warn">{{ e.line }}</td></tr>
      {% endfor %}
    </table>
    {% else %}
    <h2>Logcat Events</h2>
    <p class="none">No crash or ANR events were captured during this session.</p>
    {% endif %}
  </div>

</main>

<script>
/* ── Tab switching ── */
function showTab(name, btn) {
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  btn.classList.add('active');
}

/* ── Time range filter ── */
function applyTimeFilter() {
  var fromVal = document.getElementById('ts-from').value;
  var toVal   = document.getElementById('ts-to').value;
  var fromMs  = fromVal ? new Date(fromVal).getTime() : -Infinity;
  var toMs    = toVal   ? new Date(toVal).getTime()   :  Infinity;
  var rows    = document.querySelectorAll('#raw-table tr[data-ts]');
  var visible = 0;
  rows.forEach(function(row) {
    var ts = row.getAttribute('data-ts');
    // timestamps are ISO-8601: "2024-01-15 10:30:00" → replace space with T
    var rowMs = new Date(ts.replace(' ', 'T')).getTime();
    if (rowMs >= fromMs && rowMs <= toMs) {
      row.style.display = '';
      visible++;
    } else {
      row.style.display = 'none';
    }
  });
  var countEl = document.getElementById('visible-count');
  if (countEl) countEl.textContent = visible;
}

function clearTimeFilter() {
  document.getElementById('ts-from').value = '';
  document.getElementById('ts-to').value   = '';
  var rows = document.querySelectorAll('#raw-table tr[data-ts]');
  rows.forEach(function(row) { row.style.display = ''; });
  var countEl = document.getElementById('visible-count');
  if (countEl) countEl.textContent = rows.length;
}

/* Pre-fill time inputs with session min/max on load */
document.addEventListener('DOMContentLoaded', function() {
  var rows = document.querySelectorAll('#raw-table tr[data-ts]');
  if (!rows.length) return;
  var timestamps = Array.from(rows).map(function(r) {
    return new Date(r.getAttribute('data-ts').replace(' ', 'T')).getTime();
  }).filter(function(t) { return !isNaN(t); });
  if (!timestamps.length) return;
  function toLocalInput(ms) {
    var d = new Date(ms);
    var pad = function(n) { return String(n).padStart(2, '0'); };
    return d.getFullYear() + '-' + pad(d.getMonth()+1) + '-' + pad(d.getDate()) +
           'T' + pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
  }
  document.getElementById('ts-from').value = toLocalInput(Math.min.apply(null, timestamps));
  document.getElementById('ts-to').value   = toLocalInput(Math.max.apply(null, timestamps));
});

/* ── Per-package table sort ── */
(function () {
  var _col = 1;      // default: Avg RAM column
  var _asc = false;  // default: descending (highest first)

  window.sortPkgTable = function (col, type) {
    var table = document.getElementById('pkg-table');
    if (!table) return;

    // Toggle direction when clicking the same column again.
    _asc = (col === _col) ? !_asc : false;
    _col = col;

    // Update header icons and active highlight.
    var headers = table.querySelectorAll('th');
    headers.forEach(function (th, i) {
      th.classList.remove('sort-active');
      var icon = th.querySelector('.sort-icon');
      if (icon) icon.innerHTML = '&#8645;';
    });
    headers[col].classList.add('sort-active');
    var activeIcon = headers[col].querySelector('.sort-icon');
    if (activeIcon) activeIcon.innerHTML = _asc ? '&#9650;' : '&#9660;';

    // Collect body rows (skip header row 0).
    var rows = Array.from(table.rows).slice(1);
    rows.sort(function (a, b) {
      var va = a.cells[col].textContent.trim();
      var vb = b.cells[col].textContent.trim();
      var result;
      if (type === 'num') {
        var na = parseFloat(va.replace(/,/g, ''));
        var nb = parseFloat(vb.replace(/,/g, ''));
        // Treat '-' (missing data) as -1 so it always sinks to the bottom.
        na = isNaN(na) ? -1 : na;
        nb = isNaN(nb) ? -1 : nb;
        result = na - nb;
      } else {
        result = va.localeCompare(vb);
      }
      return _asc ? result : -result;
    });

    // Re-attach rows in sorted order.
    var tbody = table.tBodies[0] || table;
    rows.forEach(function (r) { tbody.appendChild(r); });
  };

  // Apply default sort (Avg RAM descending) after DOM is ready.
  document.addEventListener('DOMContentLoaded', function () {
    window.sortPkgTable(1, 'num');
  });
})();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Internal helpers — summary
# ---------------------------------------------------------------------------

def _compute_summary(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Compute min, max, and average for each numeric metric across all records.

    Args:
        records (List[Dict]): Collected metric rows.

    Returns:
        List[Dict]: One dict per metric with keys ``metric``, ``min``,
                    ``max``, ``avg``.
    """
    numeric_fields = {
        "RAM PSS (KB)": "ram_pss_kb",
        "CPU Total (%)": "cpu_total_pct",
        "Load 1m": "cpu_load_1m",
        "Battery (%)": "batt_level",
        "Temperature (°C)": "batt_temp_c",
        "Voltage (mV)": "batt_voltage_mv",
    }
    summary = []
    for label, field in numeric_fields.items():
        values = [r[field] for r in records if r.get(field) is not None]
        if not values:
            continue
        summary.append({
            "metric": label,
            "min": round(min(values), 2),
            "max": round(max(values), 2),
            "avg": round(sum(values) / len(values), 2),
        })
    return summary


def _compute_pkg_summary(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Compute per-package RAM and CPU statistics, sorted by average RAM descending.

    Args:
        records (List[Dict]): Collected metric rows (must include a ``package`` key).

    Returns:
        List[Dict]: One dict per package with keys ``package``, ``avg_ram``,
                    ``peak_ram``, ``avg_cpu``, ``peak_cpu``.
    """
    buckets: Dict[str, Dict[str, list]] = defaultdict(lambda: {"ram": [], "cpu": []})
    for r in records:
        pkg = r.get("package") or "unknown"
        if r.get("ram_pss_kb") is not None:
            buckets[pkg]["ram"].append(r["ram_pss_kb"])
        if r.get("cpu_total_pct") is not None:
            buckets[pkg]["cpu"].append(r["cpu_total_pct"])

    rows = []
    for pkg, data in buckets.items():
        avg_ram = round(sum(data["ram"]) / len(data["ram"])) if data["ram"] else None
        peak_ram = round(max(data["ram"])) if data["ram"] else None
        avg_cpu = round(sum(data["cpu"]) / len(data["cpu"]), 1) if data["cpu"] else None
        peak_cpu = round(max(data["cpu"]), 1) if data["cpu"] else None
        rows.append({
            "package": pkg,
            "avg_ram": avg_ram,
            "peak_ram": peak_ram,
            "avg_cpu": avg_cpu,
            "peak_cpu": peak_cpu,
        })

    return sorted(rows, key=lambda x: (x["avg_ram"] or 0), reverse=True)


# ---------------------------------------------------------------------------
# Internal helpers — insights
# ---------------------------------------------------------------------------

def _compute_insights(
    records: List[Dict[str, Any]],
    logcat_events: List[Dict],
) -> tuple:
    """
    Generate insight cards and a narrative paragraph from telemetry records.

    Analyzes per-package RAM and CPU data plus device-level battery metrics
    to produce human-readable findings.

    Args:
        records (List[Dict]):       Collected metric rows.
        logcat_events (List[Dict]): Logcat crash/ANR events.

    Returns:
        Tuple[List[Dict], str]: (cards, narrative_html)
            cards     — list of card dicts for the Jinja2 template.
            narrative — HTML string with a plain-language summary paragraph.
    """
    if not records:
        return [], "<p>No data collected during this session.</p>"

    # ── Per-package buckets ──────────────────────────────────────────────
    buckets: Dict[str, Dict[str, list]] = defaultdict(lambda: {"ram": [], "cpu": []})
    for r in records:
        pkg = r.get("package") or "unknown"
        if r.get("ram_pss_kb") is not None:
            buckets[pkg]["ram"].append(r["ram_pss_kb"])
        if r.get("cpu_total_pct") is not None:
            buckets[pkg]["cpu"].append(r["cpu_total_pct"])

    pkg_avg_ram = {p: sum(d["ram"]) / len(d["ram"]) for p, d in buckets.items() if d["ram"]}
    pkg_avg_cpu = {p: sum(d["cpu"]) / len(d["cpu"]) for p, d in buckets.items() if d["cpu"]}
    pkg_peak_ram = {p: max(d["ram"]) for p, d in buckets.items() if d["ram"]}

    # ── Battery records ──────────────────────────────────────────────────
    batt_records = [r for r in records if r.get("batt_level") is not None]
    start_batt = batt_records[0]["batt_level"] if batt_records else None
    end_batt = batt_records[-1]["batt_level"] if batt_records else None
    batt_drain = (start_batt - end_batt) if (start_batt is not None and end_batt is not None) else None

    temp_records = [r for r in records if r.get("batt_temp_c") is not None]
    start_temp = temp_records[0]["batt_temp_c"] if temp_records else None
    end_temp = temp_records[-1]["batt_temp_c"] if temp_records else None
    temp_rise = (end_temp - start_temp) if (start_temp is not None and end_temp is not None) else None

    # ── Session duration ─────────────────────────────────────────────────
    try:
        t_start = datetime.fromisoformat(records[0]["timestamp"])
        t_end = datetime.fromisoformat(records[-1]["timestamp"])
        duration_sec = (t_end - t_start).total_seconds()
        duration_str = f"{int(duration_sec // 60)}m {int(duration_sec % 60)}s"
        drain_per_min = (batt_drain / (duration_sec / 60)) if (batt_drain is not None and duration_sec > 0) else None
    except (ValueError, KeyError, ZeroDivisionError):
        duration_str = "unknown"
        drain_per_min = None

    # ── Build cards ──────────────────────────────────────────────────────
    cards = []

    # Session overview
    cards.append({
        "color": "blue",
        "label": "Session Duration",
        "package": f"{len(buckets)} package(s) monitored",
        "value": duration_str,
        "detail": f"{len(records)} total samples collected",
    })

    # Highest RAM
    if pkg_avg_ram:
        top_ram_pkg = max(pkg_avg_ram, key=pkg_avg_ram.get)
        cards.append({
            "color": "red",
            "label": "Highest RAM Usage",
            "package": top_ram_pkg,
            "value": f"{pkg_avg_ram[top_ram_pkg]:,.0f} KB avg",
            "detail": f"Peak: {pkg_peak_ram[top_ram_pkg]:,.0f} KB",
        })

    # Lowest RAM (only meaningful if >1 package)
    if len(pkg_avg_ram) > 1:
        low_ram_pkg = min(pkg_avg_ram, key=pkg_avg_ram.get)
        cards.append({
            "color": "green",
            "label": "Lowest RAM Usage",
            "package": low_ram_pkg,
            "value": f"{pkg_avg_ram[low_ram_pkg]:,.0f} KB avg",
            "detail": f"Peak: {pkg_peak_ram[low_ram_pkg]:,.0f} KB",
        })

    # Highest CPU
    if pkg_avg_cpu:
        top_cpu_pkg = max(pkg_avg_cpu, key=pkg_avg_cpu.get)
        cards.append({
            "color": "orange",
            "label": "Most CPU Intensive",
            "package": top_cpu_pkg,
            "value": f"{pkg_avg_cpu[top_cpu_pkg]:.1f}% avg CPU",
            "detail": f"Peak: {max(buckets[top_cpu_pkg]['cpu']):.1f}%",
        })

    # Lowest CPU (only meaningful if >1 package)
    if len(pkg_avg_cpu) > 1:
        low_cpu_pkg = min(pkg_avg_cpu, key=pkg_avg_cpu.get)
        cards.append({
            "color": "green",
            "label": "Least CPU Intensive",
            "package": low_cpu_pkg,
            "value": f"{pkg_avg_cpu[low_cpu_pkg]:.1f}% avg CPU",
            "detail": "",
        })

    # Battery drain
    if batt_drain is not None:
        drain_color = "red" if batt_drain > 10 else "orange" if batt_drain > 3 else "green"
        drain_sign = "-" if batt_drain >= 0 else "+"
        cards.append({
            "color": drain_color,
            "label": "Battery Drain",
            "package": "Device-level",
            "value": f"{drain_sign}{abs(batt_drain):.0f}%",
            "detail": (
                f"{drain_per_min:.2f}% / min  |  "
                f"{start_batt}% → {end_batt}%"
            ) if drain_per_min is not None else f"{start_batt}% → {end_batt}%",
        })

    # Temperature change
    if temp_rise is not None:
        temp_color = "red" if temp_rise > 3 else "orange" if temp_rise > 1 else "green"
        temp_sign = "+" if temp_rise >= 0 else ""
        cards.append({
            "color": temp_color,
            "label": "Temperature Change",
            "package": "Device-level",
            "value": f"{temp_sign}{temp_rise:.1f} °C",
            "detail": f"{start_temp:.1f} °C  →  {end_temp:.1f} °C",
        })

    # Logcat events
    event_color = "red" if logcat_events else "green"
    cards.append({
        "color": event_color,
        "label": "Crash / ANR Events",
        "package": "All packages",
        "value": str(len(logcat_events)),
        "detail": "Check the Logcat tab for details" if logcat_events else "No issues detected",
    })

    # ── Build narrative ──────────────────────────────────────────────────
    parts = []

    if duration_str != "unknown":
        parts.append(
            f"The monitoring session lasted <b>{duration_str}</b> and collected "
            f"<b>{len(records)}</b> samples across <b>{len(buckets)}</b> package(s)."
        )

    if pkg_avg_ram:
        top = max(pkg_avg_ram, key=pkg_avg_ram.get)
        parts.append(
            f"<b>{top}</b> consumed the most RAM on average "
            f"(<b>{pkg_avg_ram[top]:,.0f} KB PSS</b>, "
            f"peak {pkg_peak_ram[top]:,.0f} KB)."
        )
        if len(pkg_avg_ram) > 1:
            low = min(pkg_avg_ram, key=pkg_avg_ram.get)
            parts.append(
                f"<b>{low}</b> was the most memory-efficient app "
                f"(<b>{pkg_avg_ram[low]:,.0f} KB PSS</b> on average)."
            )

    if pkg_avg_cpu:
        top = max(pkg_avg_cpu, key=pkg_avg_cpu.get)
        parts.append(
            f"<b>{top}</b> was the most CPU-intensive process "
            f"(average <b>{pkg_avg_cpu[top]:.1f}%</b>, "
            f"peak {max(buckets[top]['cpu']):.1f}%)."
        )
        if len(pkg_avg_cpu) > 1:
            low = min(pkg_avg_cpu, key=pkg_avg_cpu.get)
            parts.append(
                f"<b>{low}</b> had the lightest CPU footprint "
                f"(<b>{pkg_avg_cpu[low]:.1f}%</b> average)."
            )

    if batt_drain is not None:
        if batt_drain > 0:
            drain_desc = (
                f"Battery dropped by <b>{batt_drain:.0f}%</b> "
                f"({start_batt}% &rarr; {end_batt}%)"
            )
            if drain_per_min:
                drain_desc += f", at a rate of <b>{drain_per_min:.2f}% per minute</b>"
            parts.append(drain_desc + ".")
        else:
            parts.append(f"Battery level remained stable or increased during the session.")

    if temp_rise is not None:
        if temp_rise > 2:
            parts.append(
                f"Device temperature rose by <b>{temp_rise:.1f} °C</b> "
                f"({start_temp:.1f} °C &rarr; {end_temp:.1f} °C), "
                f"which may indicate thermal stress."
            )
        else:
            parts.append(
                f"Device temperature remained stable "
                f"({start_temp:.1f} °C &rarr; {end_temp:.1f} °C)."
            )

    if logcat_events:
        parts.append(
            f"<b style='color:#e74c3c'>{len(logcat_events)} crash/ANR event(s)</b> "
            f"were captured during the session — review the Logcat tab for details."
        )
    else:
        parts.append("No crash or ANR events were detected during the session.")

    narrative = " ".join(parts)
    return cards, narrative


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def save_csv(records: List[Dict[str, Any]], output_path: Path) -> None:
    """
    Write collected metric records to a CSV file.

    Args:
        records (List[Dict]): Rows of metric data (all sharing the same keys).
        output_path (Path):   Destination CSV file path.
    """
    if not records:
        logger.warning("No records to write — CSV will not be created.")
        return
    try:
        with open(output_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=records[0].keys())
            writer.writeheader()
            writer.writerows(records)
        logger.info("CSV saved → '%s' (%d rows).", output_path, len(records))
    except OSError as exc:
        logger.error("Failed to write CSV to '%s': %s", output_path, exc)


def generate_html_report(
    records: List[Dict[str, Any]],
    logcat_events: List[Dict[str, str]],
    device_id: str,
    package_name: str,
    output_path: Path,
    battery_attribution: Optional[Dict[str, float]] = None,
) -> None:
    """
    Render a standalone HTML telemetry report with Insights, Charts,
    Battery Usage, Summary, Raw Data, and Logcat tabs.

    Args:
        records (List[Dict]):              Collected metric rows.
        logcat_events (List[Dict]):        Captured logcat crash/ANR lines.
        device_id (str):                   Target device serial number.
        package_name (str):                Monitored package name (label only).
        output_path (Path):                Destination HTML file path.
        battery_attribution (Dict | None): Per-app mAh consumption from
            ``collectors.battery_stats.get_battery_attribution``.
            ``None`` or empty dict hides the Battery Usage tab content.
    """
    if not _JINJA2_AVAILABLE:
        logger.error(
            "Jinja2 is not installed. Run `pip install jinja2`. "
            "Skipping HTML report."
        )
        return

    meta = {
        "device_id": device_id,
        "package": package_name,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    summary = _compute_summary(records)
    pkg_summary = _compute_pkg_summary(records)
    charts = generate_charts(records)
    insights, narrative = _compute_insights(records, logcat_events)

    # Flag used by the template to show a note when per-process CPU was not
    # collected (cpu_total_pct is None for all records).
    cpu_data_available = any(r.get("cpu_total_pct") is not None for r in records)

    # Generate battery attribution bar chart (None when attribution is empty).
    batt_attr = battery_attribution or {}
    batt_chart = generate_battery_attribution_chart(batt_attr) if batt_attr else None

    env = Environment(loader=BaseLoader(), autoescape=False)
    template = env.from_string(_HTML_TEMPLATE)
    html = template.render(
        meta=meta,
        summary=summary,
        pkg_summary=pkg_summary,
        records=records,
        logcat_events=logcat_events,
        charts=charts,
        insights=insights,
        narrative=narrative,
        cpu_data_available=cpu_data_available,
        battery_attribution=batt_attr,
        batt_chart=batt_chart,
        batt_stats_reset=True,  # reset is always attempted in MonitorEngine.start()
    )

    try:
        output_path.write_text(html, encoding="utf-8")
        logger.info("HTML report saved → '%s'.", output_path)
    except OSError as exc:
        logger.error("Failed to write HTML report to '%s': %s", output_path, exc)


# ---------------------------------------------------------------------------
# PDF report helpers
# ---------------------------------------------------------------------------

def _strip_html(text: str) -> str:
    """
    Remove HTML tags from *text* for plain-text PDF paragraphs.

    Args:
        text (str): HTML string.

    Returns:
        str: Plain text with tags removed and common entities replaced.
    """
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&rarr;", "→").replace("&amp;", "&").replace("&nbsp;", " ")
    return text.strip()


def _b64_to_image(b64_str: str, width: float, height: float) -> Optional["Image"]:
    """
    Decode a base64 PNG string and return a reportlab ``Image`` flowable.

    Args:
        b64_str (str):  Base64-encoded PNG data.
        width (float):  Target width in reportlab units (points).
        height (float): Target height in reportlab units (points).

    Returns:
        Image flowable, or ``None`` if decoding fails.
    """
    try:
        raw = base64.b64decode(b64_str)
        buf = io.BytesIO(raw)
        return Image(buf, width=width, height=height)
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("Could not decode chart image for PDF: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Public API — PDF report
# ---------------------------------------------------------------------------

def generate_pdf_report(
    records: List[Dict[str, Any]],
    logcat_events: List[Dict[str, str]],
    device_id: str,
    package_name: str,
    output_path: Path,
) -> None:
    """
    Render a print-ready PDF telemetry report using reportlab.

    The PDF contains four sections:
      1. Header  — device, package, generation timestamp
      2. Insights — narrative paragraph + per-metric summary table
      3. Charts  — RAM, CPU, Battery level, Battery temp (2×2 grid)
      4. Logcat  — crash/ANR event table (omitted if empty)

    Args:
        records (List[Dict]):        Collected metric rows.
        logcat_events (List[Dict]):  Captured logcat crash/ANR lines.
        device_id (str):             Target device serial number.
        package_name (str):          Monitored package name (label only).
        output_path (Path):          Destination PDF file path.
    """
    if not _REPORTLAB_AVAILABLE:
        logger.error(
            "reportlab is not installed. Run `pip install reportlab`. "
            "Skipping PDF report."
        )
        return

    # ── Styles ────────────────────────────────────────────────────────────
    base_styles = getSampleStyleSheet()

    style_title = ParagraphStyle(
        "ReportTitle",
        parent=base_styles["Title"],
        fontSize=18,
        textColor=colors.HexColor("#2c3e50"),
        spaceAfter=4,
    )
    style_subtitle = ParagraphStyle(
        "ReportSubtitle",
        parent=base_styles["Normal"],
        fontSize=9,
        textColor=colors.HexColor("#888888"),
        spaceAfter=12,
    )
    style_h2 = ParagraphStyle(
        "H2",
        parent=base_styles["Heading2"],
        fontSize=13,
        textColor=colors.HexColor("#34495e"),
        spaceBefore=14,
        spaceAfter=6,
        borderPad=2,
    )
    style_body = ParagraphStyle(
        "Body",
        parent=base_styles["Normal"],
        fontSize=9,
        leading=14,
        textColor=colors.HexColor("#444444"),
        spaceAfter=8,
    )
    style_warn = ParagraphStyle(
        "Warn",
        parent=base_styles["Normal"],
        fontSize=8,
        textColor=colors.HexColor("#c0392b"),
        fontName="Courier",
    )

    # ── Table style helpers ───────────────────────────────────────────────
    _TH_BG = colors.HexColor("#2c3e50")
    _TH_FG = colors.white
    _ROW_ALT = colors.HexColor("#eaf4fb")

    def _base_table_style(header_rows: int = 1) -> TableStyle:
        cmds = [
            ("BACKGROUND", (0, 0), (-1, header_rows - 1), _TH_BG),
            ("TEXTCOLOR",  (0, 0), (-1, header_rows - 1), _TH_FG),
            ("FONTNAME",   (0, 0), (-1, header_rows - 1), "Helvetica-Bold"),
            ("FONTSIZE",   (0, 0), (-1, header_rows - 1), 8),
            ("FONTSIZE",   (0, header_rows), (-1, -1), 8),
            ("ROWBACKGROUNDS", (0, header_rows), (-1, -1),
             [colors.white, _ROW_ALT]),
            ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#cccccc")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING",    (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING",   (0, 0), (-1, -1), 6),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 6),
        ]
        return TableStyle(cmds)

    # ── Data preparation ──────────────────────────────────────────────────
    summary = _compute_summary(records)
    pkg_summary = _compute_pkg_summary(records)
    _, narrative_html = _compute_insights(records, logcat_events)
    narrative_plain = _strip_html(narrative_html)
    charts = generate_charts(records)

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # ── Build flowables ───────────────────────────────────────────────────
    page_w, _ = A4
    usable_w = page_w - 4 * cm   # left + right margins 2 cm each

    story: List[Any] = []

    # Header
    story.append(Paragraph("ADB Telemetry &amp; Health Report", style_title))
    story.append(Paragraph(
        f"Device: <b>{device_id}</b>  |  "
        f"Package(s): <b>{package_name}</b>  |  "
        f"{len(records)} samples  |  Generated: {generated_at}",
        style_subtitle,
    ))
    story.append(HRFlowable(width="100%", thickness=1,
                             color=colors.HexColor("#bdc3c7"), spaceAfter=6))

    # ── Section 1: Insights narrative ─────────────────────────────────────
    story.append(Paragraph("Session Insights", style_h2))
    story.append(Paragraph(narrative_plain, style_body))

    # ── Section 2: Metric summary table ──────────────────────────────────
    story.append(Paragraph("Metric Summary", style_h2))
    if summary:
        tdata = [["Metric", "Min", "Max", "Avg"]]
        for row in summary:
            tdata.append([row["metric"], str(row["min"]),
                          str(row["max"]), str(row["avg"])])
        t = Table(tdata, colWidths=[usable_w * 0.4] + [usable_w * 0.2] * 3)
        t.setStyle(_base_table_style())
        story.append(t)
        story.append(Spacer(1, 8))

    # Per-package table
    if pkg_summary:
        story.append(Paragraph("Per-Package RAM &amp; CPU Averages", style_h2))
        tdata = [["Package", "Avg RAM (KB)", "Peak RAM (KB)",
                  "Avg CPU (%)", "Peak CPU (%)"]]
        for row in pkg_summary:
            tdata.append([
                row["package"],
                str(row["avg_ram"])  if row["avg_ram"]  is not None else "—",
                str(row["peak_ram"]) if row["peak_ram"] is not None else "—",
                str(row["avg_cpu"])  if row["avg_cpu"]  is not None else "—",
                str(row["peak_cpu"]) if row["peak_cpu"] is not None else "—",
            ])
        col_w = usable_w / 5
        t = Table(tdata, colWidths=[col_w * 2] + [col_w * 0.75] * 4)
        t.setStyle(_base_table_style())
        story.append(t)
        story.append(Spacer(1, 8))

    # ── Section 3: Charts ─────────────────────────────────────────────────
    chart_keys = [
        ("ram",        "RAM PSS (KB)"),
        ("cpu",        "CPU (%)"),
        ("batt_level", "Battery Level (%)"),
        ("batt_temp",  "Battery Temp (°C)"),
    ]
    available_charts = [(k, lbl) for k, lbl in chart_keys if charts.get(k)]
    if available_charts:
        story.append(Paragraph("Charts", style_h2))
        # 2-column grid layout
        chart_w = (usable_w - 0.4 * cm) / 2
        chart_h = chart_w * 0.46    # keep 16:7 aspect

        for i in range(0, len(available_charts), 2):
            row_cells = []
            for k, lbl in available_charts[i:i + 2]:
                img = _b64_to_image(charts[k], chart_w, chart_h)
                if img:
                    row_cells.append(img)
                else:
                    row_cells.append(Paragraph(f"[{lbl} unavailable]", style_body))
            if len(row_cells) == 1:
                row_cells.append("")    # fill second cell
            grid = Table([row_cells],
                         colWidths=[chart_w + 0.2 * cm, chart_w + 0.2 * cm])
            grid.setStyle(TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING",   (0, 0), (-1, -1), 0),
                ("RIGHTPADDING",  (0, 0), (-1, -1), 4),
                ("TOPPADDING",    (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]))
            story.append(grid)

    # ── Section 4: Logcat events ──────────────────────────────────────────
    story.append(Paragraph("Logcat Events", style_h2))
    if logcat_events:
        tdata = [["Timestamp", "Log Line"]]
        for evt in logcat_events:
            tdata.append([
                str(evt.get("timestamp", "")),
                str(evt.get("line", "")),
            ])
        t = Table(tdata, colWidths=[usable_w * 0.25, usable_w * 0.75])
        t.setStyle(_base_table_style())
        story.append(t)
    else:
        story.append(Paragraph(
            "No crash or ANR events were captured during this session.",
            style_body,
        ))

    # ── Build PDF ─────────────────────────────────────────────────────────
    try:
        doc = SimpleDocTemplate(
            str(output_path),
            pagesize=A4,
            leftMargin=2 * cm,
            rightMargin=2 * cm,
            topMargin=2 * cm,
            bottomMargin=2 * cm,
            title=f"ADB Telemetry Report — {package_name}",
            author="ADB Telemetry & Health Monitor",
        )
        doc.build(story)
        logger.info("PDF report saved → '%s'.", output_path)
    except OSError as exc:
        logger.error("Failed to write PDF report to '%s': %s", output_path, exc)
