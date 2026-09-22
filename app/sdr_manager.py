"""
SDRManager: autoridade central sobre quem pode usar o único RTL-SDR.

Estados: IDLE, ADSB, RADIO, SWITCHING, ERROR.

Duas travas com papéis distintos:
  - `_transition_lock` (Lock comum, adquirida em modo não-bloqueante):
    serializa as trocas de modo em si (start/stop). Uma segunda troca
    solicitada enquanto outra está em andamento é rejeitada de imediato
    com SwitchInProgress, em vez de enfileirar ou correr em paralelo.
  - `_state_lock` (Lock comum, mantida só o tempo de ler/escrever a
    variável de estado): protege `_state`/`_error_message`, e é usada
    também por `get_status()` - por isso uma leitura de status NUNCA fica
    bloqueada esperando uma troca de modo terminar, e consegue observar
    SWITCHING em andamento a partir de outra requisição HTTP concorrente.

Nunca inicia um processo novo sem antes confirmar que o anterior realmente
encerrou (stop() dos serviços já bloqueia até o subprocesso morrer - SIGTERM,
aguarda, SIGKILL como fallback - ver procutil.terminate_process), e nunca
declara um modo "ativo" sem antes confirmar que o novo processo continua de
pé alguns instantes depois de iniciado (protege contra "device busy" e
outras falhas rápidas de inicialização).
"""
from __future__ import annotations

import logging
import threading
import time

from .adsb_service import AdsbService
from .config import Settings
from .database import Database
from .models import SDRState
from .radio_service import RadioService

logger = logging.getLogger("luliradar.sdr_manager")

# Pausa entre encerrar um serviço e iniciar o próximo, para dar tempo ao
# kernel de liberar a interface USB do dongle antes de reabri-la.
_USB_RELEASE_DELAY_SEC = 0.3


class SwitchInProgress(RuntimeError):
    """Já existe uma troca de modo em andamento; tente novamente em instantes."""


class SDRManager:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings

        self._state_lock = threading.Lock()
        self._transition_lock = threading.Lock()
        self._state = SDRState.IDLE
        self._error_message: str | None = None

        self.adsb_service = AdsbService(db, settings)
        self.radio_service = RadioService(db, settings)

    # ------------------------------------------------------------------ #
    # estado (leitura rápida, nunca bloqueada por uma troca em andamento)
    # ------------------------------------------------------------------ #
    @property
    def state(self) -> SDRState:
        with self._state_lock:
            return self._state

    def _set_state(self, state: SDRState, error: str | None = None) -> None:
        with self._state_lock:
            self._state = state
            self._error_message = error
        logger.info("Estado do SDR: %s%s", state.value, f" ({error})" if error else "")

    # ------------------------------------------------------------------ #
    # troca de modo
    # ------------------------------------------------------------------ #
    def _acquire_transition(self) -> None:
        if not self._transition_lock.acquire(blocking=False):
            raise SwitchInProgress("Já existe uma troca de modo em andamento. Aguarde e tente novamente.")

    def _ensure_only(self, keep: str | None) -> None:
        """Garante que nenhum serviço além de `keep` ('adsb'|'radio'|None) esteja
        segurando o dongle. Tenta encerrar ambos mesmo se um falhar, e só então
        relata o erro agregado - nunca deixa dois processos concorrentes."""
        errors: list[str] = []
        if keep != "adsb" and self.adsb_service.is_running():
            logger.info("Encerrando ADS-B (dump1090-mutability)")
            try:
                self.adsb_service.stop()
            except Exception as exc:
                logger.exception("Falha ao encerrar ADS-B")
                errors.append(f"ADS-B: {exc}")
        if keep != "radio" and self.radio_service.is_running():
            logger.info("Encerrando Rádio (rtl_fm/aplay)")
            try:
                self.radio_service.stop()
            except Exception as exc:
                logger.exception("Falha ao encerrar Rádio")
                errors.append(f"Rádio: {exc}")
        if errors:
            raise RuntimeError("Falha ao liberar o SDR: " + "; ".join(errors))

    def _confirm_started(self, service, timeout: float) -> None:
        """Aguarda `timeout` segundos confirmando que o subprocesso recém-lançado
        não morreu de imediato (ex.: dongle ocupado)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not service.is_running():
                err = service.get_status().get("error")
                raise RuntimeError(err or "o processo encerrou logo após iniciar (verifique se o RTL-SDR está livre)")
            time.sleep(0.1)
        if not service.is_running():
            err = service.get_status().get("error")
            raise RuntimeError(err or "o processo encerrou logo após iniciar (verifique se o RTL-SDR está livre)")

    def start_adsb(self) -> dict:
        self._acquire_transition()
        try:
            if self.state == SDRState.ADSB and self.adsb_service.is_running() and not self.radio_service.is_running():
                return self.get_status()  # já ativo, idempotente

            self._set_state(SDRState.SWITCHING)
            try:
                self._ensure_only(keep=None)
                time.sleep(_USB_RELEASE_DELAY_SEC)
                self.adsb_service.start()
                self._confirm_started(self.adsb_service, self.settings.adsb_start_confirm_sec)
            except Exception as exc:
                logger.exception("Falha ao ativar ADS-B")
                try:
                    self._ensure_only(keep=None)
                except Exception:
                    logger.exception("Falha adicional ao limpar após erro de ativação do ADS-B")
                self._set_state(SDRState.ERROR, error=str(exc))
                raise RuntimeError(str(exc)) from exc

            self._set_state(SDRState.ADSB)
            return self.get_status()
        finally:
            self._transition_lock.release()

    def start_radio(self, **radio_params) -> dict:
        self._acquire_transition()
        try:
            self._set_state(SDRState.SWITCHING)
            try:
                self._ensure_only(keep=None)
                time.sleep(_USB_RELEASE_DELAY_SEC)
                self.radio_service.start(**radio_params)
                self._confirm_started(self.radio_service, self.settings.radio_start_confirm_sec)
            except Exception as exc:
                logger.exception("Falha ao ativar Rádio")
                try:
                    self._ensure_only(keep=None)
                except Exception:
                    logger.exception("Falha adicional ao limpar após erro de ativação do Rádio")
                self._set_state(SDRState.ERROR, error=str(exc))
                raise RuntimeError(str(exc)) from exc

            self._set_state(SDRState.RADIO)
            return self.get_status()
        finally:
            self._transition_lock.release()

    def tune_radio(self, **radio_params) -> dict:
        """Reajuste de parâmetros do rádio já ativo. Se o rádio não é o modo
        ativo no momento, isso equivale a assumir o SDR e iniciar o rádio."""
        if self.state != SDRState.RADIO:
            return self.start_radio(**radio_params)
        self._acquire_transition()
        try:
            try:
                self.radio_service.tune(**radio_params)
            except Exception as exc:
                logger.exception("Falha ao sintonizar rádio")
                self._set_state(SDRState.ERROR, error=str(exc))
                raise RuntimeError(str(exc)) from exc
            self._set_state(SDRState.RADIO)
            return self.get_status()
        finally:
            self._transition_lock.release()

    def stop_current(self) -> dict:
        self._acquire_transition()
        try:
            if self.state == SDRState.IDLE and not self.adsb_service.is_running() and not self.radio_service.is_running():
                return self.get_status()  # já ocioso, idempotente

            self._set_state(SDRState.SWITCHING)
            try:
                self._ensure_only(keep=None)
            except Exception as exc:
                logger.exception("Falha ao parar SDR")
                self._set_state(SDRState.ERROR, error=str(exc))
                raise RuntimeError(str(exc)) from exc

            self._set_state(SDRState.IDLE)
            return self.get_status()
        finally:
            self._transition_lock.release()

    # ------------------------------------------------------------------ #
    def get_status(self) -> dict:
        with self._state_lock:
            state = self._state
            error_message = self._error_message
        return {
            "sdr_state": state.value,
            "error": error_message,
            "receiver": {
                "lat": self.settings.receiver_lat,
                "lon": self.settings.receiver_lon,
                "label": self.settings.receiver_label,
                "configured": self.settings.receiver_position_configured,
            },
            "adsb": self.adsb_service.get_status(),
            "radio": self.radio_service.get_status(),
        }

    def shutdown(self) -> None:
        """Chamado no encerramento do servidor (SIGINT/SIGTERM/atexit).

        Aqui a espera pelo `_transition_lock` é bloqueante de propósito (ao
        contrário das trocas normais): se uma troca estiver em andamento,
        aguardamos ela terminar (tipicamente < 2s, limitado pelos timeouts de
        confirmação/encerramento) para então parar tudo, em vez de arriscar
        derrubar um subprocesso no meio de sua inicialização.
        """
        with self._transition_lock:
            logger.info("Encerrando SDRManager - parando qualquer processo ativo")
            try:
                self.adsb_service.stop()
            except Exception:
                logger.exception("Erro ao parar ADS-B durante shutdown")
            try:
                self.radio_service.stop()
            except Exception:
                logger.exception("Erro ao parar rádio durante shutdown")
            self._set_state(SDRState.IDLE)
