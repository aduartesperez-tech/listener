"""Configuracion cargada desde .env / entorno."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Lector minimo de .env: KEY=VALUE, ignora comentarios y lineas vacias.

    No sobreescribe variables que ya vengan del entorno (systemd manda).
    """
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv(BASE_DIR / ".env")


def _str(name: str, default: str) -> str:
    return os.environ.get(name, default).strip()


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "si", "sí", "on"}


@dataclass(frozen=True)
class Settings:
    host: str
    port: int

    language: str
    vocab_prompt: str

    live_model: str
    live_compute: str
    live_threads: int
    live_beam: int

    enable_final: bool
    final_model: str
    final_compute: str
    final_threads: int
    final_beam: int

    enable_diarization: bool
    hf_token: str

    vad_aggressiveness: int
    vad_start_ms: int
    vad_end_ms: int
    vad_preroll_ms: int
    max_utterance_sec: float
    min_utterance_sec: float

    max_meeting_hours: float

    data_dir: Path
    model_cache_dir: str

    # Audio: fijo. Es lo que Whisper espera y lo que manda el navegador.
    sample_rate: int = 16000
    frame_ms: int = 30

    @property
    def recordings_dir(self) -> Path:
        return self.data_dir / "recordings"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "listener.db"

    @property
    def static_dir(self) -> Path:
        return BASE_DIR / "static"


def load_settings() -> Settings:
    data_dir = Path(_str("DATA_DIR", "data"))
    if not data_dir.is_absolute():
        data_dir = BASE_DIR / data_dir

    settings = Settings(
        host=_str("HOST", "127.0.0.1"),
        port=_int("PORT", 8000),
        language=_str("LANGUAGE", "es"),
        vocab_prompt=_str("VOCAB_PROMPT", ""),
        live_model=_str("LIVE_MODEL", "small"),
        live_compute=_str("LIVE_COMPUTE", "int8"),
        live_threads=_int("LIVE_THREADS", 4),
        live_beam=_int("LIVE_BEAM", 1),
        enable_final=_bool("ENABLE_FINAL", True),
        final_model=_str("FINAL_MODEL", "large-v3-turbo"),
        final_compute=_str("FINAL_COMPUTE", "int8"),
        final_threads=_int("FINAL_THREADS", 4),
        final_beam=_int("FINAL_BEAM", 5),
        enable_diarization=_bool("ENABLE_DIARIZATION", False),
        hf_token=_str("HF_TOKEN", ""),
        vad_aggressiveness=_int("VAD_AGGRESSIVENESS", 2),
        vad_start_ms=_int("VAD_START_MS", 90),
        vad_end_ms=_int("VAD_END_MS", 700),
        vad_preroll_ms=_int("VAD_PREROLL_MS", 300),
        max_utterance_sec=_float("MAX_UTTERANCE_SEC", 20.0),
        min_utterance_sec=_float("MIN_UTTERANCE_SEC", 0.35),
        max_meeting_hours=_float("MAX_MEETING_HOURS", 4.0),
        data_dir=data_dir,
        model_cache_dir=_str("MODEL_CACHE_DIR", ""),
    )

    settings.recordings_dir.mkdir(parents=True, exist_ok=True)
    return settings


settings = load_settings()
