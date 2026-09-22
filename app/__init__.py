"""Application factory."""
from __future__ import annotations

import logging

from flask import Flask

from .config import Settings, get_settings
from .database import Database
from .sdr_manager import SDRManager


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def create_app(settings: Settings | None = None) -> Flask:
    """`settings` normalmente é omitido (usa get_settings(), lendo .env/env
    vars de verdade) - o parâmetro existe para os testes automatizados
    injetarem uma Settings apontando para diretórios temporários, para nunca
    tocar em data/aviation.db ou recordings/ reais."""
    settings = settings or get_settings()
    _configure_logging(settings.log_level)

    app = Flask(__name__, template_folder="../templates", static_folder="../static")

    db = Database(settings.db_path)
    sdr_manager = SDRManager(db, settings)

    app.extensions["settings"] = settings
    app.extensions["db"] = db
    app.extensions["sdr_manager"] = sdr_manager

    from .routes import bp

    app.register_blueprint(bp)

    return app
