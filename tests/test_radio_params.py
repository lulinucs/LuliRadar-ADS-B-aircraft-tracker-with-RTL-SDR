"""
Testes de resolução de preset e conversão de unidade de frequência (rastreia
o valor de config/frequencies.json -> routes._resolve_radio_params ->
RadioService.start() -> comando rtl_fm). Usa o config/frequencies.json real
do projeto (valida o conteúdo realmente publicado), mas isola dados/
gravações em diretório temporário - nunca toca no RTL-SDR real.

Rodar com:
    python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import create_app
from app.config import Settings
from app.radio_service import RadioService
from app.routes import ApiError, _resolve_radio_params
from tests.fakes import FakePopenFactory


class ResolveRadioParamsTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="luliradar-test-"))
        settings = Settings()
        # isola dados/gravações em diretório temporário - nunca toca em
        # data/aviation.db ou recordings/ reais. config_dir fica apontando
        # pro config/ real do projeto DE PROPÓSITO, para validar os presets
        # realmente publicados em config/frequencies.json.
        settings.data_dir = self.tmp_dir / "data"
        settings.recordings_dir = self.tmp_dir / "recordings"
        settings.ensure_dirs()
        self.settings = settings

        self.app = create_app(settings=settings)
        self.ctx = self.app.app_context()
        self.ctx.push()

    def tearDown(self):
        self.ctx.pop()
        self.app.extensions["db"].close()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    # ------------------------------------------------------------------ #
    # H) preset configurado corretamente
    # ------------------------------------------------------------------ #
    def test_h_preset_torre(self):
        params = _resolve_radio_params({"preset": "Torre Florianópolis"})
        self.assertAlmostEqual(params["frequency_mhz"], 118.700, places=3)
        self.assertEqual(params["preset"], "Torre Florianópolis")
        self.assertEqual(params["mode"], "am")

    def test_h_preset_atis(self):
        params = _resolve_radio_params({"preset": "ATIS Florianópolis"})
        self.assertAlmostEqual(params["frequency_mhz"], 127.450, places=3)
        self.assertEqual(params["preset"], "ATIS Florianópolis")

    def test_preset_desconhecido_da_404(self):
        with self.assertRaises(ApiError):
            _resolve_radio_params({"preset": "Preset Que Não Existe"})

    # ------------------------------------------------------------------ #
    # I) frequência manual
    # ------------------------------------------------------------------ #
    def test_i_frequencia_manual_sem_preset_correspondente(self):
        params = _resolve_radio_params({"frequency_mhz": 125.325})
        self.assertEqual(params["preset"], "Manual")
        self.assertAlmostEqual(params["frequency_mhz"], 125.325, places=3)

    # ------------------------------------------------------------------ #
    # J) histórico recebe preset correto - o rótulo enviado pelo cliente é
    # só um "palpite"; quem decide é a frequência efetiva
    # ------------------------------------------------------------------ #
    def test_j_preset_enviado_nao_bate_com_frequencia_manual_diferente(self):
        # o dropdown ficou em "Torre Florianópolis" (resíduo de seleção
        # anterior) mas a frequência manual enviada é a do Guard - não pode
        # virar "Torre Florianópolis" no histórico (bug relatado)
        params = _resolve_radio_params({"preset": "Torre Florianópolis", "frequency_mhz": 121.500})
        self.assertEqual(params["preset"], "Emergência / Guard")

    def test_j_preset_nao_configurado_mas_frequencia_manual_enviada(self):
        params = _resolve_radio_params({"preset": "ATIS Florianópolis", "frequency_mhz": 133.333})
        self.assertEqual(params["preset"], "Manual")

    # ------------------------------------------------------------------ #
    # K) manual puro vira "Manual"
    # ------------------------------------------------------------------ #
    def test_k_manual_puro(self):
        params = _resolve_radio_params({"preset": None, "frequency_mhz": 133.333})
        self.assertEqual(params["preset"], "Manual")

    def test_manual_que_bate_exatamente_com_preset_conhecido(self):
        # regra 9: frequência manual que corresponde a um preset é associada a ele
        params = _resolve_radio_params({"frequency_mhz": 118.700})
        self.assertEqual(params["preset"], "Torre Florianópolis")

    # ------------------------------------------------------------------ #
    # validação de faixa - pega o typo relatado (118100 em vez de 118.100)
    # ------------------------------------------------------------------ #
    def test_validacao_faixa_pega_typo_118100(self):
        with self.assertRaises(ApiError) as ctx:
            _resolve_radio_params({"frequency_mhz": 118100})
        self.assertIn("118.100", ctx.exception.message)
        self.assertEqual(ctx.exception.status, 400)

    def test_validacao_faixa_rejeita_frequencia_absurda(self):
        with self.assertRaises(ApiError):
            _resolve_radio_params({"frequency_mhz": 99999})

    # ------------------------------------------------------------------ #
    # L, M) conversão MHz -> Hz no comando rtl_fm de fato lançado
    # ------------------------------------------------------------------ #
    def test_l_torre_118700_para_118700000hz(self):
        self._assert_hz(118.700, 118700000)

    def test_m_atis_127450_para_127450000hz(self):
        self._assert_hz(127.450, 127450000)

    def test_demais_presets_mhz_para_hz(self):
        casos = [
            (121.700, 121700000),  # Solo
            (122.500, 122500000),  # Operações
            (121.500, 121500000),  # Guard
            (122.800, 122800000),  # Torre MIL
        ]
        for mhz, hz in casos:
            self._assert_hz(mhz, hz)

    def _assert_hz(self, mhz: float, expected_hz: int):
        svc = RadioService(self.app.extensions["db"], self.settings)
        with patch("app.radio_service.subprocess.Popen") as mock_popen:
            factory = FakePopenFactory()
            mock_popen.side_effect = factory
            svc.start(frequency_mhz=mhz, preset="Manual")
            try:
                rtl_fm_cmd = factory.commands[0]
                self.assertIn("-f", rtl_fm_cmd)
                freq_arg = rtl_fm_cmd[rtl_fm_cmd.index("-f") + 1]
                self.assertEqual(int(freq_arg), expected_hz)
                # e o valor exposto no status/histórico continua em MHz, não em Hz
                self.assertAlmostEqual(svc.get_status()["frequency_mhz"], mhz, places=3)
            finally:
                svc.stop()


if __name__ == "__main__":
    unittest.main()
