#!/usr/bin/env python3
"""
Ponto de entrada da aplicação.

- limpa processos órfãos de dump1090/rtl_fm de execuções anteriores, para
  garantir posse exclusiva do RTL-SDR sem precisar matar nada manualmente;
- registra handlers de SIGINT/SIGTERM para encerrar os subprocessos e o
  banco de dados de forma limpa;
- sobe o servidor Flask (apenas em localhost, sem o reloader do Werkzeug -
  o reloader iniciaria um segundo processo e duplicaria o SDRManager).
"""
from __future__ import annotations

import atexit
import logging
import os
import signal
import sys

from app import create_app
from app.procutil import cleanup_stray_processes

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("luliradar.run")


def main() -> None:
    cleanup_stray_processes(logger)

    app = create_app()
    settings = app.extensions["settings"]
    sdr_manager = app.extensions["sdr_manager"]
    db = app.extensions["db"]

    if not settings.receiver_position_configured:
        logger.warning(
            "RECEIVER_LAT/RECEIVER_LON não configurados (.env) - distâncias e posição do "
            "receptor no mapa ficarão indisponíveis até você configurar."
        )

    shutdown_state = {"done": False}

    def _shutdown(*_args) -> None:
        if shutdown_state["done"]:
            return
        shutdown_state["done"] = True
        logger.info("Encerrando servidor - liberando RTL-SDR...")
        try:
            sdr_manager.shutdown()
        finally:
            db.close()

    def _signal_handler(signum, _frame):
        logger.info("Sinal %s recebido", signal.Signals(signum).name)
        _shutdown()
        os._exit(0)

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)
    atexit.register(_shutdown)

    logger.info("SDR AVIATION MONITOR disponível em http://%s:%s", settings.host, settings.port)
    try:
        app.run(host=settings.host, port=settings.port, threaded=True, use_reloader=False)
    finally:
        _shutdown()


if __name__ == "__main__":
    sys.exit(main() or 0)
