"""
gui/widgets/stat_cards.py
--------------------------
Live metric summary card strip for the top of the Dashboard tab.

Displays four always-visible cards updated on every monitoring snapshot:
  - Total RAM PSS across all monitored packages (KB → MB)
  - Average CPU % across all packages
  - Current battery level (%)
  - Number of active packages being monitored

Each card has a coloured accent border, a large value label, and a
small subtitle.  Cards turn amber/red when values cross warning zones.

Public API:
    StatCards(master, **kwargs)
    StatCards.update(rows)
    StatCards.clear()
"""

import logging
from typing import Any, Dict, List, Optional

import customtkinter as ctk

from droidperf.i18n import t

logger = logging.getLogger(__name__)

# Colour palette
_GREEN  = "#27ae60"
_AMBER  = "#e67e22"
_RED    = "#e74c3c"
_BLUE   = "#2980b9"
_GRAY   = "#7f8c8d"
_CARD_BG   = "#1e1e2e"
_BORDER_INACTIVE = "#2d2d4e"


def _ram_color(mb: float) -> str:
    if mb >= 512:
        return _RED
    if mb >= 256:
        return _AMBER
    return _BLUE


def _cpu_color(pct: float) -> str:
    if pct >= 60:
        return _RED
    if pct >= 30:
        return _AMBER
    return _GREEN


def _batt_color(lvl: float) -> str:
    if lvl <= 15:
        return _RED
    if lvl <= 30:
        return _AMBER
    return _GREEN


class _Card(ctk.CTkFrame):
    """A single metric card with icon, value, and subtitle."""

    def __init__(self, master: Any, icon: str, subtitle: str, accent: str, **kwargs) -> None:
        kwargs.setdefault("fg_color", _CARD_BG)
        kwargs.setdefault("corner_radius", 8)
        kwargs.setdefault("border_width", 2)
        kwargs.setdefault("border_color", accent)
        super().__init__(master, **kwargs)

        self._accent = accent

        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=10, pady=(8, 0))

        ctk.CTkLabel(top, text=icon, font=ctk.CTkFont(size=14),
                     text_color=accent, width=20).pack(side="left")

        self._value_lbl = ctk.CTkLabel(
            self, text="—",
            font=ctk.CTkFont(size=20, weight="bold"),
            text_color=accent,
        )
        self._value_lbl.pack(padx=10, pady=(2, 0))

        ctk.CTkLabel(
            self, text=subtitle,
            font=ctk.CTkFont(size=9),
            text_color=_GRAY,
        ).pack(padx=10, pady=(0, 8))

    def set_value(self, text: str, color: Optional[str] = None) -> None:
        """Update the displayed value and optional accent colour."""
        c = color or self._accent
        self._value_lbl.configure(text=text, text_color=c)
        self.configure(border_color=c)


class StatCards(ctk.CTkFrame):
    """
    Horizontal strip of four live metric summary cards.

    Place at the top of the Dashboard tab; call ``update(rows)`` on
    every snapshot and ``clear()`` when a session ends.

    Args:
        master: Parent widget.
        **kwargs: Forwarded to ``ctk.CTkFrame.__init__``.
    """

    def __init__(self, master: Any, **kwargs) -> None:
        kwargs.setdefault("fg_color", "transparent")
        super().__init__(master, **kwargs)

        self._build_cards()
        logger.debug("StatCards initialised.")

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_cards(self) -> None:
        """Create and grid the four metric cards."""
        self.grid_columnconfigure((0, 1, 2, 3), weight=1)

        self._card_ram = _Card(self, "💾", t("stat_ram"), _BLUE)
        self._card_cpu = _Card(self, "⚡", t("stat_cpu"), _GREEN)
        self._card_batt = _Card(self, "🔋", t("stat_battery"), _GREEN)
        self._card_pkgs = _Card(self, "📦", t("stat_packages"), _BLUE)

        for col, card in enumerate([
            self._card_ram, self._card_cpu,
            self._card_batt, self._card_pkgs,
        ]):
            card.grid(row=0, column=col, padx=(0 if col == 0 else 6, 0), pady=6, sticky="ew")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update(self, rows: List[Dict[str, Any]]) -> None:
        """
        Refresh all cards from a new snapshot batch.

        Args:
            rows: Record dicts from one monitoring cycle.
        """
        if not rows:
            return

        # Total RAM
        ram_vals = [r["ram_pss_kb"] for r in rows if r.get("ram_pss_kb") is not None]
        total_ram_mb = sum(ram_vals) / 1024 if ram_vals else None

        # Average CPU
        cpu_vals = [r["cpu_total_pct"] for r in rows if r.get("cpu_total_pct") is not None]
        avg_cpu = sum(cpu_vals) / len(cpu_vals) if cpu_vals else None

        # Battery (latest non-None)
        batt_vals = [r["batt_level"] for r in rows if r.get("batt_level") is not None]
        batt = batt_vals[-1] if batt_vals else None

        # Unique packages
        pkgs = len({r.get("package") for r in rows if r.get("package")})

        # Update cards
        if total_ram_mb is not None:
            self._card_ram.set_value(f"{total_ram_mb:.1f}", _ram_color(total_ram_mb))
        if avg_cpu is not None:
            self._card_cpu.set_value(f"{avg_cpu:.1f}%", _cpu_color(avg_cpu))
        if batt is not None:
            self._card_batt.set_value(f"{batt:.0f}%", _batt_color(float(batt)))
        if pkgs:
            self._card_pkgs.set_value(str(pkgs), _BLUE)

    def clear(self) -> None:
        """Reset all cards to their default '—' state."""
        for card in (self._card_ram, self._card_cpu, self._card_batt, self._card_pkgs):
            card.set_value("—")
        logger.debug("StatCards cleared.")
