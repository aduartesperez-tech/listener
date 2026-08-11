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
import time
import wave
from pathlib import Path


def audio_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as wav:
        if wav.getframerate() != 16000 or wav.getnchannels() != 1:
            print(
                f"  aviso: {path.name} es {wav.getframerate()} Hz / "
                f"{wav.getnchannels()} canal(es). Whisper espera 16 kHz mono; "
                "se remuestrea internamente y el numero sale algo peor."
            )
        return wav.getnframes() / wav.getframerate()


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

    from faster_whisper import WhisperModel

    duration = audio_duration(args.audio)
    print(f"\nAudio: {args.audio.name} — {duration:.1f} s")
    print(f"Config: compute={args.compute} threads={args.threads} beam={args.beam}\n")
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

            if rtf < 0.5:
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
        print(text[:600] + ("…" if len(text) > 600 else ""))
        print()

    print(
        "Compara la CALIDAD del texto, no solo el RTF. En espanol, `base` suele\n"
        "inventar nombres propios y siglas: para un acta institucional eso pesa\n"
        "mas que 200 ms de latencia.\n"
    )


if __name__ == "__main__":
    main()
