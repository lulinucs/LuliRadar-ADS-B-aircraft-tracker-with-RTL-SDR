"""
Modelagem leve dos dados manipulados pela aplicação.

Não usamos um ORM (seria overengineering para este projeto). Este módulo
concentra:
  - o enum de estados do SDRManager;
  - os rótulos usados pela classificação de estado de voo (ver
    state_classifier.py), guardados aqui para ficarem num único lugar;
  - a normalização de um registro de aeronave vindo do aircraft.json do
    dump1090-mutability para um dicionário com nomes de campo estáveis,
    independente da variante exata do dump1090 instalada.

O dump1090-mutability (build clássico, "mutability") expõe por aeronave:
    hex, flight, squawk, category, altitude, vert_rate, speed, track,
    lat, lon, seen, seen_pos, messages, rssi, mlat
Forks mais novos (dump1090-fa / readsb) usam nomes como alt_baro, alt_geom,
gs, ias, tas, mag_heading, baro_rate, etc. Para não travar caso o binário
seja trocado no futuro, tentamos várias chaves conhecidas por campo e
preservamos o registro bruto inteiro (campo 'raw') para nunca perder dado.
"""
from __future__ import annotations

from enum import Enum
from typing import Any


class SDRState(str, Enum):
    IDLE = "idle"
    ADSB = "adsb"
    RADIO = "radio"
    SWITCHING = "switching"
    ERROR = "error"


def _first(d: dict, *keys, default=None):
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def _clean_callsign(value: Any) -> str | None:
    if value is None:
        return None
    v = str(value).strip()
    return v or None


def normalize_aircraft(ac: dict, receiver_ts: float) -> dict:
    """Converte um registro bruto do aircraft.json num dict com campos estáveis.

    Mantém tudo que o dump1090 disponibilizar: o dicionário original fica em
    'raw' para consulta/depuração e para não descartar nenhum campo.
    """
    icao = str(_first(ac, "hex", "icao", default="")).upper().strip()

    lat = _first(ac, "lat")
    lon = _first(ac, "lon")

    mlat_fields = ac.get("mlat") or []
    position_from_mlat = isinstance(mlat_fields, list) and (
        "lat" in mlat_fields or "lon" in mlat_fields
    )

    return {
        "icao": icao,
        "callsign": _clean_callsign(_first(ac, "flight", "call")),
        "squawk": _first(ac, "squawk"),
        "category": _first(ac, "category"),
        "emergency": _first(ac, "emergency"),
        "altitude_baro": _first(ac, "alt_baro", "altitude"),
        "altitude_geom": _first(ac, "alt_geom"),
        "ground_speed": _first(ac, "gs", "speed"),
        "ias": _first(ac, "ias"),
        "tas": _first(ac, "tas"),
        "track": _first(ac, "track"),
        "heading": _first(ac, "mag_heading", "true_heading", "heading"),
        "vertical_rate": _first(ac, "vert_rate", "baro_rate", "geom_rate"),
        "lat": lat,
        "lon": lon,
        "has_position": lat is not None and lon is not None,
        "position_from_mlat": position_from_mlat,
        "messages": _first(ac, "messages", default=0),
        "rssi": _first(ac, "rssi"),
        "seen": _first(ac, "seen", default=0.0),
        "seen_pos": _first(ac, "seen_pos"),
        "nic": _first(ac, "nic"),
        "receiver_ts": receiver_ts,
        "raw": ac,
    }
