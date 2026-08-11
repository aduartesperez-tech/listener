"""Tests del filtro de alucinaciones.

Whisper rellena los silencios con frases de subtitulos de YouTube. En un acta
institucional eso es inaceptable, y no se detecta a simple vista en una reunion
de una hora.
"""

from __future__ import annotations

import pytest

from app.asr import _hhmmss, format_transcript, is_garbage


@pytest.mark.parametrize(
    "text",
    [
        "Subtítulos realizados por la comunidad de Amara.org",
        "subtitulos realizados por la comunidad de amara.org",
        "¡Gracias por ver el video!",
        "No te olvides de suscribirte al canal",
        "www.mooji.org",
        "",
        "   ",
        "...",
        "¿?",
        "Eh",
    ],
)
def test_descarta_basura(text):
    assert is_garbage(text)


@pytest.mark.parametrize(
    "text",
    [
        "Buenos días, comenzamos la sesión.",
        "El presupuesto del segundo semestre queda aprobado.",
        "Gracias.",
        "Adrián, ¿podés compartir la pantalla?",
    ],
)
def test_conserva_habla_real(text):
    assert not is_garbage(text)


def test_umbrales_de_confianza():
    real = "El acuerdo se aprueba por unanimidad."
    assert not is_garbage(real)
    # Whisper dice que probablemente no habia voz.
    assert is_garbage(real, no_speech_prob=0.95)
    # Logprob muy bajo: el modelo no tiene idea de lo que oyo.
    assert is_garbage(real, avg_logprob=-1.5)
    # Ratio de compresion alto: el sintoma clasico del bucle de repeticion.
    assert is_garbage(real, compression_ratio=3.0)


def test_hhmmss():
    assert _hhmmss(0) == "00:00:00"
    assert _hhmmss(59.9) == "00:00:59"
    assert _hhmmss(61) == "00:01:01"
    assert _hhmmss(3661) == "01:01:01"


def test_format_transcript_sin_hablantes():
    out = format_transcript(
        [
            {"start": 0.0, "end": 2.0, "text": "Primera frase."},
            {"start": 65.0, "end": 67.0, "text": "Segunda frase."},
        ]
    )
    assert "[00:00:00] Primera frase." in out
    assert "[00:01:05] Segunda frase." in out


def test_format_transcript_agrupa_por_hablante():
    out = format_transcript(
        [
            {"start": 0.0, "end": 2.0, "speaker": "Hablante 1", "text": "Hola."},
            {"start": 2.0, "end": 4.0, "speaker": "Hablante 1", "text": "Seguimos."},
            {"start": 4.0, "end": 6.0, "speaker": "Hablante 2", "text": "De acuerdo."},
        ]
    )
    # El nombre del hablante aparece una vez por turno, no por frase.
    assert out.count("Hablante 1:") == 1
    assert out.count("Hablante 2:") == 1
    assert "Seguimos." in out


def test_format_transcript_acepta_claves_de_sqlite():
    """Los segmentos de la base traen start_sec/end_sec, no start/end."""
    out = format_transcript([{"start_sec": 12.0, "end": 14.0, "text": "Desde SQLite."}])
    assert "[00:00:12] Desde SQLite." in out
