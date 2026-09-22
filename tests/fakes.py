"""Duplos de teste para subprocessos, usados para exercitar o SDRManager e os
serviços de ADS-B/Rádio SEM tocar no RTL-SDR real nem em binários externos.
"""
from __future__ import annotations

import io
import subprocess


class FakeStdin:
    def __init__(self):
        self.written = bytearray()
        self.closed = False

    def write(self, data: bytes):
        if self.closed:
            raise BrokenPipeError("stdin fechado")
        self.written.extend(data)

    def close(self):
        self.closed = True


class FakeProcess:
    """Substitui subprocess.Popen para dump1090-mutability / rtl_fm / aplay.

    - dies_immediately: simula falha rápida de inicialização (ex.: dongle
      ocupado / "device busy") - poll() já retorna não-None no primeiro check.
    - unresponsive_to_sigterm: terminate() não mata o processo (simula um
      processo travado), forçando o caller a cair no fallback SIGKILL.
    """

    def __init__(
        self,
        pid: int = 4242,
        dies_immediately: bool = False,
        returncode_on_die: int = 1,
        unresponsive_to_sigterm: bool = False,
        stdout_data: bytes = b"",
    ):
        self.pid = pid
        self._alive = not dies_immediately
        self.returncode = None if self._alive else returncode_on_die
        self._unresponsive = unresponsive_to_sigterm
        self.terminate_called = False
        self.kill_called = False
        self.stdout = io.BytesIO(stdout_data)
        self.stderr = io.StringIO("")
        self.stdin = FakeStdin()

    def poll(self):
        return None if self._alive else self.returncode

    def terminate(self):
        self.terminate_called = True
        if not self._unresponsive:
            self._alive = False
            if self.returncode is None:
                self.returncode = 0

    def kill(self):
        self.kill_called = True
        self._alive = False
        self.returncode = -9

    def wait(self, timeout=None):
        if self._alive:
            raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout)
        return self.returncode

    @property
    def is_dead(self) -> bool:
        return not self._alive


class FakePopenFactory:
    """Callable que substitui subprocess.Popen; grava todo processo criado
    (e o comando usado) para inspeção posterior nos testes."""

    def __init__(self, **fake_kwargs):
        self.fake_kwargs = fake_kwargs
        self.created: list[FakeProcess] = []
        self.commands: list[list[str]] = []

    def __call__(self, cmd, *args, **kwargs):
        self.commands.append(list(cmd))
        proc = FakeProcess(**self.fake_kwargs)
        self.created.append(proc)
        return proc
