"""Sesion de transcripcion en vivo y el candado de sesion unica.

El servidor tiene 4 nucleos. Transcripcion en vivo con modelo `small` consume
practicamente toda la CPU disponible, asi que se admite UNA reunion a la vez.
El intento numero dos recibe un "ocupado" claro en lugar de degradar las dos.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Awaitable

from .asr import AsrEngine
from .audio import WavWriter
from .config import Settings
from .db import Database
from .vad import StreamingSegmenter, Utterance

log = logging.getLogger("listener.session")

# Sentinela para cerrar la cola de transcripcion.
_STOP = object()


@dataclass
class LiveSegment:
    start_sec: float
    end_sec: float
    text: str


@dataclass
class SessionInfo:
    meeting_id: int
    title: str
    user: str
    started_at: float
    audio_seconds: float = 0.0
    queue_depth: int = 0
    segments: int = 0
    last_rtf: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "meeting_id": self.meeting_id,
            "title": self.title,
            "user": self.user,
            "elapsed_sec": round(time.monotonic() - self.started_at, 1),
            "audio_sec": round(self.audio_seconds, 1),
            "queue_depth": self.queue_depth,
            "segments": self.segments,
            "last_rtf": round(self.last_rtf, 2) if self.last_rtf else None,
        }


class LiveSession:
    """Une el WebSocket, el WAV, el VAD y el ASR de una reunion."""

    def __init__(
        self,
        meeting_id: int,
        title: str,
        user: str,
        wav_path,
        settings: Settings,
        asr: AsrEngine,
        db: Database,
        executor: ThreadPoolExecutor,
    ):
        self.settings = settings
        self.asr = asr
        self.db = db
        self.executor = executor

        self.info = SessionInfo(
            meeting_id=meeting_id,
            title=title,
            user=user,
            started_at=time.monotonic(),
        )

        self.writer = WavWriter(wav_path, settings.sample_rate)
        self.segmenter = StreamingSegmenter(
            sample_rate=settings.sample_rate,
            frame_ms=settings.frame_ms,
            aggressiveness=settings.vad_aggressiveness,
            start_ms=settings.vad_start_ms,
            end_ms=settings.vad_end_ms,
            preroll_ms=settings.vad_preroll_ms,
            max_sec=settings.max_utterance_sec,
        )

        self._queue: asyncio.Queue = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self._closed = False
        self._max_seconds = settings.max_meeting_hours * 3600

    @property
    def meeting_id(self) -> int:
        return self.info.meeting_id

    @property
    def limit_reached(self) -> bool:
        return self.segmenter.seconds_seen >= self._max_seconds

    def start_worker(self, emit: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        self._worker = asyncio.create_task(self._consume(emit))

    async def feed(self, pcm: bytes) -> None:
        """Recibe PCM del navegador. El VAD es de microsegundos: va en el loop."""
        if self._closed:
            return
        self.writer.write(pcm)
        for utterance in self.segmenter.feed(pcm):
            if utterance.duration_sec < self.settings.min_utterance_sec:
                continue
            self._queue.put_nowait(utterance)
        self.info.audio_seconds = self.segmenter.seconds_seen
        self.info.queue_depth = self._queue.qsize()

    async def _consume(self, emit: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        """Un solo consumidor: serializa la inferencia y respeta el limite de CPU."""
        loop = asyncio.get_running_loop()
        while True:
            item = await self._queue.get()
            if item is _STOP:
                self._queue.task_done()
                return
            utterance: Utterance = item
            started = time.monotonic()
            try:
                text = await loop.run_in_executor(
                    self.executor, self.asr.transcribe_live, utterance.pcm
                )
            except Exception:  # noqa: BLE001
                log.exception("fallo transcribiendo frase de la reunion %s", self.meeting_id)
                text = ""
            finally:
                self._queue.task_done()

            elapsed = time.monotonic() - started
            if utterance.duration_sec > 0:
                self.info.last_rtf = elapsed / utterance.duration_sec
            self.info.queue_depth = self._queue.qsize()

            if not text:
                continue

            self.info.segments += 1
            try:
                self.db.add_segment(
                    self.meeting_id,
                    "live",
                    utterance.start_sec,
                    utterance.end_sec,
                    text,
                )
            except Exception:  # noqa: BLE001
                log.exception("no se pudo guardar el segmento en SQLite")

            with contextlib.suppress(Exception):
                await emit(
                    {
                        "type": "segment",
                        "start": round(utterance.start_sec, 2),
                        "end": round(utterance.end_sec, 2),
                        "text": text,
                        "rtf": round(self.info.last_rtf, 2) if self.info.last_rtf else None,
                        "queue": self.info.queue_depth,
                    }
                )

    async def finish(self) -> float:
        """Cierra la frase pendiente, drena la cola y cierra el WAV.

        Devuelve la duracion grabada. Idempotente.
        """
        if self._closed:
            return self.writer.duration_sec
        self._closed = True

        tail = self.segmenter.flush()
        if tail is not None and tail.duration_sec >= self.settings.min_utterance_sec:
            self._queue.put_nowait(tail)

        self._queue.put_nowait(_STOP)
        if self._worker is not None:
            try:
                # La cola puede traer varias frases atrasadas si el ASR venia
                # con retraso; se les da tiempo generoso antes de rendirse.
                await asyncio.wait_for(self._worker, timeout=180)
            except asyncio.TimeoutError:
                log.warning("se agoto el drenaje de la cola en la reunion %s", self.meeting_id)
                self._worker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._worker

        duration = self.writer.duration_sec
        self.writer.close()
        return duration


class SessionManager:
    """Guardian de la unica sesion activa."""

    def __init__(self, settings: Settings, asr: AsrEngine, db: Database):
        self.settings = settings
        self.asr = asr
        self.db = db
        self._active: LiveSession | None = None
        self._lock = asyncio.Lock()
        # max_workers=1: la inferencia ya usa los 4 hilos de CTranslate2 por
        # dentro. Mas workers solo generarian contencion.
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="asr")

    @property
    def busy(self) -> bool:
        return self._active is not None

    def current(self) -> dict[str, Any] | None:
        return self._active.info.as_dict() if self._active else None

    async def acquire(self, title: str, user: str) -> LiveSession | None:
        """Crea la sesion, o None si ya hay una en curso."""
        async with self._lock:
            if self._active is not None:
                return None

            safe_title = (title or "").strip() or f"Reunion {datetime.now():%d-%m-%Y %H:%M}"
            meeting_id = self.db.create_meeting(safe_title, user, self.settings.live_model)
            wav_path = self.settings.recordings_dir / f"meeting-{meeting_id:06d}.wav"

            session = LiveSession(
                meeting_id=meeting_id,
                title=safe_title,
                user=user,
                wav_path=wav_path,
                settings=self.settings,
                asr=self.asr,
                db=self.db,
                executor=self.executor,
            )
            self.db.set_audio_path(meeting_id, str(wav_path))
            self._active = session
            log.info("reunion %s iniciada por %s: %s", meeting_id, user, safe_title)
            return session

    async def release(self, session: LiveSession) -> None:
        duration = await session.finish()
        next_status = "pending_final" if self.settings.enable_final else "done"
        self.db.end_meeting(session.meeting_id, duration, next_status)
        async with self._lock:
            if self._active is session:
                self._active = None
        log.info(
            "reunion %s cerrada: %.1f s de audio, %d frases -> %s",
            session.meeting_id,
            duration,
            session.info.segments,
            next_status,
        )

    def shutdown(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)
