"""Diarizacion opcional: etiquetar quien habla.

Desactivada por defecto a proposito. pyannote.audio arrastra PyTorch (~2.5 GB
en disco) y en un i5-6500T la pasada de diarizacion es lenta. Por eso solo se
usa en el post-proceso, nunca en vivo.

Si esta desactivada o falla, el acta sale igual pero sin etiquetas de hablante.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

log = logging.getLogger("listener.diarize")

_pipeline: Any | None = None
_unavailable_reason: str | None = None


def available(hf_token: str) -> tuple[bool, str]:
    if not hf_token:
        return False, "falta HF_TOKEN"
    try:
        import pyannote.audio  # noqa: F401
    except ImportError:
        return False, "pyannote.audio no esta instalado (pip install pyannote.audio)"
    return True, ""


def _load(hf_token: str) -> Any | None:
    global _pipeline, _unavailable_reason
    if _pipeline is not None:
        return _pipeline
    if _unavailable_reason:
        return None
    ok, reason = available(hf_token)
    if not ok:
        _unavailable_reason = reason
        log.warning("diarizacion no disponible: %s", reason)
        return None
    try:
        from pyannote.audio import Pipeline

        log.info("cargando pipeline de diarizacion (primera vez descarga pesos)")
        _pipeline = Pipeline.from_pretrained(
            "pyannote/speaker-diarization-3.1", use_auth_token=hf_token
        )
        return _pipeline
    except Exception as exc:  # noqa: BLE001
        _unavailable_reason = str(exc)
        log.warning("no se pudo cargar la diarizacion: %s", exc)
        return None


def speaker_turns(wav_path: Path, hf_token: str) -> list[dict[str, Any]]:
    """Devuelve [{'start','end','speaker'}] o [] si no se pudo diarizar."""
    pipeline = _load(hf_token)
    if pipeline is None:
        return []
    try:
        annotation = pipeline(str(wav_path))
    except Exception as exc:  # noqa: BLE001
        log.warning("la diarizacion fallo en %s: %s", wav_path.name, exc)
        return []

    turns: list[dict[str, Any]] = []
    for segment, _track, label in annotation.itertracks(yield_label=True):
        turns.append(
            {"start": float(segment.start), "end": float(segment.end), "speaker": str(label)}
        )
    turns.sort(key=lambda t: t["start"])
    return turns


def assign_speakers(
    segments: list[dict[str, Any]], turns: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Asigna a cada segmento de texto el hablante con mayor solape temporal."""
    if not turns:
        return segments

    # SPEAKER_00 -> "Hablante 1", mas legible en el acta.
    labels = sorted({t["speaker"] for t in turns})
    pretty = {label: f"Hablante {i + 1}" for i, label in enumerate(labels)}

    for seg in segments:
        best_label, best_overlap = None, 0.0
        for turn in turns:
            if turn["start"] >= seg["end"]:
                break
            overlap = min(seg["end"], turn["end"]) - max(seg["start"], turn["start"])
            if overlap > best_overlap:
                best_overlap, best_label = overlap, turn["speaker"]
        seg["speaker"] = pretty.get(best_label) if best_label else None
    return segments
