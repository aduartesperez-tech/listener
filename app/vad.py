"""Segmentador de voz en streaming.

Whisper no es un modelo de streaming: trabaja en ventanas de 30 s. El patron
"re-transcribir un buffer deslizante cada 500 ms" quema CPU repitiendo trabajo
y hace que el texto en pantalla parpadee.

Aca hacemos lo correcto: WebRTC VAD marca donde hay voz, y cerramos una
*frase* cuando el hablante hace una pausa. Cada frase se transcribe una sola
vez. La latencia percibida es basicamente VAD_END_MS + el tiempo de inferencia.
"""

from __future__ import annotations

import collections
from dataclasses import dataclass

import webrtcvad

BYTES_PER_SAMPLE = 2  # PCM 16-bit mono


@dataclass
class Utterance:
    """Una frase cerrada, lista para transcribir."""

    pcm: bytes
    start_sec: float
    end_sec: float

    @property
    def duration_sec(self) -> float:
        return self.end_sec - self.start_sec


class StreamingSegmenter:
    def __init__(
        self,
        sample_rate: int = 16000,
        frame_ms: int = 30,
        aggressiveness: int = 2,
        start_ms: int = 90,
        end_ms: int = 700,
        preroll_ms: int = 300,
        max_sec: float = 20.0,
    ):
        if frame_ms not in (10, 20, 30):
            raise ValueError("webrtcvad solo acepta frames de 10, 20 o 30 ms")
        if sample_rate not in (8000, 16000, 32000, 48000):
            raise ValueError("webrtcvad solo acepta 8/16/32/48 kHz")

        self.sample_rate = sample_rate
        self.frame_samples = sample_rate * frame_ms // 1000
        self.frame_bytes = self.frame_samples * BYTES_PER_SAMPLE

        self._vad = webrtcvad.Vad(max(0, min(3, aggressiveness)))

        # Cuantos frames consecutivos hacen falta para abrir / cerrar.
        self.start_frames = max(1, start_ms // frame_ms)
        self.end_frames = max(1, end_ms // frame_ms)
        self.max_samples = int(max_sec * sample_rate)

        self._preroll: collections.deque[bytes] = collections.deque(
            maxlen=max(1, preroll_ms // frame_ms)
        )
        self._leftover = bytearray()
        self._current = bytearray()
        self._in_speech = False
        self._voiced_run = 0
        self._silence_run = 0
        self._start_sample = 0
        self._samples_seen = 0

    @property
    def seconds_seen(self) -> float:
        return self._samples_seen / self.sample_rate

    def feed(self, pcm: bytes) -> list[Utterance]:
        """Consume PCM crudo y devuelve las frases que se hayan cerrado."""
        out: list[Utterance] = []
        self._leftover += pcm
        while len(self._leftover) >= self.frame_bytes:
            frame = bytes(self._leftover[: self.frame_bytes])
            del self._leftover[: self.frame_bytes]
            utterance = self._push_frame(frame)
            if utterance is not None:
                out.append(utterance)
        return out

    def flush(self) -> Utterance | None:
        """Cierra la frase abierta (al terminar la reunion)."""
        if not self._in_speech or not self._current:
            return None
        return self._close(self._samples_seen)

    # -- interno ------------------------------------------------------------

    def _push_frame(self, frame: bytes) -> Utterance | None:
        frame_start = self._samples_seen
        self._samples_seen += self.frame_samples
        is_speech = self._vad.is_speech(frame, self.sample_rate)

        if not self._in_speech:
            # Guardamos siempre los ultimos frames: cuando se dispare el
            # arranque, los reinyectamos para no cortar la primera silaba.
            self._preroll.append(frame)
            if not is_speech:
                self._voiced_run = 0
                return None
            self._voiced_run += 1
            if self._voiced_run < self.start_frames:
                return None

            preroll = b"".join(self._preroll)
            self._current = bytearray(preroll)
            # El preroll incluye el frame actual, de ahi el +frame_samples.
            self._start_sample = (
                frame_start
                + self.frame_samples
                - len(preroll) // BYTES_PER_SAMPLE
            )
            self._preroll.clear()
            self._in_speech = True
            self._silence_run = 0
            return None

        self._current += frame
        frame_end = frame_start + self.frame_samples

        if is_speech:
            self._silence_run = 0
        else:
            self._silence_run += 1
            if self._silence_run >= self.end_frames:
                return self._close(frame_end)

        # Corte forzado: nunca acercarse a la ventana de 30 s de Whisper.
        if len(self._current) // BYTES_PER_SAMPLE >= self.max_samples:
            return self._close(frame_end)
        return None

    def _close(self, end_sample: int) -> Utterance:
        utterance = Utterance(
            pcm=bytes(self._current),
            start_sec=self._start_sample / self.sample_rate,
            end_sec=end_sample / self.sample_rate,
        )
        self._current = bytearray()
        self._in_speech = False
        self._voiced_run = 0
        self._silence_run = 0
        self._preroll.clear()
        return utterance
