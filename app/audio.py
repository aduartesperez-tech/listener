"""Utilidades de audio: escritura de WAV y conversion a float32."""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

BYTES_PER_SAMPLE = 2


def pcm16_to_float32(pcm: bytes) -> np.ndarray:
    """PCM 16-bit little-endian -> float32 en [-1, 1], que es lo que come Whisper."""
    if len(pcm) % BYTES_PER_SAMPLE:
        pcm = pcm[: len(pcm) - (len(pcm) % BYTES_PER_SAMPLE)]
    return np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0


class WavWriter:
    """Escritor incremental de WAV mono 16-bit.

    Se graba TODO el audio de la reunion mientras corre el vivo: es la fuente
    para el acta final de calidad y para la diarizacion.
    A 16 kHz mono son ~115 MB por hora.
    """

    def __init__(self, path: Path, sample_rate: int = 16000):
        self.path = path
        self.sample_rate = sample_rate
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._wav = wave.open(str(path), "wb")
        self._wav.setnchannels(1)
        self._wav.setsampwidth(BYTES_PER_SAMPLE)
        self._wav.setframerate(sample_rate)
        self._bytes_written = 0
        self._closed = False

    def write(self, pcm: bytes) -> None:
        if self._closed:
            return
        self._wav.writeframes(pcm)
        self._bytes_written += len(pcm)

    @property
    def duration_sec(self) -> float:
        return self._bytes_written / (self.sample_rate * BYTES_PER_SAMPLE)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._wav.close()
        except Exception:  # noqa: BLE001 - cerrar nunca debe tumbar la sesion
            pass


def read_wav_float32(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as wav:
        sample_rate = wav.getframerate()
        frames = wav.readframes(wav.getnframes())
    return pcm16_to_float32(frames), sample_rate
