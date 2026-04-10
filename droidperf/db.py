"""
droidperf/db.py
---------------
Optional SQLite persistence layer for the ADB Telemetry & Health Monitor.

All metric records are stored in a local SQLite database alongside the
existing CSV files.  The database is created automatically on first use in
the configured output directory (``telemetry.db`` by default).

Schema
------
sessions
    id          INTEGER PRIMARY KEY AUTOINCREMENT
    device_id   TEXT NOT NULL
    packages    TEXT          (JSON array)
    started_at  TEXT NOT NULL (ISO-8601)
    ended_at    TEXT
    report_html TEXT          (relative path to HTML report)

records
    id          INTEGER PRIMARY KEY AUTOINCREMENT
    session_id  INTEGER NOT NULL REFERENCES sessions(id)
    timestamp   TEXT NOT NULL
    package     TEXT
    ram_pss_kb  REAL
    cpu_total   REAL
    cpu_user    REAL
    cpu_load_1m REAL
    batt_level  REAL
    batt_temp_c REAL
    batt_voltage_mv REAL
    batt_status TEXT
    net_rx_kb   REAL
    net_tx_kb   REAL
    disk_read_kb  REAL
    disk_write_kb REAL
    thread_count  REAL
    fd_count      REAL

Public API:
    TelemetryDB(db_path)
    TelemetryDB.open_session(device_id, packages)    -> session_id
    TelemetryDB.close_session(session_id, report_html)
    TelemetryDB.insert_records(session_id, records)
    TelemetryDB.get_records(session_id)              -> List[Dict]
    TelemetryDB.list_sessions()                      -> List[Dict]
    TelemetryDB.export_csv(session_id, output_path)
    TelemetryDB.close()
"""

import csv
import json
import logging
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Column definitions used for both INSERT and SELECT operations.
_RECORD_COLS = (
    "session_id", "timestamp", "package",
    "ram_pss_kb", "cpu_total", "cpu_user", "cpu_load_1m",
    "batt_level", "batt_temp_c", "batt_voltage_mv", "batt_status",
    "net_rx_kb", "net_tx_kb", "disk_read_kb", "disk_write_kb",
    "thread_count", "fd_count",
)

# Maps CSV / record dict keys → DB column names.
_KEY_MAP: Dict[str, str] = {
    "cpu_total_pct": "cpu_total",
    "cpu_user_pct":  "cpu_user",
}


def _map_key(key: str) -> str:
    """Translate a record-dict key to its DB column name."""
    return _KEY_MAP.get(key, key)


class TelemetryDB:
    """
    Thread-safe SQLite wrapper for telemetry persistence.

    A single ``TelemetryDB`` instance may be shared across monitoring
    threads; all writes are serialised through an internal ``threading.Lock``.

    Args:
        db_path (Path): Absolute path to the SQLite database file.
                        The file and all parent directories are created
                        automatically if they do not exist.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        self._connect()
        self._create_schema()
        logger.info("TelemetryDB opened at '%s'.", self._db_path)

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _connect(self) -> None:
        """Open the SQLite connection with WAL mode for concurrent reads."""
        self._conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,
        )
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self._conn.row_factory = sqlite3.Row

    def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            try:
                self._conn.close()
                logger.debug("TelemetryDB closed.")
            except Exception as exc:  # pylint: disable=broad-except
                logger.warning("Error closing DB: %s", exc)
            finally:
                self._conn = None

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _create_schema(self) -> None:
        """Create tables if they do not exist yet."""
        with self._lock:
            cur = self._conn.cursor()
            cur.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_id   TEXT    NOT NULL,
                    packages    TEXT,
                    started_at  TEXT    NOT NULL,
                    ended_at    TEXT,
                    report_html TEXT
                );

                CREATE TABLE IF NOT EXISTS records (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id      INTEGER NOT NULL REFERENCES sessions(id),
                    timestamp       TEXT    NOT NULL,
                    package         TEXT,
                    ram_pss_kb      REAL,
                    cpu_total       REAL,
                    cpu_user        REAL,
                    cpu_load_1m     REAL,
                    batt_level      REAL,
                    batt_temp_c     REAL,
                    batt_voltage_mv REAL,
                    batt_status     TEXT,
                    net_rx_kb       REAL,
                    net_tx_kb       REAL,
                    disk_read_kb    REAL,
                    disk_write_kb   REAL,
                    thread_count    REAL,
                    fd_count        REAL
                );

                CREATE INDEX IF NOT EXISTS idx_records_session
                    ON records(session_id);
                CREATE INDEX IF NOT EXISTS idx_records_ts
                    ON records(timestamp);
            """)
            self._conn.commit()

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    def open_session(
        self,
        device_id: str,
        packages: List[str],
    ) -> int:
        """
        Create a new monitoring session record and return its ID.

        Args:
            device_id (str):   Device serial number.
            packages (List[str]): Monitored package names.

        Returns:
            int: The new session's primary key.
        """
        started_at = datetime.now().isoformat(timespec="seconds")
        pkg_json = json.dumps(packages)
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO sessions (device_id, packages, started_at) VALUES (?, ?, ?)",
                (device_id, pkg_json, started_at),
            )
            self._conn.commit()
            session_id = cur.lastrowid
        logger.debug("DB: opened session %d for device '%s'.", session_id, device_id)
        return session_id

    def close_session(
        self,
        session_id: int,
        report_html: Optional[str] = None,
    ) -> None:
        """
        Mark a session as ended and optionally record the report path.

        Args:
            session_id (int):         Session primary key.
            report_html (str|None):   Relative path to the HTML report.
        """
        ended_at = datetime.now().isoformat(timespec="seconds")
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET ended_at=?, report_html=? WHERE id=?",
                (ended_at, report_html, session_id),
            )
            self._conn.commit()
        logger.debug("DB: closed session %d.", session_id)

    # ------------------------------------------------------------------
    # Records
    # ------------------------------------------------------------------

    def insert_records(
        self,
        session_id: int,
        records: List[Dict[str, Any]],
    ) -> None:
        """
        Bulk-insert a list of metric record dicts for a session.

        Unknown keys in each record dict are silently ignored so the DB
        layer stays decoupled from the collector schema.

        Args:
            session_id (int):       Session primary key.
            records (List[Dict]):   Metric rows from ``MonitorEngine``.
        """
        if not records:
            return

        insert_cols = [c for c in _RECORD_COLS if c != "session_id"]
        placeholders = ", ".join("?" for _ in _RECORD_COLS)
        sql = (
            f"INSERT INTO records ({', '.join(_RECORD_COLS)}) "
            f"VALUES ({placeholders})"
        )

        rows = []
        for r in records:
            row = [session_id]
            for col in insert_cols:
                # Check both original key and mapped key.
                raw_key = next(
                    (k for k, v in _KEY_MAP.items() if v == col),
                    col,
                )
                val = r.get(col) if r.get(col) is not None else r.get(raw_key)
                row.append(val)
            rows.append(tuple(row))

        with self._lock:
            try:
                self._conn.executemany(sql, rows)
                self._conn.commit()
            except sqlite3.Error as exc:
                logger.error("DB: failed to insert records: %s", exc)

    def get_records(self, session_id: int) -> List[Dict[str, Any]]:
        """
        Retrieve all records for a session as a list of dicts.

        Args:
            session_id (int): Session primary key.

        Returns:
            List[Dict]: One dict per record row.
        """
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM records WHERE session_id=? ORDER BY timestamp",
                (session_id,),
            )
            rows = cur.fetchall()
        return [dict(row) for row in rows]

    def list_sessions(self) -> List[Dict[str, Any]]:
        """
        Return all sessions ordered newest first.

        Returns:
            List[Dict]: One dict per session row.
        """
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM sessions ORDER BY started_at DESC"
            )
            rows = cur.fetchall()
        result = []
        for row in rows:
            d = dict(row)
            try:
                d["packages"] = json.loads(d.get("packages") or "[]")
            except (json.JSONDecodeError, TypeError):
                d["packages"] = []
            result.append(d)
        return result

    # ------------------------------------------------------------------
    # CSV export
    # ------------------------------------------------------------------

    def export_csv(self, session_id: int, output_path: Path) -> None:
        """
        Export all records for a session to a CSV file.

        Args:
            session_id (int):       Session primary key.
            output_path (Path):     Destination CSV file path.
        """
        records = self.get_records(session_id)
        if not records:
            logger.warning("DB: no records for session %d — CSV not written.", session_id)
            return
        try:
            with open(output_path, "w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=records[0].keys())
                writer.writeheader()
                writer.writerows(records)
            logger.info("DB: exported session %d → '%s'.", session_id, output_path)
        except OSError as exc:
            logger.error("DB: failed to export CSV: %s", exc)

    def delete_old_sessions(self, max_age_days: int) -> int:
        """
        Delete sessions (and their records) older than *max_age_days*.

        Args:
            max_age_days (int): Sessions with ``started_at`` older than this
                                number of days are removed.

        Returns:
            int: Number of sessions deleted.
        """
        if max_age_days <= 0:
            return 0
        from datetime import timedelta
        cutoff = (datetime.now() - timedelta(days=max_age_days)).isoformat()
        with self._lock:
            cur = self._conn.execute(
                "SELECT id FROM sessions WHERE started_at < ?", (cutoff,)
            )
            old_ids = [row[0] for row in cur.fetchall()]
            if not old_ids:
                return 0
            placeholders = ",".join("?" for _ in old_ids)
            self._conn.execute(
                f"DELETE FROM records WHERE session_id IN ({placeholders})", old_ids
            )
            self._conn.execute(
                f"DELETE FROM sessions WHERE id IN ({placeholders})", old_ids
            )
            self._conn.commit()
        logger.info("DB: deleted %d old session(s) older than %d days.", len(old_ids), max_age_days)
        return len(old_ids)
