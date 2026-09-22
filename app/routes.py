"""Rotas HTTP (páginas + API JSON)."""
from __future__ import annotations

import json
import logging

from flask import Blueprint, current_app, jsonify, render_template, request, send_from_directory
from werkzeug.exceptions import HTTPException

from .sdr_manager import SwitchInProgress

logger = logging.getLogger("luliradar.routes")

# Faixa de sintonia suportada pelo RTL-SDR Blog V4 (chip R820T2), em MHz.
# Serve de rede de segurança contra erros de dígito (ex.: "118100" em vez de
# "118.100") que de outra forma passariam batidos por todas as camadas.
RADIO_FREQ_MIN_MHZ = 20.0
RADIO_FREQ_MAX_MHZ = 1800.0

# Tolerância (MHz) para considerar que uma frequência sintonizada "é" um
# preset conhecido - cobre arredondamento de ponto flutuante, não a
# intenção de aceitar frequências próximas mas diferentes.
PRESET_FREQ_EPSILON_MHZ = 0.0005

bp = Blueprint("main", __name__)


def _sdr_manager():
    return current_app.extensions["sdr_manager"]


def _settings():
    return current_app.extensions["settings"]


def _db():
    return current_app.extensions["db"]


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


@bp.errorhandler(ApiError)
def _handle_api_error(err: ApiError):
    return jsonify({"error": err.message}), err.status


@bp.errorhandler(SwitchInProgress)
def _handle_switch_in_progress(err: SwitchInProgress):
    return jsonify({"error": str(err)}), 409


@bp.errorhandler(HTTPException)
def _handle_http_error(err: HTTPException):
    # Preserva erros HTTP nativos do Flask/Werkzeug (404 de arquivo inexistente,
    # 405 de método errado, etc.) em vez de mascará-los como 500.
    return jsonify({"error": err.description or err.name}), err.code or 500


@bp.errorhandler(Exception)
def _handle_unexpected_error(err: Exception):
    logger.exception("Erro não tratado numa rota")
    return jsonify({"error": f"Erro interno: {err}"}), 500


# ---------------------------------------------------------------------- #
# páginas
# ---------------------------------------------------------------------- #
@bp.route("/")
def index():
    return render_template("index.html")


# ---------------------------------------------------------------------- #
# status geral / troca de modo
# ---------------------------------------------------------------------- #
@bp.route("/api/status")
def api_status():
    return jsonify(_sdr_manager().get_status())


@bp.route("/api/mode/adsb", methods=["POST"])
def api_mode_adsb():
    try:
        status = _sdr_manager().start_adsb()
    except SwitchInProgress:
        raise
    except RuntimeError as exc:
        raise ApiError(str(exc), 500)
    return jsonify(status)


@bp.route("/api/mode/radio", methods=["POST"])
def api_mode_radio():
    body = request.get_json(silent=True) or {}
    params = _resolve_radio_params(body)
    try:
        status = _sdr_manager().start_radio(**params)
    except SwitchInProgress:
        raise
    except RuntimeError as exc:
        raise ApiError(str(exc), 500)
    return jsonify(status)


@bp.route("/api/stop", methods=["POST"])
def api_stop():
    try:
        status = _sdr_manager().stop_current()
    except SwitchInProgress:
        raise
    except RuntimeError as exc:
        raise ApiError(str(exc), 500)
    return jsonify(status)


# ---------------------------------------------------------------------- #
# ADS-B
# ---------------------------------------------------------------------- #
@bp.route("/api/aircraft")
def api_aircraft():
    sort_by = request.args.get("sort")
    return jsonify(_sdr_manager().adsb_service.get_aircraft(sort_by=sort_by))


@bp.route("/api/aircraft/<icao>")
def api_aircraft_detail(icao):
    detail = _sdr_manager().adsb_service.get_aircraft_detail(icao)
    if detail is None:
        raise ApiError(f"Aeronave {icao} não encontrada (fora do alcance atual)", 404)
    return jsonify(detail)


@bp.route("/api/history/flights")
def api_history_flights():
    limit = min(int(request.args.get("limit", 100)), 500)
    icao = request.args.get("icao")
    date = request.args.get("date")  # YYYY-MM-DD
    sql = "SELECT * FROM flight_sessions WHERE 1=1"
    params: list = []
    if icao:
        sql += " AND icao = ?"
        params.append(icao.upper())
    if date:
        sql += " AND start_time LIKE ?"
        params.append(f"{date}%")
    sql += " ORDER BY start_time DESC LIMIT ?"
    params.append(limit)
    return jsonify(_db().query(sql, tuple(params)))


@bp.route("/api/history/flights/<int:session_id>")
def api_history_flight_detail(session_id):
    session_row = _db().query_one("SELECT * FROM flight_sessions WHERE id=?", (session_id,))
    if session_row is None:
        raise ApiError("Sessão não encontrada", 404)
    positions = _db().query(
        "SELECT timestamp, lat, lon, altitude_baro, ground_speed, track, vertical_rate, distance_nm "
        "FROM positions WHERE flight_session_id=? ORDER BY timestamp ASC",
        (session_id,),
    )
    session_row["positions"] = positions
    return jsonify(session_row)


# ---------------------------------------------------------------------- #
# Rádio
# ---------------------------------------------------------------------- #
def _load_presets() -> list[dict]:
    path = _settings().frequencies_file
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        raise ApiError(f"Não foi possível ler config/frequencies.json: {exc}", 500)


def _match_preset_by_frequency(presets: list[dict], frequency_mhz: float) -> str | None:
    for p in presets:
        pf = p.get("frequency_mhz")
        if pf is not None:
            try:
                if abs(float(pf) - frequency_mhz) <= PRESET_FREQ_EPSILON_MHZ:
                    return p.get("name")
            except (TypeError, ValueError):
                continue
    return None


def _resolve_radio_params(body: dict) -> dict:
    """Monta os parâmetros para RadioService.start()/tune().

    Regra importante: o rótulo do preset gravado no histórico NUNCA é
    simplesmente o que o cliente mandou em `preset` - ele é sempre
    recalculado a partir da frequência efetivamente sintonizada, batendo
    contra config/frequencies.json. Isso evita que uma frequência manual
    (ou um valor "grudado" de uma seleção anterior) acabe registrada sob o
    nome de um preset ao qual ela não corresponde. Se nenhuma frequência
    conhecida bater, o preset efetivo é "Manual".
    """
    preset_name = body.get("preset")
    frequency_mhz = body.get("frequency_mhz")
    mode = body.get("mode", "am")
    presets = _load_presets()

    if frequency_mhz is None:
        if not preset_name:
            raise ApiError("Informe 'frequency_mhz' ou um 'preset' válido com frequência configurada", 400)
        preset = next((p for p in presets if p.get("name") == preset_name), None)
        if preset is None:
            raise ApiError(f"Preset '{preset_name}' não encontrado em config/frequencies.json", 404)
        frequency_mhz = preset.get("frequency_mhz")
        mode = preset.get("mode", mode)
        if frequency_mhz is None:
            raise ApiError(
                f"O preset '{preset_name}' ainda não tem frequência configurada. "
                "Edite config/frequencies.json e informe frequency_mhz.",
                400,
            )

    try:
        frequency_mhz = float(frequency_mhz)
    except (TypeError, ValueError):
        raise ApiError("frequency_mhz inválida", 400)

    if not (RADIO_FREQ_MIN_MHZ <= frequency_mhz <= RADIO_FREQ_MAX_MHZ):
        hint = ""
        if RADIO_FREQ_MIN_MHZ <= frequency_mhz / 1000 <= RADIO_FREQ_MAX_MHZ:
            hint = f" Você quis dizer {frequency_mhz / 1000:.3f} MHz?"
        raise ApiError(
            f"Frequência {frequency_mhz:g} MHz fora da faixa suportada pelo RTL-SDR "
            f"({RADIO_FREQ_MIN_MHZ:.0f}–{RADIO_FREQ_MAX_MHZ:.0f} MHz).{hint}",
            400,
        )

    effective_preset = _match_preset_by_frequency(presets, frequency_mhz) or "Manual"

    params = {"frequency_mhz": frequency_mhz, "mode": mode, "preset": effective_preset}
    for key in ("gain", "squelch_dbfs", "volume", "silence_timeout", "prebuffer_sec"):
        if key in body and body[key] is not None:
            params[key] = body[key]
    return params


@bp.route("/api/radio/presets")
def api_radio_presets():
    return jsonify(_load_presets())


@bp.route("/api/radio/tune", methods=["POST"])
def api_radio_tune():
    body = request.get_json(silent=True) or {}
    params = _resolve_radio_params(body)
    try:
        status = _sdr_manager().tune_radio(**params)
    except SwitchInProgress:
        raise
    except RuntimeError as exc:
        raise ApiError(str(exc), 500)
    return jsonify(status)


@bp.route("/api/radio/status")
def api_radio_status():
    return jsonify(_sdr_manager().radio_service.get_status())


@bp.route("/api/radio/recordings")
def api_radio_recordings():
    limit = min(int(request.args.get("limit", 50)), 500)
    return jsonify(_sdr_manager().radio_service.list_recordings(limit=limit))


@bp.route("/recordings/<path:filename>")
def serve_recording(filename):
    return send_from_directory(_settings().recordings_dir, filename)
