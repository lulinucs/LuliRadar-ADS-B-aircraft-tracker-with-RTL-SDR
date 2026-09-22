"""
AdsbService: gerencia o processo dump1090-mutability e consome o aircraft.json
que ele produz periodicamente (--write-json). Não reimplementa nenhum
decoder ADS-B - toda a recepção/decodificação continua sendo feita pelo
dump1090.

Responsabilidades:
  - subir/derrubar o processo dump1090-mutability
  - ler aircraft.json a cada mudança de mtime (~1x/s, conforme --write-json-every)
  - manter um cache em memória do estado atual de cada aeronave (para a API
    responder rápido, sem tocar o SQLite a cada request)
  - persistir no SQLite: aircraft (upsert), flight_sessions (abre/fecha) e
    positions (com throttling para não gravar milhares de pontos idênticos)
"""
from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from collections import deque
from datetime import datetime, timezone

from .config import Settings
from .database import Database
from .distance import haversine_nm
from .models import normalize_aircraft
from .procutil import terminate_process
from .state_classifier import classify_aircraft_state

logger = logging.getLogger("luliradar.adsb")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class AdsbService:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings

        self._proc: subprocess.Popen | None = None
        self._poll_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

        self._state_lock = threading.RLock()
        self._aircraft_cache: dict[str, dict] = {}
        self._last_position_saved: dict[str, tuple[float, float, float]] = {}
        self._last_summary_write = 0.0

        self._stats = {
            "running": False,
            "pid": None,
            "start_time": None,
            "messages_total": 0,
            "messages_per_second": 0.0,
            "aircraft_count": 0,
            "last_update": None,
            "error": None,
        }

    # ------------------------------------------------------------------ #
    # ciclo de vida
    # ------------------------------------------------------------------ #
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        if self.is_running():
            return

        cmd = [
            self.settings.dump1090_bin,
            "--net",
            "--write-json", str(self.settings.dump1090_json_dir),
            "--write-json-every", "1",
            "--quiet",
            "--json-location-accuracy", "2",
            "--max-range", str(self.settings.dump1090_max_range_nm),
        ]
        if self.settings.receiver_position_configured:
            cmd += ["--lat", str(self.settings.receiver_lat), "--lon", str(self.settings.receiver_lon)]
        gain = (self.settings.dump1090_gain or "").strip().lower()
        if gain in ("auto", "agc"):
            cmd += ["--gain", "-10"]
        elif gain not in ("", "max"):
            cmd += ["--gain", gain]
        cmd += self.settings.dump1090_extra_args

        logger.info("Iniciando ADS-B: %s", " ".join(cmd))
        with self._state_lock:
            self._stats["error"] = None
            try:
                self._proc = subprocess.Popen(
                    cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                    text=True, start_new_session=True,
                )
            except FileNotFoundError as exc:
                self._stats["error"] = f"Binário '{self.settings.dump1090_bin}' não encontrado: {exc}"
                logger.error(self._stats["error"])
                raise RuntimeError(self._stats["error"]) from exc

            self._stats["running"] = True
            self._stats["pid"] = self._proc.pid
            self._stats["start_time"] = time.time()

        self._stop_event.clear()
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True, name="adsb-poll")
        self._poll_thread.start()
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True, name="adsb-stderr")
        self._stderr_thread.start()
        logger.info("dump1090-mutability iniciado (pid=%s)", self._proc.pid)

    def stop(self) -> None:
        if not self.is_running() and self._poll_thread is None:
            return
        logger.info("Parando ADS-B")
        self._stop_event.set()
        terminate_process(self._proc, "dump1090-mutability")
        self._proc = None
        if self._poll_thread is not None:
            self._poll_thread.join(timeout=4)
            self._poll_thread = None
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=2)
            self._stderr_thread = None
        self._close_all_active_sessions()
        with self._state_lock:
            self._stats["running"] = False
            self._stats["pid"] = None
        logger.info("ADS-B parado")

    def _drain_stderr(self) -> None:
        if self._proc is None or self._proc.stderr is None:
            return
        try:
            for line in self._proc.stderr:
                line = line.strip()
                if line:
                    logger.warning("dump1090: %s", line)
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # loop de leitura do aircraft.json
    # ------------------------------------------------------------------ #
    def _poll_loop(self) -> None:
        json_path = self.settings.dump1090_json_dir / "aircraft.json"
        last_mtime = None
        while not self._stop_event.is_set():
            try:
                if self._proc is not None and self._proc.poll() is not None:
                    err = f"dump1090-mutability encerrou inesperadamente (código={self._proc.returncode})"
                    logger.error(err)
                    with self._state_lock:
                        self._stats["error"] = err
                        self._stats["running"] = False
                    break
                if json_path.exists():
                    mtime = json_path.stat().st_mtime
                    if mtime != last_mtime:
                        last_mtime = mtime
                        with open(json_path, "r", encoding="utf-8") as f:
                            data = json.load(f)
                        self._process_snapshot(data)
            except (json.JSONDecodeError, OSError):
                # arquivo pode estar sendo escrito no exato momento da leitura; ignora e tenta de novo
                pass
            except Exception:
                logger.exception("Erro no loop de leitura do ADS-B")
            self._stop_event.wait(1.0)

    def _process_snapshot(self, data: dict) -> None:
        now_wall = time.time()
        receiver_ts = data.get("now", now_wall)
        msgs_total = data.get("messages", 0) or 0
        aircraft_list = data.get("aircraft", []) or []

        with self._state_lock:
            prev_total = self._stats["messages_total"]
            prev_time = self._stats["last_update"] or now_wall
            dt = max(now_wall - prev_time, 0.001)
            if prev_total and msgs_total >= prev_total:
                self._stats["messages_per_second"] = round((msgs_total - prev_total) / dt, 2)
            self._stats["messages_total"] = msgs_total
            self._stats["last_update"] = now_wall
            self._stats["aircraft_count"] = len(aircraft_list)

            for ac in aircraft_list:
                icao = str(ac.get("hex") or "").upper().strip()
                if not icao:
                    continue
                self._ingest_aircraft(icao, ac, receiver_ts, now_wall)

            self._sweep_stale_sessions(now_wall)

            if now_wall - self._last_summary_write >= self.settings.adsb_summary_interval_sec:
                self._last_summary_write = now_wall
                self.db.execute(
                    "INSERT INTO adsb_messages_summary (timestamp, aircraft_count, messages_total, "
                    "messages_per_second) VALUES (?,?,?,?)",
                    (_now_iso(), len(aircraft_list), msgs_total, self._stats["messages_per_second"]),
                )

    def _ingest_aircraft(self, icao: str, ac: dict, receiver_ts: float, now_wall: float) -> None:
        norm = normalize_aircraft(ac, receiver_ts)
        distance_nm = None
        if norm["has_position"] and self.settings.receiver_position_configured:
            distance_nm = haversine_nm(
                self.settings.receiver_lat, self.settings.receiver_lon, norm["lat"], norm["lon"]
            )
        norm["distance_nm"] = distance_nm

        # precisa existir em 'aircraft' antes de qualquer flight_session que a referencie (FK)
        self._upsert_aircraft_row(icao, norm, now_wall)

        cached = self._aircraft_cache.get(icao)
        gap = self.settings.adsb_session_gap_seconds
        new_session = cached is None or (now_wall - cached["_last_wall"] > gap)

        if new_session:
            session_id = self._create_flight_session(icao, norm, now_wall)
            distance_history: deque = deque(maxlen=10)
        else:
            session_id = cached["session_id"]
            distance_history = cached["distance_history"]
            self._update_flight_session(session_id, norm)

        if distance_nm is not None:
            distance_history.append(distance_nm)

        state_info = classify_aircraft_state(norm, list(distance_history))

        entry = dict(norm)
        entry.update(
            session_id=session_id,
            _last_wall=now_wall,
            distance_history=distance_history,
            state_info=state_info,
        )
        self._aircraft_cache[icao] = entry

        self._maybe_save_position(session_id, icao, norm, now_wall)

    # ------------------------------------------------------------------ #
    # persistência
    # ------------------------------------------------------------------ #
    def _upsert_aircraft_row(self, icao: str, norm: dict, now_wall: float) -> None:
        now_iso = _now_iso()
        raw_json = json.dumps(norm["raw"], ensure_ascii=False)
        existing = self.db.query_one("SELECT icao FROM aircraft WHERE icao = ?", (icao,))
        if existing is None:
            self.db.execute(
                "INSERT INTO aircraft (icao, first_seen, last_seen, last_callsign, last_squawk, "
                "last_category, message_count, last_lat, last_lon, last_altitude_baro, last_raw_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    icao, now_iso, now_iso, norm["callsign"], norm["squawk"], norm["category"],
                    norm["messages"] or 0, norm["lat"], norm["lon"], norm["altitude_baro"], raw_json,
                ),
            )
        else:
            self.db.execute(
                "UPDATE aircraft SET last_seen=?, last_callsign=COALESCE(?, last_callsign), "
                "last_squawk=COALESCE(?, last_squawk), last_category=COALESCE(?, last_category), "
                "message_count=?, last_lat=COALESCE(?, last_lat), last_lon=COALESCE(?, last_lon), "
                "last_altitude_baro=COALESCE(?, last_altitude_baro), last_raw_json=? WHERE icao=?",
                (
                    now_iso, norm["callsign"], norm["squawk"], norm["category"], norm["messages"] or 0,
                    norm["lat"], norm["lon"], norm["altitude_baro"], raw_json, icao,
                ),
            )

    def _create_flight_session(self, icao: str, norm: dict, now_wall: float) -> int:
        now_iso = _now_iso()
        cur = self.db.execute(
            "INSERT INTO flight_sessions (icao, callsign, start_time, end_time, first_lat, first_lon, "
            "last_lat, last_lon, min_altitude, max_altitude, max_ground_speed, closest_distance_nm, "
            "is_active) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1)",
            (
                icao, norm["callsign"], now_iso, now_iso, norm["lat"], norm["lon"], norm["lat"], norm["lon"],
                norm["altitude_baro"], norm["altitude_baro"], norm["ground_speed"], norm["distance_nm"],
            ),
        )
        return cur.lastrowid

    def _update_flight_session(self, session_id: int, norm: dict) -> None:
        row = self.db.query_one("SELECT * FROM flight_sessions WHERE id=?", (session_id,))
        if row is None:
            return
        min_alt = row["min_altitude"]
        max_alt = row["max_altitude"]
        if norm["altitude_baro"] is not None:
            min_alt = norm["altitude_baro"] if min_alt is None else min(min_alt, norm["altitude_baro"])
            max_alt = norm["altitude_baro"] if max_alt is None else max(max_alt, norm["altitude_baro"])
        max_speed = row["max_ground_speed"]
        if norm["ground_speed"] is not None:
            max_speed = norm["ground_speed"] if max_speed is None else max(max_speed, norm["ground_speed"])
        closest = row["closest_distance_nm"]
        if norm["distance_nm"] is not None:
            closest = norm["distance_nm"] if closest is None else min(closest, norm["distance_nm"])
        self.db.execute(
            "UPDATE flight_sessions SET end_time=?, callsign=COALESCE(?, callsign), "
            "last_lat=COALESCE(?, last_lat), last_lon=COALESCE(?, last_lon), min_altitude=?, "
            "max_altitude=?, max_ground_speed=?, closest_distance_nm=? WHERE id=?",
            (
                _now_iso(), norm["callsign"], norm["lat"], norm["lon"], min_alt, max_alt, max_speed,
                closest, session_id,
            ),
        )

    def _maybe_save_position(self, session_id: int, icao: str, norm: dict, now_wall: float) -> None:
        if not norm["has_position"]:
            return
        lat, lon = norm["lat"], norm["lon"]
        last = self._last_position_saved.get(icao)
        should_save = last is None
        if not should_save:
            last_t, last_lat, last_lon = last
            moved_m = haversine_nm(last_lat, last_lon, lat, lon) * 1852.0
            should_save = (
                now_wall - last_t >= self.settings.adsb_position_min_interval_sec
                or moved_m >= self.settings.adsb_position_min_distance_m
            )
        if not should_save:
            return
        self.db.execute(
            "INSERT INTO positions (flight_session_id, icao, timestamp, lat, lon, altitude_baro, "
            "altitude_geom, ground_speed, ias, tas, track, heading, vertical_rate, squawk, category, "
            "rssi, distance_nm) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                session_id, icao, _now_iso(), lat, lon, norm["altitude_baro"], norm["altitude_geom"],
                norm["ground_speed"], norm["ias"], norm["tas"], norm["track"], norm["heading"],
                norm["vertical_rate"], norm["squawk"], norm["category"], norm["rssi"], norm["distance_nm"],
            ),
        )
        self._last_position_saved[icao] = (now_wall, lat, lon)

    def _sweep_stale_sessions(self, now_wall: float) -> None:
        gap = self.settings.adsb_session_gap_seconds
        for icao, entry in list(self._aircraft_cache.items()):
            if now_wall - entry["_last_wall"] > gap:
                self._close_session(entry["session_id"])
                self._last_position_saved.pop(icao, None)
                # mantém no cache mais um pouco para a UI ainda listar como "perdida"
                if now_wall - entry["_last_wall"] > gap + 120:
                    del self._aircraft_cache[icao]

    def _close_session(self, session_id: int) -> None:
        self.db.execute(
            "UPDATE flight_sessions SET is_active=0, end_time=? WHERE id=? AND is_active=1",
            (_now_iso(), session_id),
        )

    def _close_all_active_sessions(self) -> None:
        with self._state_lock:
            for entry in self._aircraft_cache.values():
                self._close_session(entry["session_id"])

    # ------------------------------------------------------------------ #
    # API para as rotas HTTP
    # ------------------------------------------------------------------ #
    def get_status(self) -> dict:
        with self._state_lock:
            stats = dict(self._stats)
        stats["running"] = self.is_running()
        return stats

    def get_aircraft(self, sort_by: str | None = None) -> list[dict]:
        now_wall = time.time()
        stale_after = self.settings.adsb_stale_seconds
        out = []
        with self._state_lock:
            entries = list(self._aircraft_cache.values())
        for entry in entries:
            seen = now_wall - entry["_last_wall"] + (entry.get("seen") or 0)
            if now_wall - entry["_last_wall"] > stale_after:
                continue
            item = {
                "icao": entry["icao"],
                "callsign": entry["callsign"],
                "squawk": entry["squawk"],
                "category": entry["category"],
                "emergency": entry["emergency"],
                "altitude_baro": entry["altitude_baro"],
                "altitude_geom": entry["altitude_geom"],
                "ground_speed": entry["ground_speed"],
                "ias": entry["ias"],
                "tas": entry["tas"],
                "track": entry["track"],
                "heading": entry["heading"],
                "vertical_rate": entry["vertical_rate"],
                "lat": entry["lat"],
                "lon": entry["lon"],
                "position_from_mlat": entry["position_from_mlat"],
                "messages": entry["messages"],
                "rssi": entry["rssi"],
                "seen": round(seen, 1),
                "distance_nm": round(entry["distance_nm"], 2) if entry["distance_nm"] is not None else None,
                "session_id": entry["session_id"],
                "state": entry["state_info"],
            }
            out.append(item)

        sort_keys = {
            "distance": lambda x: (x["distance_nm"] is None, x["distance_nm"] or 0),
            "altitude": lambda x: (x["altitude_baro"] is None, x["altitude_baro"] or 0),
            "rssi": lambda x: (x["rssi"] is None, -(x["rssi"] or -999)),
            "callsign": lambda x: x["callsign"] or "",
        }
        if sort_by in sort_keys:
            out.sort(key=sort_keys[sort_by])
        return out

    def get_aircraft_detail(self, icao: str) -> dict | None:
        icao = icao.upper()
        with self._state_lock:
            entry = self._aircraft_cache.get(icao)
        if entry is None:
            return None
        aircraft = next((a for a in self.get_aircraft() if a["icao"] == icao), None)
        if aircraft is None:
            return None
        session_row = self.db.query_one("SELECT * FROM flight_sessions WHERE id=?", (entry["session_id"],))
        positions = self.db.query(
            "SELECT timestamp, lat, lon, altitude_baro, ground_speed, track, vertical_rate, distance_nm "
            "FROM positions WHERE flight_session_id=? ORDER BY timestamp ASC",
            (entry["session_id"],),
        )
        aircraft_row = self.db.query_one("SELECT first_seen FROM aircraft WHERE icao=?", (icao,))
        aircraft["first_seen"] = aircraft_row["first_seen"] if aircraft_row else None
        aircraft["session"] = session_row
        aircraft["track_positions"] = positions
        return aircraft
