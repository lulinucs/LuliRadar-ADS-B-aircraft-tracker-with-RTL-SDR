"""Cálculo de distância entre coordenadas geográficas (fórmula de Haversine)."""
from __future__ import annotations

import math

EARTH_RADIUS_NM = 3440.065
EARTH_RADIUS_KM = 6371.0088


def haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distância em milhas náuticas entre dois pontos (lat/lon em graus)."""
    return _haversine(lat1, lon1, lat2, lon2, EARTH_RADIUS_NM)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distância em quilômetros entre dois pontos (lat/lon em graus)."""
    return _haversine(lat1, lon1, lat2, lon2, EARTH_RADIUS_KM)


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float, radius: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return radius * c
