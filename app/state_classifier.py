"""
Classificação SIMPLES e explicitamente marcada como INFERÊNCIA do estado de
uma aeronave, a partir de altitude, taxa vertical e histórico recente de
distância ao receptor.

Isso NÃO é uma detecção oficial de fase de voo nem de aproximação a um
aeroporto - é só uma heurística de exibição. O texto é sempre qualificado
("provável...") para deixar isso claro na interface.

Estrutura pensada para, no futuro, ganhar lógica específica de aproximação
a um aeroporto conhecido (comparando posição/altitude/rumo com a pista),
sem precisar mudar o formato do retorno.
"""
from __future__ import annotations

LOW_ALTITUDE_FT = 3000
VERTICAL_RATE_THRESHOLD_FPM = 150
DISTANCE_TREND_MIN_SAMPLES = 3
DISTANCE_TREND_EPSILON_NM = 0.15


def classify_aircraft_state(aircraft: dict, distance_history: list[float]) -> dict:
    """Retorna um dicionário descrevendo o estado inferido da aeronave.

    aircraft: dict normalizado (ver models.normalize_aircraft)
    distance_history: lista de distâncias (nm) mais recentes, em ordem
                       cronológica (a última é a mais recente)
    """
    altitude = aircraft.get("altitude_baro")
    vertical_rate = aircraft.get("vertical_rate")
    seen = aircraft.get("seen") or 0

    if seen is not None and seen > 60:
        return {
            "vertical": "desconhecido",
            "proximity": "desconhecido",
            "label": "Sinal perdido",
            "code": "PERDIDO",
            "inference": True,
        }

    # --- tendência vertical -------------------------------------------------
    if vertical_rate is None:
        vertical_code, vertical_label = "DESCONHECIDO", "altitude estável (sem dado)"
    elif vertical_rate > VERTICAL_RATE_THRESHOLD_FPM:
        vertical_code, vertical_label = "SUBINDO", "provável subida"
    elif vertical_rate < -VERTICAL_RATE_THRESHOLD_FPM:
        vertical_code, vertical_label = "DESCENDO", "provável descida"
    elif altitude is not None and altitude < LOW_ALTITUDE_FT:
        vertical_code, vertical_label = "BAIXA_ALTITUDE", "baixa altitude"
    else:
        vertical_code, vertical_label = "CRUZEIRO", "provável cruzeiro"

    # --- tendência de distância ao receptor ---------------------------------
    proximity_code, proximity_label = "ESTAVEL", "distância estável"
    hist = [d for d in distance_history if d is not None]
    if len(hist) >= DISTANCE_TREND_MIN_SAMPLES:
        delta = hist[-1] - hist[0]
        if delta < -DISTANCE_TREND_EPSILON_NM:
            proximity_code, proximity_label = "APROXIMANDO", "aproximando-se do receptor"
        elif delta > DISTANCE_TREND_EPSILON_NM:
            proximity_code, proximity_label = "AFASTANDO", "afastando-se do receptor"

    label_parts = [vertical_label]
    if proximity_code != "ESTAVEL":
        label_parts.append(proximity_label)
    label = ", ".join(label_parts).capitalize()

    return {
        "vertical": vertical_code,
        "proximity": proximity_code,
        "label": label,
        "code": vertical_code,
        "inference": True,
    }
