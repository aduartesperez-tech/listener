"""Tests del segmentador de voz.

Se sustituye webrtcvad por un stub determinista: lo que se prueba es la maquina
de estados del segmentador (preroll, cierre por silencio, corte forzado,
timestamps), no el detector en si. Con el VAD real los tests serian flaky.

    python -m pytest tests/ -v
"""

from __future__ import annotations

import struct
import sys
import types

import pytest

SR = 16000
FRAME_MS = 30
FRAME_SAMPLES = SR * FRAME_MS // 1000


@pytest.fixture(autouse=True)
def stub_webrtcvad(monkeypatch):
    """VAD falso: un frame es 'voz' si su primera muestra no es cero."""

    class FakeVad:
        def __init__(self, aggressiveness: int = 0):
            self.aggressiveness = aggressiveness

        def is_speech(self, frame: bytes, sample_rate: int) -> bool:
            return struct.unpack_from("<h", frame, 0)[0] != 0

    module = types.ModuleType("webrtcvad")
    module.Vad = FakeVad
    monkeypatch.setitem(sys.modules, "webrtcvad", module)

    # El import de app.vad tiene que ver el stub, no el modulo real.
    monkeypatch.delitem(sys.modules, "app.vad", raising=False)
    yield


def make_frame(voiced: bool) -> bytes:
    return struct.pack("<%dh" % FRAME_SAMPLES, *([1000 if voiced else 0] * FRAME_SAMPLES))


def make_segmenter(**overrides):
    from app.vad import StreamingSegmenter

    kwargs = dict(
        sample_rate=SR,
        frame_ms=FRAME_MS,
        aggressiveness=2,
        start_ms=90,
        end_ms=300,
        preroll_ms=300,
        max_sec=1.0,
    )
    kwargs.update(overrides)
    return StreamingSegmenter(**kwargs)


def feed_pattern(segmenter, pattern: list[bool]) -> list:
    out = []
    for voiced in pattern:
        out.extend(segmenter.feed(make_frame(voiced)))
    return out


def test_cierra_una_frase_tras_el_silencio():
    segmenter = make_segmenter(max_sec=10.0)
    utterances = feed_pattern(segmenter, [False] * 10 + [True] * 20 + [False] * 15)
    assert len(utterances) == 1


def test_pcm_y_timestamps_cuadran():
    """Si esto falla, los tiempos del acta no corresponden con el audio."""
    segmenter = make_segmenter(max_sec=10.0)
    utterances = feed_pattern(segmenter, [False] * 10 + [True] * 20 + [False] * 15)
    for utterance in utterances:
        samples = len(utterance.pcm) // 2
        assert samples == round(utterance.duration_sec * SR)


def test_el_preroll_no_corta_el_arranque():
    """La voz arranca en 0.300 s; el preroll debe capturar desde antes."""
    segmenter = make_segmenter(max_sec=10.0)
    utterances = feed_pattern(segmenter, [False] * 10 + [True] * 20 + [False] * 15)
    utterance = utterances[0]
    assert utterance.start_sec <= 0.300
    # Y no debe perder el final: la voz termina en 0.900 s.
    assert utterance.end_sec >= 0.900


def test_corte_forzado_respeta_max_sec():
    """Nunca acercarse a la ventana de 30 s de Whisper."""
    segmenter = make_segmenter(max_sec=1.0)
    utterances = feed_pattern(segmenter, [True] * 60)  # 1.8 s de voz continua
    assert utterances
    # Tolerancia de un frame: el corte se evalua despues de anadirlo.
    limit = 1.0 + FRAME_MS / 1000
    assert all(u.duration_sec <= limit for u in utterances)


def test_chunks_no_alineados_al_frame():
    """El WebSocket entrega bloques de 100 ms; el frame del VAD es de 30 ms."""
    segmenter = make_segmenter(max_sec=10.0)
    data = b"".join(make_frame(v) for v in ([False] * 5 + [True] * 15 + [False] * 15))
    chunk = 3200  # 100 ms, no es multiplo de los 960 bytes del frame
    utterances = []
    for offset in range(0, len(data), chunk):
        utterances.extend(segmenter.feed(data[offset : offset + chunk]))
    assert len(utterances) == 1
    assert segmenter.seconds_seen == pytest.approx(len(data) / 2 / SR, abs=0.031)


def test_flush_recupera_la_frase_abierta():
    """Al detener la reunion no se puede perder lo ultimo que se dijo."""
    segmenter = make_segmenter(max_sec=10.0)
    assert feed_pattern(segmenter, [True] * 20) == []
    tail = segmenter.flush()
    assert tail is not None
    assert tail.duration_sec > 0.5
    assert segmenter.flush() is None  # idempotente


def test_el_silencio_puro_no_genera_frases():
    segmenter = make_segmenter()
    assert feed_pattern(segmenter, [False] * 100) == []
    assert segmenter.flush() is None


def test_rechaza_parametros_invalidos():
    from app.vad import StreamingSegmenter

    with pytest.raises(ValueError):
        StreamingSegmenter(frame_ms=25)  # webrtcvad solo acepta 10/20/30
    with pytest.raises(ValueError):
        StreamingSegmenter(sample_rate=22050)
