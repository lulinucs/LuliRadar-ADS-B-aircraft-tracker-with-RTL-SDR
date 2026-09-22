"""
RadioService: gerencia rtl_fm (demodulação AM) e a reprodução local via
aplay, faz medição de nível em tempo real, detecção de transmissão
(squelch simples baseado em dBFS) e gravação automática em WAV com
pré-buffer circular.

Não usa pyrtlsdr: rtl_fm (do pacote rtl-sdr) faz a demodulação; nós apenas
consumimos o PCM bruto que ele escreve em stdout.
"""
from __future__ import annotations

import logging
import math
import subprocess
import threading
import time
import wave
from array import array
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from .config import Settings
from .database import Database
from .procutil import terminate_process

logger = logging.getLogger("luliradar.radio")

CHUNK_SECONDS = 0.1


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _compute_rms(samples: array) -> float:
    if len(samples) == 0:
        return 0.0
    total = sum(s * s for s in samples)
    return math.sqrt(total / len(samples))


def _rms_to_dbfs(rms: float) -> float:
    if rms <= 0:
        return -120.0
    return max(20 * math.log10(rms / 32768.0), -120.0)


def _scale_samples(samples: array, factor: float) -> array:
    if factor == 1.0:
        return samples
    out = array("h", bytes(len(samples) * 2))
    for i, s in enumerate(samples):
        v = int(s * factor)
        if v > 32767:
            v = 32767
        elif v < -32768:
            v = -32768
        out[i] = v
    return out


class RadioService:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings

        self._lock = threading.RLock()
        self._rtl_proc: subprocess.Popen | None = None
        self._aplay_proc: subprocess.Popen | None = None
        self._audio_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

        self._current_params: dict = {}
        self._status: dict = {
            "running": False,
            "error": None,
            "preset": None,
            "frequency_mhz": None,
            "mode": "am",
            "gain": None,
            "squelch_dbfs": None,
            "volume": 1.0,
            "level_dbfs": -120.0,
            "rms": 0.0,
            "squelch_state": "SILÊNCIO",
            "recording": False,
            "started_at": None,
        }

        self._prebuffer: deque = deque()
        self._recording = False
        self._wav_file: wave.Wave_write | None = None
        self._wav_path: Path | None = None
        self._wav_start_wall: float | None = None
        self._last_active_wall: float | None = None
        self._peak_dbfs = -120.0
        self._sum_dbfs = 0.0
        self._n_dbfs = 0

    # ------------------------------------------------------------------ #
    def is_running(self) -> bool:
        return self._rtl_proc is not None and self._rtl_proc.poll() is None

    def start(
        self,
        frequency_mhz: float,
        mode: str = "am",
        gain: str | float | None = None,
        squelch_dbfs: float | None = None,
        volume: float = 1.0,
        preset: str | None = None,
        silence_timeout: float | None = None,
        prebuffer_sec: float | None = None,
    ) -> dict:
        with self._lock:
            if self.is_running():
                self._stop_locked()

            gain = gain if gain not in (None, "") else self.settings.radio_default_gain
            squelch_dbfs = (
                squelch_dbfs if squelch_dbfs is not None else self.settings.radio_default_squelch_dbfs
            )
            silence_timeout = (
                silence_timeout if silence_timeout is not None else self.settings.radio_default_silence_timeout
            )
            prebuffer_sec = (
                prebuffer_sec if prebuffer_sec is not None else self.settings.radio_default_prebuffer_sec
            )

            self._current_params = dict(
                frequency_mhz=float(frequency_mhz), mode=mode, gain=gain, squelch_dbfs=float(squelch_dbfs),
                volume=float(volume), preset=preset, silence_timeout=float(silence_timeout),
                prebuffer_sec=float(prebuffer_sec),
            )

            freq_hz = int(round(float(frequency_mhz) * 1_000_000))
            sr = self.settings.radio_sample_rate
            cmd = [self.settings.rtl_fm_bin, "-f", str(freq_hz), "-M", mode, "-s", str(sr), "-l", "0"]
            gain_str = str(gain).strip().lower()
            if gain_str not in ("", "auto"):
                cmd += ["-g", gain_str]
            cmd += ["-E", "dc", "-"]

            logger.info("Iniciando rádio: %s", " ".join(cmd))
            try:
                self._rtl_proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0, start_new_session=True,
                )
            except FileNotFoundError as exc:
                msg = f"Binário '{self.settings.rtl_fm_bin}' não encontrado: {exc}"
                self._status["error"] = msg
                logger.error(msg)
                raise RuntimeError(msg) from exc

            try:
                self._aplay_proc = subprocess.Popen(
                    [self.settings.aplay_bin, "-q", "-r", str(sr), "-f", "S16_LE", "-t", "raw", "-c", "1", "-"],
                    stdin=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True,
                )
            except FileNotFoundError:
                logger.warning("aplay não encontrado - áudio não será reproduzido localmente (gravação/nível continuam)")
                self._aplay_proc = None

            self._stop_event.clear()
            self._prebuffer.clear()
            self._recording = False
            self._peak_dbfs = -120.0

            self._status.update(
                running=True, error=None, preset=preset, frequency_mhz=float(frequency_mhz), mode=mode,
                gain=gain, squelch_dbfs=float(squelch_dbfs), volume=float(volume), level_dbfs=-120.0,
                rms=0.0, squelch_state="SILÊNCIO", recording=False, started_at=time.time(),
            )

            self._audio_thread = threading.Thread(target=self._audio_loop, daemon=True, name="radio-audio")
            self._audio_thread.start()
            self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True, name="radio-stderr")
            self._stderr_thread.start()
            return dict(self._status)

    def tune(self, **kwargs) -> dict:
        """Reajusta parâmetros. Frequência/ganho exigem reiniciar o rtl_fm; os
        demais (squelch/volume/timeout/prebuffer) só reiniciam se vierem
        junto - caso contrário são aplicados a quente via set_live_params."""
        with self._lock:
            live_only = {"squelch_dbfs", "volume", "silence_timeout", "prebuffer_sec"}
            if set(kwargs.keys()) and set(kwargs.keys()).issubset(live_only) and self.is_running():
                self._current_params.update({k: v for k, v in kwargs.items() if v is not None})
                self._status.update({k: v for k, v in kwargs.items() if v is not None and k in self._status})
                return dict(self._status)
            params = dict(self._current_params)
            params.update({k: v for k, v in kwargs.items() if v is not None})
            return self.start(**params)

    def stop(self) -> dict:
        with self._lock:
            self._stop_locked()
            return dict(self._status)

    def _stop_locked(self) -> None:
        if not self.is_running() and self._audio_thread is None:
            return
        logger.info("Parando rádio")
        self._stop_event.set()
        terminate_process(self._rtl_proc, "rtl_fm")
        self._rtl_proc = None
        terminate_process(self._aplay_proc, "aplay")
        self._aplay_proc = None
        if self._audio_thread is not None:
            self._audio_thread.join(timeout=4)
            self._audio_thread = None
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=2)
            self._stderr_thread = None
        self._close_wav_if_open()
        self._status.update(running=False, squelch_state="SILÊNCIO", recording=False)

    def _drain_stderr(self) -> None:
        if self._rtl_proc is None or self._rtl_proc.stderr is None:
            return
        try:
            for line in self._rtl_proc.stderr:
                line = line.strip()
                if line:
                    logger.debug("rtl_fm: %s", line)
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # loop de áudio
    # ------------------------------------------------------------------ #
    def _audio_loop(self) -> None:
        sr = self.settings.radio_sample_rate
        chunk_bytes = int(sr * CHUNK_SECONDS) * 2
        proc = self._rtl_proc
        stream = proc.stdout
        try:
            while not self._stop_event.is_set():
                if proc.poll() is not None:
                    err = f"rtl_fm encerrou inesperadamente (código={proc.returncode})"
                    logger.error(err)
                    with self._lock:
                        self._status["error"] = err
                        self._status["running"] = False
                    break
                try:
                    raw = stream.read(chunk_bytes)
                except Exception:
                    break
                if not raw:
                    break
                usable = raw[: (len(raw) // 2) * 2]
                if not usable:
                    continue
                samples = array("h")
                samples.frombytes(usable)

                rms = _compute_rms(samples)
                dbfs = _rms_to_dbfs(rms)

                with self._lock:
                    volume = self._current_params.get("volume", 1.0)
                    squelch = self._current_params.get("squelch_dbfs", -35.0)

                out_samples = _scale_samples(samples, volume)
                out_bytes = out_samples.tobytes()

                if self._aplay_proc is not None and self._aplay_proc.stdin is not None:
                    try:
                        self._aplay_proc.stdin.write(out_bytes)
                    except Exception:
                        pass

                active = dbfs > squelch
                self._handle_vad(active, out_bytes, dbfs)

                with self._lock:
                    self._status["level_dbfs"] = round(dbfs, 1)
                    self._status["rms"] = round(rms, 1)
                    self._status["squelch_state"] = "RECEBENDO" if active else "SILÊNCIO"
                    self._status["recording"] = self._recording
        finally:
            self._close_wav_if_open()

    def _handle_vad(self, active: bool, out_bytes: bytes, dbfs: float) -> None:
        prebuffer_sec = self._current_params.get("prebuffer_sec", 1.5)
        silence_timeout = self._current_params.get("silence_timeout", 2.0)
        maxlen = max(int(prebuffer_sec / CHUNK_SECONDS) + 1, 1)

        if not self._recording:
            self._prebuffer.append(out_bytes)
            while len(self._prebuffer) > maxlen:
                self._prebuffer.popleft()
            if active:
                self._start_recording()
                for chunk in self._prebuffer:
                    self._wav_file.writeframes(chunk)
                self._prebuffer.clear()
                self._last_active_wall = time.time()
                self._track_levels(dbfs)
        else:
            self._wav_file.writeframes(out_bytes)
            self._track_levels(dbfs)
            if active:
                self._last_active_wall = time.time()
            elif self._last_active_wall is not None and time.time() - self._last_active_wall >= silence_timeout:
                self._close_wav_if_open()

    def _start_recording(self) -> None:
        now = datetime.now()
        date_dir = self.settings.recordings_dir / now.strftime("%Y-%m-%d")
        date_dir.mkdir(parents=True, exist_ok=True)
        preset = (self._current_params.get("preset") or "MANUAL")
        preset_safe = "".join(c if c.isalnum() else "_" for c in str(preset).upper()).strip("_") or "MANUAL"
        freq = self._current_params.get("frequency_mhz", 0.0)
        fname = f"{now.strftime('%H-%M-%S')}_{preset_safe}_{freq:.3f}.wav"
        path = date_dir / fname

        wf = wave.open(str(path), "wb")
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(self.settings.radio_sample_rate)

        self._wav_file = wf
        self._wav_path = path
        self._wav_start_wall = time.time()
        self._recording = True
        self._peak_dbfs = -120.0
        self._sum_dbfs = 0.0
        self._n_dbfs = 0
        logger.info("Gravação iniciada: %s", path)

    def _track_levels(self, dbfs: float) -> None:
        self._peak_dbfs = max(self._peak_dbfs, dbfs)
        self._sum_dbfs += dbfs
        self._n_dbfs += 1

    def _close_wav_if_open(self) -> None:
        if not self._recording or self._wav_file is None:
            return
        try:
            self._wav_file.close()
        except Exception:
            logger.exception("Erro ao fechar arquivo WAV")

        duration = time.time() - (self._wav_start_wall or time.time())
        avg_dbfs = (self._sum_dbfs / self._n_dbfs) if self._n_dbfs else self._peak_dbfs

        if duration < self.settings.radio_min_recording_sec:
            try:
                if self._wav_path is not None:
                    self._wav_path.unlink(missing_ok=True)
            except Exception:
                pass
            logger.info("Gravação descartada (curta demais: %.2fs)", duration)
        else:
            self._save_recording_row(duration, avg_dbfs)
            logger.info(
                "Gravação finalizada: %s (%.1fs, pico=%.1f dBFS, média=%.1f dBFS)",
                self._wav_path, duration, self._peak_dbfs, avg_dbfs,
            )

        self._recording = False
        self._wav_file = None
        self._wav_path = None
        self._wav_start_wall = None
        self._last_active_wall = None
        with self._lock:
            self._status["recording"] = False

    def _save_recording_row(self, duration: float, avg_level: float) -> None:
        p = self._current_params
        start_iso = datetime.fromtimestamp(self._wav_start_wall, tz=timezone.utc).isoformat()
        end_iso = _now_iso()
        rel_path = self._wav_path.relative_to(self.settings.recordings_dir)
        self.db.execute(
            "INSERT INTO radio_recordings (start_time, end_time, duration, preset, frequency_mhz, "
            "filename, peak_level, average_level) VALUES (?,?,?,?,?,?,?,?)",
            (
                start_iso, end_iso, duration, p.get("preset"), p.get("frequency_mhz"), str(rel_path),
                self._peak_dbfs, avg_level,
            ),
        )

    # ------------------------------------------------------------------ #
    def get_status(self) -> dict:
        with self._lock:
            status = dict(self._status)
            status["current_params"] = dict(self._current_params)
        status["running"] = self.is_running()
        return status

    def list_recordings(self, limit: int = 50) -> list[dict]:
        return self.db.query(
            "SELECT * FROM radio_recordings ORDER BY start_time DESC LIMIT ?", (limit,)
        )
