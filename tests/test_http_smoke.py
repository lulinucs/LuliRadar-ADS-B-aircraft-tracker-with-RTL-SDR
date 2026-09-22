"""
Smoke test das rotas HTTP fim-a-fim (Flask test_client + subprocess.Popen
mockado) - garante que a fiação routes.py -> SDRManager -> serviços está
correta, sem tocar no RTL-SDR real.

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
from tests.fakes import FakePopenFactory


class HttpSmokeTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="luliradar-test-"))
        settings = Settings()
        settings.data_dir = self.tmp_dir / "data"
        settings.recordings_dir = self.tmp_dir / "recordings"
        settings.ensure_dirs()
        settings.adsb_start_confirm_sec = 0.05
        settings.radio_start_confirm_sec = 0.05
        self.settings = settings

        self.app = create_app(settings=settings)
        self.client = self.app.test_client()

        self._routes: dict[str, FakePopenFactory] = {}
        self.popen_patch = patch("subprocess.Popen", side_effect=self._dispatch)
        self.popen_patch.start()

    def _dispatch(self, cmd, *args, **kwargs):
        binary = cmd[0] if cmd else ""
        factory = self._routes.get(binary)
        if factory is None:
            factory = FakePopenFactory()
            self._routes[binary] = factory
        return factory(cmd, *args, **kwargs)

    def tearDown(self):
        self.app.extensions["sdr_manager"].shutdown()
        self.popen_patch.stop()
        self.app.extensions["db"].close()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_status_inicial_idle(self):
        r = self.client.get("/api/status")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(data["sdr_state"], "idle")
        self.assertIsNone(data["error"])

    def test_mode_adsb_ativa_e_stop_libera(self):
        r = self.client.post("/api/mode/adsb")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["sdr_state"], "adsb")

        r = self.client.get("/api/status")
        self.assertEqual(r.get_json()["adsb"]["running"], True)

        r = self.client.post("/api/stop")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["sdr_state"], "idle")

    def test_mode_radio_com_preset_torre(self):
        r = self.client.post("/api/mode/radio", json={"preset": "Torre Florianópolis"})
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(data["sdr_state"], "radio")
        self.assertAlmostEqual(data["radio"]["frequency_mhz"], 118.700, places=3)
        self.assertEqual(data["radio"]["preset"], "Torre Florianópolis")

    def test_troca_adsb_para_radio_via_http(self):
        self.client.post("/api/mode/adsb")
        r = self.client.post("/api/mode/radio", json={"preset": "ATIS Florianópolis"})
        data = r.get_json()
        self.assertEqual(data["sdr_state"], "radio")
        self.assertEqual(data["adsb"]["running"], False)
        self.assertAlmostEqual(data["radio"]["frequency_mhz"], 127.450, places=3)

    def test_preset_nao_configurado_da_erro_amigavel(self):
        # simula um preset sem frequência (o cenário original do bug relatado)
        presets_path = self.settings.frequencies_file
        original = presets_path.read_text(encoding="utf-8")
        presets_path.write_text(
            '[{"name": "Torre Teste", "frequency_mhz": null, "mode": "am"}]', encoding="utf-8"
        )
        try:
            r = self.client.post("/api/mode/radio", json={"preset": "Torre Teste"})
            self.assertEqual(r.status_code, 400)
            self.assertIn("frequência", r.get_json()["error"])
        finally:
            presets_path.write_text(original, encoding="utf-8")

    def test_frequencia_invalida_digitada_errada_da_400_com_dica(self):
        r = self.client.post("/api/mode/radio", json={"frequency_mhz": 118100})
        self.assertEqual(r.status_code, 400)
        self.assertIn("118.100", r.get_json()["error"])

    def test_historico_de_preset_nao_confia_cegamente_no_rotulo_do_cliente(self):
        # cliente manda preset="Torre Florianópolis" só porque o <select> ficou
        # nessa posição, mas a frequência efetiva enviada é outra
        r = self.client.post(
            "/api/mode/radio", json={"preset": "Torre Florianópolis", "frequency_mhz": 121.500}
        )
        data = r.get_json()
        self.assertEqual(data["radio"]["preset"], "Emergência / Guard")


if __name__ == "__main__":
    unittest.main()
