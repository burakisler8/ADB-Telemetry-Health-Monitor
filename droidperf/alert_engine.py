"""
droidperf/alert_engine.py
--------------------------
Threshold-based alert evaluation and spike detection for telemetry records.

``AlertEngine`` is called once per monitoring cycle with the batch of new
records.  It fires an ``on_alert`` callback for every rule that triggers,
passing an ``AlertEvent`` namedtuple with enough context for the GUI to
display a banner and for the notifier to send a webhook payload.

Alert rules (all configurable via ``settings_manager.settings``):
    alert_ram_kb      RAM PSS threshold in KB  (0 = disabled)
    alert_cpu_pct     CPU total % threshold    (0 = disabled)
    alert_temp_c      Battery temperature °C   (0 = disabled)
    alert_batt_drop   Battery level drop % per cycle (0 = disabled)

Spike detection (statistical):
    A metric is flagged as a spike when its current value exceeds
    μ + N·σ of the rolling history, where N = ``spike_std_multiplier``
    (default 3.0) and the rolling window is 20 data points per series.

Public API:
    AlertEngine(on_alert)
    AlertEngine.check(rows)   — call after every monitoring cycle
    AlertEngine.clear()       — reset rolling state (new session)

    AlertEvent(kind, package, device_id, metric, value, threshold, message)
"""

import logging
import math
from collections import defaultdict, deque
from typing import Callable, Dict, List, NamedTuple, Optional

from droidperf.settings_manager import settings

logger = logging.getLogger(__name__)

# Rolling history length for spike detection.
_SPIKE_WINDOW = 20


class AlertEvent(NamedTuple):
    """Carries all information about a single alert trigger."""

    kind: str           # "threshold" | "spike"
    package: str        # Package name (empty string for device-level)
    device_id: str      # ADB serial
    metric: str         # e.g. "ram_pss_kb", "cpu_total_pct"
    value: float        # Current measured value
    threshold: float    # Threshold or μ+N·σ that was exceeded
    message: str        # Human-readable description


class AlertEngine:
    """
    Evaluates alert rules against incoming telemetry records and fires
    the ``on_alert`` callback for every triggered condition.

    Args:
        on_alert (Callable[[AlertEvent], None]):
            Called (from the monitoring thread) whenever an alert fires.
            Must be thread-safe (e.g. ``queue.Queue.put``).
    """

    def __init__(self, on_alert: Callable[[AlertEvent], None]) -> None:
        self._on_alert = on_alert

        # Rolling histories: {(device_id, package, metric): deque([float, ...])}
        self._history: Dict[tuple, deque] = defaultdict(
            lambda: deque(maxlen=_SPIKE_WINDOW)
        )

        # Previous battery level per device for drop-rate calculation.
        self._prev_batt: Dict[str, Optional[float]] = {}

        logger.debug("AlertEngine initialised.")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check(self, rows: List[dict]) -> None:
        """
        Evaluate all alert rules against a batch of telemetry records.

        Args:
            rows (List[dict]): Record dicts from one monitoring cycle.
        """
        ram_thresh = settings.get("alert_ram_kb", 0)
        cpu_thresh = settings.get("alert_cpu_pct", 0)
        temp_thresh = settings.get("alert_temp_c", 0)
        batt_drop_thresh = settings.get("alert_batt_drop", 0)
        spike_mult = float(settings.get("spike_std_multiplier", 3.0))

        for record in rows:
            pkg = record.get("package", "")
            dev = record.get("device_id", "")

            # ── Threshold: RAM ────────────────────────────────────────
            ram = record.get("ram_pss_kb")
            if ram is not None and ram_thresh > 0 and ram > ram_thresh:
                self._fire(AlertEvent(
                    kind="threshold", package=pkg, device_id=dev,
                    metric="ram_pss_kb", value=ram, threshold=ram_thresh,
                    message=(
                        f"{self._short(pkg)} RAM {ram:,.0f} KB "
                        f"exceeds threshold {ram_thresh:,.0f} KB"
                    ),
                ))

            # ── Threshold: CPU ────────────────────────────────────────
            cpu = record.get("cpu_total_pct")
            if cpu is not None and cpu_thresh > 0 and cpu > cpu_thresh:
                self._fire(AlertEvent(
                    kind="threshold", package=pkg, device_id=dev,
                    metric="cpu_total_pct", value=cpu, threshold=cpu_thresh,
                    message=(
                        f"{self._short(pkg)} CPU {cpu:.1f}% "
                        f"exceeds threshold {cpu_thresh:.1f}%"
                    ),
                ))

            # ── Threshold: Temperature ────────────────────────────────
            temp = record.get("batt_temp_c")
            if temp is not None and temp_thresh > 0 and temp > temp_thresh:
                self._fire(AlertEvent(
                    kind="threshold", package=pkg, device_id=dev,
                    metric="batt_temp_c", value=temp, threshold=temp_thresh,
                    message=(
                        f"Device {dev} battery temp {temp:.1f}°C "
                        f"exceeds threshold {temp_thresh:.1f}°C"
                    ),
                ))

            # ── Threshold: Battery drop ───────────────────────────────
            batt = record.get("batt_level")
            if batt is not None and batt_drop_thresh > 0:
                prev = self._prev_batt.get(dev)
                if prev is not None:
                    drop = prev - batt
                    if drop > batt_drop_thresh:
                        self._fire(AlertEvent(
                            kind="threshold", package="", device_id=dev,
                            metric="batt_level", value=drop, threshold=batt_drop_thresh,
                            message=(
                                f"Device {dev} battery dropped {drop:.1f}% "
                                f"(threshold {batt_drop_thresh:.1f}%)"
                            ),
                        ))
                self._prev_batt[dev] = batt

            # ── Spike detection: RAM & CPU ────────────────────────────
            if spike_mult > 0:
                for metric, val in [
                    ("ram_pss_kb", ram),
                    ("cpu_total_pct", cpu),
                ]:
                    if val is None:
                        continue
                    key = (dev, pkg, metric)
                    hist = self._history[key]
                    spike_thresh = self._spike_threshold(hist, spike_mult)
                    if spike_thresh is not None and val > spike_thresh:
                        self._fire(AlertEvent(
                            kind="spike", package=pkg, device_id=dev,
                            metric=metric, value=val, threshold=spike_thresh,
                            message=(
                                f"{self._short(pkg)} {metric} spike: "
                                f"{val:.1f} > μ+{spike_mult:.1f}σ "
                                f"({spike_thresh:.1f})"
                            ),
                        ))
                    # Always append to history AFTER checking to avoid
                    # self-triggering on the first anomalous value.
                    hist.append(val)

    def clear(self) -> None:
        """Reset all rolling state (call when a new session starts)."""
        self._history.clear()
        self._prev_batt.clear()
        logger.debug("AlertEngine state cleared.")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fire(self, event: AlertEvent) -> None:
        """Invoke the callback and log the alert."""
        logger.warning("ALERT [%s] %s", event.kind.upper(), event.message)
        try:
            self._on_alert(event)
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("on_alert callback raised an exception: %s", exc)

    @staticmethod
    def _spike_threshold(
        history: deque,
        multiplier: float,
    ) -> Optional[float]:
        """
        Compute μ + multiplier·σ from *history*.

        Returns ``None`` when fewer than 5 data points are available
        (not enough to estimate a meaningful distribution).

        Args:
            history (deque[float]): Recent metric values.
            multiplier (float):     Standard deviation multiplier.

        Returns:
            Optional[float]: Spike threshold, or ``None``.
        """
        if len(history) < 5:
            return None
        n = len(history)
        mean = sum(history) / n
        variance = sum((x - mean) ** 2 for x in history) / n
        std = math.sqrt(variance) if variance > 0 else 0.0
        return mean + multiplier * std

    @staticmethod
    def _short(package: str) -> str:
        """Return the last component of a dotted package name."""
        return package.split(".")[-1] if package else "device"
