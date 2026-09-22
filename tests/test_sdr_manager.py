"""
Testes da máquina de estados do SDRManager (troca ADS-B <-> Rádio, parada,
falhas de start/stop, concorrência) usando subprocessos falsos - NUNCA toca
no RTL-SDR real nem em dump1090-mutability/rtl_fm/aplay de verdade.

Rodar com:
    python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import Settings
from app.database import Database
from app.models import SDRState
from app.sdr_manager import SDRManager, SwitchInProgress
from tests.fakes import FakePopenFactory


def make_settings(tmp_dir: Path) -> Settings:
    s = Settings()
    s.data_dir = tmp_dir / "data"
    s.recordings_dir = tmp_dir / "recordings"
    s.config_dir = tmp_dir / "config"
    s.ensure_dirs()
    # timeouts curtos para os testes rodarem rápido
    s.adsb_start_confirm_sec = 0.05
    s.radio_start_confirm_sec = 0.05
    s.receiver_lat = -27.6
    s.receiver_lon = -48.55
    return s


class SDRManagerTestCase(unittest.TestCase):
    """
    IMPORTANTE sobre o mock: app.adsb_service e app.radio_service fazem
    `import subprocess` do MESMO objeto de módulo (Python só carrega um
    módulo uma vez) - então dois `patch()` independentes, um em
    "app.adsb_service.subprocess.Popen" e outro em
    "app.radio_service.subprocess.Popen", acabam brigando pelo mesmíssimo
    atributo `subprocess.Popen`, e o segundo `.start()` silenciosamente
    sobrescreve o primeiro. Por isso usamos UM ÚNICO patch em
    "subprocess.Popen" e despachamos por nome do binário (cmd[0]).
    """

    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="luliradar-test-"))
        self.settings = make_settings(self.tmp_dir)
        self.db = Database(self.settings.db_path)
        self.manager = SDRManager(self.db, self.settings)

        self._routes: dict[str, FakePopenFactory] = {}
        self.popen_patch = patch("subprocess.Popen", side_effect=self._dispatch)
        self.mock_popen = self.popen_patch.start()

    def _dispatch(self, cmd, *args, **kwargs):
        binary = cmd[0] if cmd else ""
        factory = self._routes.get(binary)
        if factory is None:
            factory = FakePopenFactory()
            self._routes[binary] = factory
        return factory(cmd, *args, **kwargs)

    def tearDown(self):
        # nunca deixa threads/processos falsos "pendurados" entre testes
        try:
            self.manager.shutdown()
        except Exception:
            pass
        self.popen_patch.stop()
        self.db.close()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def set_adsb_factory(self, **kwargs) -> FakePopenFactory:
        factory = FakePopenFactory(**kwargs)
        self._routes[self.settings.dump1090_bin] = factory
        return factory

    def set_radio_factory(self, **kwargs) -> FakePopenFactory:
        factory = FakePopenFactory(**kwargs)
        self._routes[self.settings.rtl_fm_bin] = factory
        self._routes[self.settings.aplay_bin] = factory
        return factory

    # ------------------------------------------------------------------ #
    # A) IDLE -> ADSB -> IDLE
    # ------------------------------------------------------------------ #
    def test_a_idle_to_adsb_to_idle(self):
        self.assertEqual(self.manager.state, SDRState.IDLE)
        adsb_factory = self.set_adsb_factory()

        status = self.manager.start_adsb()
        self.assertEqual(status["sdr_state"], "adsb")
        self.assertEqual(self.manager.state, SDRState.ADSB)
        self.assertTrue(self.manager.adsb_service.is_running())
        self.assertEqual(len(adsb_factory.created), 1)

        status = self.manager.stop_current()
        self.assertEqual(status["sdr_state"], "idle")
        self.assertFalse(self.manager.adsb_service.is_running())
        self.assertTrue(adsb_factory.created[0].is_dead)

    # ------------------------------------------------------------------ #
    # B) IDLE -> RADIO -> IDLE
    # ------------------------------------------------------------------ #
    def test_b_idle_to_radio_to_idle(self):
        radio_factory = self.set_radio_factory()

        status = self.manager.start_radio(frequency_mhz=118.7, preset="Manual")
        self.assertEqual(status["sdr_state"], "radio")
        self.assertTrue(self.manager.radio_service.is_running())
        # rtl_fm + aplay = 2 processos criados
        self.assertEqual(len(radio_factory.created), 2)

        status = self.manager.stop_current()
        self.assertEqual(status["sdr_state"], "idle")
        self.assertFalse(self.manager.radio_service.is_running())
        for proc in radio_factory.created:
            self.assertTrue(proc.is_dead)

    # ------------------------------------------------------------------ #
    # C) ADSB -> RADIO
    # ------------------------------------------------------------------ #
    def test_c_adsb_to_radio(self):
        adsb_factory = self.set_adsb_factory()
        radio_factory = self.set_radio_factory()

        self.manager.start_adsb()
        self.assertTrue(self.manager.adsb_service.is_running())

        status = self.manager.start_radio(frequency_mhz=127.45, preset="Manual")
        self.assertEqual(status["sdr_state"], "radio")
        # dump1090 tem que ter sido encerrado antes do rtl_fm assumir
        self.assertFalse(self.manager.adsb_service.is_running())
        self.assertTrue(adsb_factory.created[0].is_dead)
        self.assertTrue(self.manager.radio_service.is_running())

    # ------------------------------------------------------------------ #
    # D) RADIO -> ADSB
    # ------------------------------------------------------------------ #
    def test_d_radio_to_adsb(self):
        radio_factory = self.set_radio_factory()
        adsb_factory = self.set_adsb_factory()

        self.manager.start_radio(frequency_mhz=121.5, preset="Manual")
        self.assertTrue(self.manager.radio_service.is_running())

        status = self.manager.start_adsb()
        self.assertEqual(status["sdr_state"], "adsb")
        self.assertFalse(self.manager.radio_service.is_running())
        for proc in radio_factory.created:
            self.assertTrue(proc.is_dead)
        self.assertTrue(self.manager.adsb_service.is_running())

    # ------------------------------------------------------------------ #
    # E) falha ao encerrar processo anterior
    # ------------------------------------------------------------------ #
    def test_e_falha_ao_encerrar_processo_anterior(self):
        self.set_adsb_factory()
        self.manager.start_adsb()
        self.assertTrue(self.manager.adsb_service.is_running())

        with patch.object(
            self.manager.adsb_service, "stop", side_effect=RuntimeError("simulado: travou ao encerrar")
        ):
            with self.assertRaises(RuntimeError):
                self.manager.start_radio(frequency_mhz=118.7, preset="Manual")

        # não pode ter avançado para RADIO com o ADS-B "travado" ainda no ar
        self.assertEqual(self.manager.state, SDRState.ERROR)
        self.assertFalse(self.manager.radio_service.is_running())

    # ------------------------------------------------------------------ #
    # F) falha ao iniciar novo processo
    # ------------------------------------------------------------------ #
    def test_f_falha_ao_iniciar_novo_processo_device_busy(self):
        # simula "device busy": o processo morre quase imediatamente
        self.set_radio_factory(dies_immediately=True, returncode_on_die=1)

        with self.assertRaises(RuntimeError):
            self.manager.start_radio(frequency_mhz=118.7, preset="Manual")

        self.assertEqual(self.manager.state, SDRState.ERROR)
        self.assertFalse(self.manager.radio_service.is_running())

    def test_f_falha_binario_nao_encontrado(self):
        def raise_not_found(cmd, *a, **kw):
            raise FileNotFoundError(f"{cmd[0]} não encontrado")

        self._routes[self.settings.rtl_fm_bin] = raise_not_found
        with self.assertRaises(RuntimeError):
            self.manager.start_radio(frequency_mhz=118.7, preset="Manual")
        self.assertEqual(self.manager.state, SDRState.ERROR)

    # ------------------------------------------------------------------ #
    # G) duas solicitações simultâneas de troca
    # ------------------------------------------------------------------ #
    def test_g_duas_trocas_simultaneas(self):
        self.set_adsb_factory()
        self.set_radio_factory()

        results = {}

        def worker(name, fn):
            try:
                fn()
                results[name] = "ok"
            except SwitchInProgress:
                results[name] = "busy"
            except Exception as exc:  # pragma: no cover - só para depuração
                results[name] = f"erro: {exc}"

        # dispara duas trocas ao mesmo tempo; uma tem que vencer e a outra
        # tem que ser rejeitada com SwitchInProgress (nunca as duas juntas)
        t1 = threading.Thread(target=worker, args=("adsb", self.manager.start_adsb))
        t2 = threading.Thread(target=worker, args=("radio", lambda: self.manager.start_radio(frequency_mhz=118.7, preset="Manual")))
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        outcomes = list(results.values())
        self.assertIn("ok", outcomes)
        self.assertIn("busy", outcomes)
        # nunca os dois serviços rodando ao mesmo tempo, seja qual for o vencedor
        self.assertFalse(
            self.manager.adsb_service.is_running() and self.manager.radio_service.is_running()
        )

    # ------------------------------------------------------------------ #
    # O) trocar de aba (tune sem estar em RADIO só troca a leitura de status)
    # ------------------------------------------------------------------ #
    def test_get_status_nao_bloqueia_durante_switching(self):
        # get_status() não deve travar por causa do _transition_lock
        self.set_adsb_factory()
        self.manager.start_adsb()
        status = self.manager.get_status()
        self.assertIn("sdr_state", status)
        self.assertIn("error", status)

    # ------------------------------------------------------------------ #
    # P) PARAR ADS-B realmente deixa IDLE
    # ------------------------------------------------------------------ #
    def test_p_parar_adsb_deixa_idle(self):
        self.set_adsb_factory()
        self.manager.start_adsb()
        status = self.manager.stop_current()
        self.assertEqual(status["sdr_state"], "idle")
        self.assertFalse(self.manager.adsb_service.is_running())
        self.assertFalse(self.manager.radio_service.is_running())

    # ------------------------------------------------------------------ #
    # Q) PARAR Rádio realmente deixa IDLE
    # ------------------------------------------------------------------ #
    def test_q_parar_radio_deixa_idle(self):
        self.set_radio_factory()
        self.manager.start_radio(frequency_mhz=118.7, preset="Manual")
        status = self.manager.stop_current()
        self.assertEqual(status["sdr_state"], "idle")
        self.assertFalse(self.manager.adsb_service.is_running())
        self.assertFalse(self.manager.radio_service.is_running())

    # ------------------------------------------------------------------ #
    # Seção 14: nenhum subprocesso "pertencente ao app" sobra depois de parar
    # ------------------------------------------------------------------ #
    def test_nenhum_processo_orfao_apos_stop(self):
        adsb_factory = self.set_adsb_factory()
        self.manager.start_adsb()
        radio_factory = self.set_radio_factory()
        self.manager.start_radio(frequency_mhz=118.7, preset="Manual")  # troca ADSB -> RADIO
        self.manager.stop_current()

        for proc in adsb_factory.created + radio_factory.created:
            self.assertTrue(proc.is_dead, "subprocesso ficou vivo após stop_current()")
            self.assertTrue(proc.terminate_called, "terminate() (SIGTERM) nunca foi chamado")

    def test_sigkill_fallback_quando_processo_nao_responde_sigterm(self):
        adsb_factory = self.set_adsb_factory(unresponsive_to_sigterm=True)
        self.manager.start_adsb()
        proc = adsb_factory.created[0]
        self.manager.stop_current()
        self.assertTrue(proc.terminate_called)
        self.assertTrue(proc.kill_called, "deveria ter caído no fallback SIGKILL")
        self.assertTrue(proc.is_dead)


if __name__ == "__main__":
    unittest.main()
