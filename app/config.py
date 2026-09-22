"""
Configuração central da aplicação.

Lê variáveis de ambiente (e um arquivo .env opcional na raiz do projeto,
formato KEY=VALUE simples) e expõe um objeto Settings único usado por
todo o resto do sistema.

Não inventamos valores sensíveis (posição do receptor, frequências) -
eles ficam em branco até o usuário configurar.
"""
from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Parser minimalista de .env: KEY=VALUE por linha, sem dependências externas.

    Não sobrescreve variáveis já presentes no ambiente (o ambiente tem prioridade).
    """
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv(BASE_DIR / ".env")


def _env_float(name: str, default: str | None = None) -> float | None:
    val = os.environ.get(name, default)
    if val is None or val == "":
        return None
    try:
        return float(val)
    except ValueError:
        return None


def _env_str(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


@dataclass
class Settings:
    base_dir: Path = BASE_DIR

    # --- Servidor web -----------------------------------------------------
    host: str = field(default_factory=lambda: _env_str("HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _env_int("PORT", 5000))
    log_level: str = field(default_factory=lambda: _env_str("LOG_LEVEL", "INFO"))

    # --- Diretórios ---------------------------------------------------------
    data_dir: Path = field(default_factory=lambda: BASE_DIR / "data")
    recordings_dir: Path = field(default_factory=lambda: BASE_DIR / "recordings")
    config_dir: Path = field(default_factory=lambda: BASE_DIR / "config")

    # --- Posição do receptor (obrigatório para distância/mapa correto) -----
    receiver_lat: float | None = field(default_factory=lambda: _env_float("RECEIVER_LAT"))
    receiver_lon: float | None = field(default_factory=lambda: _env_float("RECEIVER_LON"))
    receiver_label: str = field(default_factory=lambda: _env_str("RECEIVER_LABEL", "Receptor"))

    # --- dump1090-mutability -------------------------------------------------
    dump1090_bin: str = field(default_factory=lambda: _env_str("DUMP1090_BIN", "dump1090-mutability"))
    dump1090_gain: str = field(default_factory=lambda: _env_str("DUMP1090_GAIN", "auto"))
    dump1090_max_range_nm: int = field(default_factory=lambda: _env_int("DUMP1090_MAX_RANGE_NM", 300))
    dump1090_extra_args: list[str] = field(
        default_factory=lambda: shlex.split(_env_str("DUMP1090_EXTRA_ARGS", ""))
    )

    # --- rtl_fm / rádio -------------------------------------------------------
    rtl_fm_bin: str = field(default_factory=lambda: _env_str("RTL_FM_BIN", "rtl_fm"))
    aplay_bin: str = field(default_factory=lambda: _env_str("APLAY_BIN", "aplay"))
    radio_sample_rate: int = field(default_factory=lambda: _env_int("RADIO_SAMPLE_RATE", 48000))
    radio_default_gain: str = field(default_factory=lambda: _env_str("RADIO_DEFAULT_GAIN", "auto"))
    radio_default_squelch_dbfs: float = field(
        default_factory=lambda: _env_float("RADIO_DEFAULT_SQUELCH_DBFS", "-35") or -35.0
    )
    radio_default_silence_timeout: float = field(
        default_factory=lambda: _env_float("RADIO_DEFAULT_SILENCE_TIMEOUT", "2.0") or 2.0
    )
    radio_default_prebuffer_sec: float = field(
        default_factory=lambda: _env_float("RADIO_DEFAULT_PREBUFFER_SEC", "1.5") or 1.5
    )
    radio_min_recording_sec: float = field(
        default_factory=lambda: _env_float("RADIO_MIN_RECORDING_SEC", "0.6") or 0.6
    )

    # --- ADS-B: sessões / posições -------------------------------------------
    adsb_session_gap_seconds: int = field(
        default_factory=lambda: _env_int("ADSB_SESSION_GAP_SECONDS", 300)
    )
    adsb_position_min_interval_sec: float = field(
        default_factory=lambda: _env_float("ADSB_POSITION_MIN_INTERVAL_SEC", "3") or 3.0
    )
    adsb_position_min_distance_m: float = field(
        default_factory=lambda: _env_float("ADSB_POSITION_MIN_DISTANCE_M", "50") or 50.0
    )
    adsb_summary_interval_sec: float = field(
        default_factory=lambda: _env_float("ADSB_SUMMARY_INTERVAL_SEC", "10") or 10.0
    )
    adsb_stale_seconds: float = field(
        default_factory=lambda: _env_float("ADSB_STALE_SECONDS", "60") or 60.0
    )

    # --- SDRManager: confirmação de start ao trocar de modo -----------------
    # Tempo que o SDRManager aguarda, após lançar o subprocesso, para
    # confirmar que ele não morreu de imediato (ex.: "device busy") antes de
    # declarar o modo como ativo.
    adsb_start_confirm_sec: float = field(
        default_factory=lambda: _env_float("ADSB_START_CONFIRM_SEC", "1.2") or 1.2
    )
    radio_start_confirm_sec: float = field(
        default_factory=lambda: _env_float("RADIO_START_CONFIRM_SEC", "0.8") or 0.8
    )

    @property
    def db_path(self) -> Path:
        return self.data_dir / "aviation.db"

    @property
    def dump1090_json_dir(self) -> Path:
        return self.data_dir / "dump1090_json"

    @property
    def frequencies_file(self) -> Path:
        return self.config_dir / "frequencies.json"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.recordings_dir.mkdir(parents=True, exist_ok=True)
        self.dump1090_json_dir.mkdir(parents=True, exist_ok=True)
        self.config_dir.mkdir(parents=True, exist_ok=True)

    @property
    def receiver_position_configured(self) -> bool:
        return self.receiver_lat is not None and self.receiver_lon is not None


def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings
