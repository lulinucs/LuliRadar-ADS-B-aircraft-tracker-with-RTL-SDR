"""Utilitários de gerenciamento de subprocessos usados pelos serviços SDR."""
from __future__ import annotations

import logging
import subprocess
import time

logger = logging.getLogger("luliradar.procutil")


def terminate_process(proc: subprocess.Popen | None, name: str, term_timeout: float = 3.0) -> None:
    """Encerra um subprocesso de forma limpa: SIGTERM, aguarda, SIGKILL se preciso."""
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
    except Exception:
        logger.exception("Falha ao enviar SIGTERM para %s (pid=%s)", name, proc.pid)
    try:
        proc.wait(timeout=term_timeout)
        logger.info("%s (pid=%s) encerrado", name, proc.pid)
        return
    except subprocess.TimeoutExpired:
        logger.warning("%s (pid=%s) não respondeu a SIGTERM, enviando SIGKILL", name, proc.pid)
    try:
        proc.kill()
        proc.wait(timeout=2.0)
    except Exception:
        logger.exception("Falha ao forçar encerramento de %s (pid=%s)", name, proc.pid)


def cleanup_stray_processes(logger_: logging.Logger) -> None:
    """Limpeza best-effort de processos órfãos de execuções anteriores.

    Esta aplicação assume posse EXCLUSIVA do único RTL-SDR conectado. Antes
    de subir o servidor, garantimos que nenhum dump1090/rtl_fm de uma sessão
    anterior (ou iniciado manualmente pelo usuário, ex.: teste com
    `dump1090-mutability --interactive`) ainda esteja segurando o dongle -
    caso contrário o primeiro start do nosso app falharia com "device busy".
    """
    patterns = ["dump1090-mutability", "rtl_fm"]
    found_any = False
    for pattern in patterns:
        try:
            result = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True)
        except FileNotFoundError:
            logger_.debug("pgrep não disponível; pulando limpeza de processos órfãos")
            return
        pids = result.stdout.split()
        if result.returncode == 0 and pids:
            found_any = True
            logger_.warning(
                "Processo pré-existente detectado (%s, pid=%s) - encerrando para liberar o RTL-SDR",
                pattern,
                ",".join(pids),
            )
            subprocess.run(["pkill", "-TERM", "-f", pattern])
    if found_any:
        time.sleep(0.7)
        for pattern in patterns:
            subprocess.run(["pkill", "-KILL", "-f", pattern])
        time.sleep(0.8)  # dá tempo ao kernel de liberar a interface USB
