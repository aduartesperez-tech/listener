"""Motores de reconocimiento: uno rapido para el vivo, uno bueno para el acta.

faster-whisper corre sobre CTranslate2, NO sobre PyTorch: la instalacion es
liviana y el int8 en CPU aprovecha el AVX2 del Skylake.
"""

from __future__ import annotations

import gc
import logging
import re
import threading
from pathlib import Path
from typing import Any, Callable, Iterable

import numpy as np

from .audio import pcm16_to_float32
from .config import Settings

log = logging.getLogger("listener.asr")

# Whisper "rellena" los silencios con frases de subtitulos de YouTube. Es un
# artefacto conocido del entrenamiento. Si una frase entera es solo esto, fuera.
_HALLUCINATION_MARKERS = (
    "amara.org",
    "subtitulos realizados por",
    "subtitulado por",
    "subtitles by",
    "gracias por ver el video",
    "gracias por ver este video",
    "suscribete al canal",
    "suscribanse al canal",
    "www.mooji.org",
    "no te olvides de suscribirte",
    "mas informacion en",
)

_ACCENTS = str.maketrans("áéíóúüñÁÉÍÓÚÜÑ", "aeiouunAEIOUUN")
_PUNCT_ONLY = re.compile(r"^[\s\.\,\!\?\-\_\¡\¿\"\'\*\(\)\[\]…]*$")


def _normalize(text: str) -> str:
    return text.strip().lower().translate(_ACCENTS)


def is_garbage(
    text: str,
    *,
    no_speech_prob: float = 0.0,
    avg_logprob: float = 0.0,
    compression_ratio: float = 1.0,
) -> bool:
    """Heuristicas estandar de Whisper + filtro de alucinaciones en espanol."""
    if not text or _PUNCT_ONLY.match(text):
        return True
    norm = _normalize(text)
    if any(marker in norm for marker in _HALLUCINATION_MARKERS):
        return True
    if no_speech_prob > 0.85:
        return True
    if avg_logprob < -1.1:
        return True
    # Ratio alto = texto repetitivo, el sintoma clasico del bucle de Whisper.
    if compression_ratio > 2.5:
        return True
    # "Eh." / "Mm." sueltos no aportan a un acta.
    if len(norm) <= 2:
        return True
    return False


class _ModelSlot:
    """Carga perezosa de un WhisperModel, con lock y descarga explicita."""

    def __init__(self, name: str, compute_type: str, threads: int, cache_dir: str):
        self.name = name
        self.compute_type = compute_type
        self.threads = threads
        self.cache_dir = cache_dir or None
        self._model: Any | None = None
        self._lock = threading.Lock()

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def get(self) -> Any:
        with self._lock:
            if self._model is None:
                # Import diferido: arrancar el server no debe esperar a CTranslate2.
                from faster_whisper import WhisperModel

                log.info(
                    "cargando modelo %s (%s, %d hilos)",
                    self.name,
                    self.compute_type,
                    self.threads,
                )
                self._model = WhisperModel(
                    self.name,
                    device="cpu",
                    compute_type=self.compute_type,
                    cpu_threads=self.threads,
                    num_workers=1,
                    download_root=self.cache_dir,
                )
                log.info("modelo %s listo", self.name)
            return self._model

    def unload(self) -> None:
        with self._lock:
            if self._model is None:
                return
            log.info("descargando modelo %s para liberar RAM", self.name)
            self._model = None
            gc.collect()


class Aborted(Exception):
    """El post-proceso se cancelo porque arranco una reunion en vivo."""


class AsrEngine:
    """Fachada con los dos niveles: `live` y `final`."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._live = _ModelSlot(
            settings.live_model,
            settings.live_compute,
            settings.live_threads,
            settings.model_cache_dir,
        )
        self._final = _ModelSlot(
            settings.final_model,
            settings.final_compute,
            settings.final_threads,
            settings.model_cache_dir,
        )
        # Locks separados a proposito: el vivo nunca debe quedar esperando a
        # que termine un post-proceso de dos horas.
        self._live_lock = threading.Lock()
        self._final_lock = threading.Lock()

    # -- estado -------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        return {
            "live_model": self.settings.live_model,
            "live_loaded": self._live.loaded,
            "final_model": self.settings.final_model if self.settings.enable_final else None,
            "final_loaded": self._final.loaded,
        }

    def warmup_live(self) -> None:
        """Precarga + una inferencia en vacio, para que la primera frase real
        de la reunion no pague el coste de arranque."""
        model = self._live.get()
        silence = np.zeros(self.settings.sample_rate, dtype=np.float32)
        with self._live_lock:
            list(model.transcribe(silence, language=self.settings.language)[0])

    def release_final(self) -> None:
        self._final.unload()

    # -- transcripcion ------------------------------------------------------

    def transcribe_live(self, pcm: bytes) -> str:
        """Transcribe UNA frase ya delimitada por el VAD. Se corre en el executor."""
        audio = pcm16_to_float32(pcm)
        model = self._live.get()
        with self._live_lock:
            segments, _info = model.transcribe(
                audio,
                language=self.settings.language,
                beam_size=max(1, self.settings.live_beam),
                temperature=0.0,
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 300},
                # Critico en modo troceado: sin esto, un error se propaga a las
                # frases siguientes y Whisper entra en bucle.
                condition_on_previous_text=False,
                initial_prompt=self.settings.vocab_prompt or None,
                word_timestamps=False,
            )
            kept = [
                seg.text.strip()
                for seg in segments
                if not is_garbage(
                    seg.text.strip(),
                    no_speech_prob=getattr(seg, "no_speech_prob", 0.0),
                    avg_logprob=getattr(seg, "avg_logprob", 0.0),
                    compression_ratio=getattr(seg, "compression_ratio", 1.0),
                )
            ]
        return " ".join(kept).strip()

    def transcribe_file(
        self,
        path: Path,
        should_abort: Callable[[], bool] | None = None,
        on_progress: Callable[[float], None] | None = None,
    ) -> list[dict[str, Any]]:
        """Pasada de calidad sobre la grabacion completa.

        Aca si conviene `condition_on_previous_text=True`: da coherencia de
        puntuacion y contexto a lo largo de toda la reunion.

        `should_abort` se consulta entre segmentos: permite soltar la CPU en
        cuanto arranca una reunion en vivo, que siempre tiene prioridad.
        """
        model = self._final.get()
        with self._final_lock:
            segments, _info = model.transcribe(
                str(path),
                language=self.settings.language,
                beam_size=max(1, self.settings.final_beam),
                temperature=[0.0, 0.2, 0.4, 0.6, 0.8, 1.0],
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 500},
                condition_on_previous_text=True,
                initial_prompt=self.settings.vocab_prompt or None,
                word_timestamps=False,
            )
            out: list[dict[str, Any]] = []
            # El generador es perezoso: la inferencia real ocurre en este bucle.
            for seg in segments:
                if should_abort is not None and should_abort():
                    raise Aborted()
                if on_progress is not None:
                    on_progress(float(seg.end))
                text = seg.text.strip()
                if is_garbage(
                    text,
                    no_speech_prob=getattr(seg, "no_speech_prob", 0.0),
                    avg_logprob=getattr(seg, "avg_logprob", 0.0),
                    compression_ratio=getattr(seg, "compression_ratio", 1.0),
                ):
                    continue
                out.append({"start": seg.start, "end": seg.end, "text": text})
        return out


def format_transcript(segments: Iterable[dict[str, Any]], with_speakers: bool = True) -> str:
    """Vuelca los segmentos a texto plano legible, agrupando por hablante."""
    lines: list[str] = []
    last_speaker: str | None = None
    for seg in segments:
        stamp = _hhmmss(seg["start"] if "start" in seg else seg["start_sec"])
        speaker = seg.get("speaker") if isinstance(seg, dict) else None
        text = seg["text"]
        if with_speakers and speaker:
            if speaker != last_speaker:
                lines.append("")
                lines.append(f"[{stamp}] {speaker}:")
                last_speaker = speaker
            lines.append(f"  {text}")
        else:
            lines.append(f"[{stamp}] {text}")
    return "\n".join(lines).strip() + "\n"


def _hhmmss(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"
