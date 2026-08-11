#!/usr/bin/env python3
"""Benchmark de modelos en ESTA maquina. Correr antes de decidir el modelo.

    python bench.py muestra.wav
    python bench.py muestra.wav --models base,small,medium

Interpretacion del RTF (segundos de CPU por segundo de audio):

    < 0.5   tiempo real con margen
    0.5-0.8 funciona, sin holgura: una sola reunion y nada mas
    > 1.0   no sirve para vivo (si sirve para el acta final, que no tiene prisa)

Para grabar la muestra en el propio servidor:
    arecord -f S16_LE -r 16000 -c 1 -d 60 muestra.wav

Que sea habla real, en el idioma y las condiciones de sala de las reuniones.
Una muestra limpia de estudio da numeros optimistas que luego no se cumplen.
"""

from __future__ import annotations

import argparse
import struct
import sys
import time
import wave
from pathlib import Path

# Por debajo de esto, el audio no tiene voz utilizable.
SILENCE_RMS_DBFS = -55.0


def inspect_audio(path: Path) -> tuple[float, float, float]:
    """Devuelve (duracion_s, rms_dbfs, pico_dbfs).

    Medir el nivel no es un lujo: un WAV en silencio —micro desenchufado o
    captura muteada en ALSA— hace que el VAD descarte todo. La inferencia
    entonces no corre, el RTF sale ~0 y el benchmark aprueba la nada.
    """
    import math

    with wave.open(str(path), "rb") as wav:
        rate = wav.getframerate()
        channels = wav.getnchannels()
        width = wav.getsampwidth()
        frames = wav.readframes(wav.getnframes())

    if rate != 16000 or channels != 1:
        print(
            f"  aviso: {path.name} es {rate} Hz / {channels} canal(es). Whisper"
            " espera 16 kHz mono; se remuestrea y el numero sale algo peor."
        )

    duration = len(frames) / (rate * channels * width)
    if width != 2 or not frames:
        return duration, float("-inf"), float("-inf")

    samples = struct.unpack("<%dh" % (len(frames) // 2), frames)
    peak = max(abs(s) for s in samples)
    rms = math.sqrt(sum(s * s for s in samples) / len(samples))

    to_db = lambda v: 20 * math.log10(v / 32768.0) if v > 0 else float("-inf")
    return duration, to_db(rms), to_db(peak)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio", type=Path, help="WAV de prueba (idealmente 16 kHz mono)")
    parser.add_argument("--models", default="base,small", help="lista separada por comas")
    parser.add_argument("--language", default="es")
    parser.add_argument("--compute", default="int8")
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--beam", type=int, default=1, help="1 = greedy, como en vivo")
    args = parser.parse_args()

    if not args.audio.is_file():
        raise SystemExit(f"no existe: {args.audio}")

    # Se inspecciona el audio ANTES de importar faster_whisper: si la muestra no
    # sirve, no tiene sentido cargar el motor ni descargar modelos.
    duration, rms_db, peak_db = inspect_audio(args.audio)
    print(f"\nAudio: {args.audio.name} — {duration:.1f} s")
    print(f"Nivel: RMS {rms_db:.1f} dBFS, pico {peak_db:.1f} dBFS")

    if rms_db < SILENCE_RMS_DBFS:
        raise SystemExit(
            f"\nxx Este audio esta en silencio (RMS {rms_db:.1f} dBFS).\n"
            "   El VAD lo descartaria completo, la inferencia no correria y el\n"
            "   RTF saldria ~0: un resultado sin ningun valor.\n\n"
            "   Causas habituales al grabar en el servidor con arecord:\n"
            "     - no hay microfono enchufado en el conector\n"
            "     - la captura esta muteada o a cero:\n"
            "         amixer sset Capture cap && amixer sset Capture 80%\n"
            "         arecord -D hw:0,0 -f S16_LE -r 16000 -c 1 -d 10 /tmp/t.wav\n\n"
            "   Lo mas practico es grabar la muestra en una laptop y subirla:\n"
            "     ffmpeg -i muestra.m4a -ac 1 -ar 16000 muestra.wav\n"
            "     scp muestra.wav usuario@servidor:/tmp/\n\n"
            "   La muestra NO tiene que grabarse en el servidor. Lo que importa\n"
            "   es que la inferencia se mida ahi.\n"
        )

    if peak_db > -1.0:
        print("  aviso: el audio recorta (pico ~0 dBFS). Bajá la ganancia de entrada.")

    print(f"Config: compute={args.compute} threads={args.threads} beam={args.beam}\n")

    from faster_whisper import WhisperModel

    print(f"{'modelo':<18} {'carga':>8} {'inferencia':>11} {'RTF':>7}  veredicto")
    print("-" * 72)

    results = []
    for name in [m.strip() for m in args.models.split(",") if m.strip()]:
        try:
            t0 = time.monotonic()
            model = WhisperModel(
                name,
                device="cpu",
                compute_type=args.compute,
                cpu_threads=args.threads,
            )
            load_time = time.monotonic() - t0

            t1 = time.monotonic()
            segments, _info = model.transcribe(
                str(args.audio),
                language=args.language,
                beam_size=args.beam,
                vad_filter=True,
                condition_on_previous_text=False,
            )
            text = " ".join(seg.text.strip() for seg in segments)
            infer = time.monotonic() - t1
            rtf = infer / duration if duration else float("inf")

            # Sin texto no hubo inferencia real: el RTF mide el VAD, no el
            # modelo. Dar un veredicto de velocidad aca seria mentir.
            if not text.strip():
                verdict = "SIN TEXTO — medicion invalida"
            elif rtf < 0.5:
                verdict = "vivo con margen"
            elif rtf < 0.8:
                verdict = "vivo justo"
            elif rtf < 1.0:
                verdict = "al limite"
            else:
                verdict = "solo acta final"

            print(f"{name:<18} {load_time:7.1f}s {infer:10.1f}s {rtf:7.2f}  {verdict}")
            results.append((name, rtf, text))
            del model
        except Exception as exc:  # noqa: BLE001
            print(f"{name:<18} {'—':>8} {'—':>11} {'—':>7}  ERROR: {exc}")

    print()
    for name, _rtf, text in results:
        print(f"--- {name} " + "-" * (68 - len(name)))
        print(text[:600] + ("…" if len(text) > 600 else "") if text.strip() else "(vacio)")
        print()

    if results and not any(text.strip() for _n, _r, text in results):
        print(
            "xx Ningun modelo produjo texto. El nivel pasa el umbral de silencio\n"
            "   pero no hay voz reconocible: ruido de fondo, microfono muy lejos,\n"
            "   o idioma distinto al indicado con --language.\n"
            "   Los RTF de arriba NO son validos.\n"
        )
        sys.exit(1)

    print(
        "Compara la CALIDAD del texto, no solo el RTF. En espanol, `base` suele\n"
        "inventar nombres propios y siglas: para un acta institucional eso pesa\n"
        "mas que 200 ms de latencia.\n"
    )


if __name__ == "__main__":
    main()
