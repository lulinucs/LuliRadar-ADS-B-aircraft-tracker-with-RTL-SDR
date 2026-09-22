"""
Wrapper fino e thread-safe sobre sqlite3.

Uma única conexão compartilhada, protegida por um RLock. Para o volume de
escrita desta aplicação (algumas dezenas de updates por segundo no máximo)
isso é simples e suficiente - evita a complexidade de um pool de conexões
ou de um processo de escrita separado.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path

logger = logging.getLogger("luliradar.database")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS aircraft (
    icao TEXT PRIMARY KEY,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    last_callsign TEXT,
    last_squawk TEXT,
    last_category TEXT,
    message_count INTEGER DEFAULT 0,
    last_lat REAL,
    last_lon REAL,
    last_altitude_baro INTEGER,
    last_raw_json TEXT
);

CREATE TABLE IF NOT EXISTS flight_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    icao TEXT NOT NULL,
    callsign TEXT,
    start_time TEXT NOT NULL,
    end_time TEXT,
    first_lat REAL,
    first_lon REAL,
    last_lat REAL,
    last_lon REAL,
    min_altitude INTEGER,
    max_altitude INTEGER,
    max_ground_speed REAL,
    closest_distance_nm REAL,
    is_active INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY (icao) REFERENCES aircraft (icao)
);
CREATE INDEX IF NOT EXISTS idx_sessions_icao ON flight_sessions (icao);
CREATE INDEX IF NOT EXISTS idx_sessions_active ON flight_sessions (is_active);
CREATE INDEX IF NOT EXISTS idx_sessions_start ON flight_sessions (start_time);

CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    flight_session_id INTEGER NOT NULL,
    icao TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    lat REAL,
    lon REAL,
    altitude_baro INTEGER,
    altitude_geom INTEGER,
    ground_speed REAL,
    ias REAL,
    tas REAL,
    track REAL,
    heading REAL,
    vertical_rate INTEGER,
    squawk TEXT,
    category TEXT,
    rssi REAL,
    distance_nm REAL,
    FOREIGN KEY (flight_session_id) REFERENCES flight_sessions (id)
);
CREATE INDEX IF NOT EXISTS idx_positions_session ON positions (flight_session_id);
CREATE INDEX IF NOT EXISTS idx_positions_icao_ts ON positions (icao, timestamp);

CREATE TABLE IF NOT EXISTS adsb_messages_summary (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    aircraft_count INTEGER,
    messages_total INTEGER,
    messages_per_second REAL
);
CREATE INDEX IF NOT EXISTS idx_summary_ts ON adsb_messages_summary (timestamp);

CREATE TABLE IF NOT EXISTS radio_recordings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    start_time TEXT NOT NULL,
    end_time TEXT,
    duration REAL,
    preset TEXT,
    frequency_mhz REAL,
    filename TEXT NOT NULL,
    peak_level REAL,
    average_level REAL
);
CREATE INDEX IF NOT EXISTS idx_recordings_start ON radio_recordings (start_time);
"""


class Database:
    """Conexão SQLite única, compartilhada entre threads via lock."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA_SQL)
            self._conn.commit()
        logger.info("Banco de dados pronto em %s", path)

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """INSERT/UPDATE/DELETE. Faz commit imediato (volume baixo o suficiente)."""
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def executemany(self, sql: str, seq_of_params) -> None:
        with self._lock:
            self._conn.executemany(sql, seq_of_params)
            self._conn.commit()

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            cur = self._conn.execute(sql, params)
            rows = cur.fetchall()
            return [dict(r) for r in rows]

    def query_one(self, sql: str, params: tuple = ()) -> dict | None:
        with self._lock:
            cur = self._conn.execute(sql, params)
            row = cur.fetchone()
            return dict(row) if row is not None else None

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                logger.exception("Erro ao fechar banco de dados")
