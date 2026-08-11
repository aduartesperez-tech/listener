"""Worker del segundo nivel: el acta final de calidad.

Cuando termina una reunion, el servidor queda ocioso. Se aprovecha para
reprocesar la grabacion completa con un modelo grande (y diarizacion si esta
activada), sin prisa y con contexto de toda la reunion.

Regla de oro: el vivo manda. Este worker no arranca si hay una reunion en
curso, y si una arranca a mitad de trabajo, aborta y se reencola.
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import diarize
from .asr import Aborted, AsrEngine, format_transcript
from .config import Settings
from .db import Database
from .session import SessionManager

log = logging.getLogger("listener.postprocess")

IDLE_POLL_SEC = 5.0


class PostProcessor:
    def __init__(
        self,
        settings: Settings,
        asr: AsrEngine,
        db: Database,
        sessions: SessionManager,
    ):
        self.settings = settings
        self.asr = asr
        self.db = db
        self.sessions = sessions
        # Executor propio: nunca comparte cola con la inferencia en vivo.
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="post")
        self._task: asyncio.Task | None = None
        self._current_id: int | None = None
        self._progress = 0.0
        self._stop = asyncio.Event()

    # -- ciclo de vida ------------------------------------------------------

    def start(self) -> None:
        if not self.settings.enable_final:
            log.info("ENABLE_FINAL=false: no se reprocesan las grabaciones")
            return
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._executor.shutdown(wait=False, cancel_futures=True)

    def status(self) -> dict:
        pending = self.db.next_pending()
        return {
            "enabled": self.settings.enable_final,
            "processing_id": self._current_id,
            "progress_sec": round(self._progress, 1),
            "has_pending": pending is not None,
            "diarization": self.settings.enable_diarization,
        }

    # -- bucle --------------------------------------------------------------

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self.sessions.busy:
                    await asyncio.sleep(IDLE_POLL_SEC)
                    continue

                job = self.db.next_pending()
                if job is None:
                    # Nada que hacer: se libera la RAM del modelo grande.
                    if self._current_id is None:
                        self.asr.release_final()
                    await asyncio.sleep(IDLE_POLL_SEC)
                    continue

                await self._process(job)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("error inesperado en el worker de post-proceso")
                await asyncio.sleep(IDLE_POLL_SEC)

    async def _process(self, job: dict) -> None:
        meeting_id = int(job["id"])
        audio_path = Path(job["audio_path"]) if job.get("audio_path") else None

        if audio_path is None or not audio_path.is_file():
            log.warning("reunion %s sin grabacion: se marca como done", meeting_id)
            self.db.set_status(meeting_id, "done", "grabacion no encontrada")
            return

        self.db.set_status(meeting_id, "processing_final")
        self._current_id = meeting_id
        self._progress = 0.0
        log.info(
            "reprocesando reunion %s con %s (%.1f MB de audio)",
            meeting_id,
            self.settings.final_model,
            audio_path.stat().st_size / 1e6,
        )

        loop = asyncio.get_running_loop()
        try:
            segments = await loop.run_in_executor(
                self._executor, self._run_blocking, audio_path
            )
        except Aborted:
            log.info(
                "reunion %s abortada: arranco una reunion en vivo, se reencola",
                meeting_id,
            )
            self.db.set_status(meeting_id, "pending_final")
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("fallo el reproceso de la reunion %s", meeting_id)
            # El texto en vivo sigue disponible: no se pierde nada.
            self.db.set_status(meeting_id, "failed", str(exc)[:500])
            return
        finally:
            self._current_id = None
            self._progress = 0.0

        self.db.replace_segments(meeting_id, "final", segments)
        text = format_transcript(segments)
        self.db.save_final(meeting_id, self.settings.final_model, text)
        log.info("reunion %s lista: %d segmentos finales", meeting_id, len(segments))

    def _run_blocking(self, audio_path: Path) -> list[dict]:
        """Corre en el executor. Cede la CPU en cuanto hay una reunion en vivo."""

        def should_abort() -> bool:
            return self.sessions.busy

        def on_progress(seconds: float) -> None:
            self._progress = seconds

        segments = self.asr.transcribe_file(
            audio_path, should_abort=should_abort, on_progress=on_progress
        )

        if self.settings.enable_diarization and segments:
            if should_abort():
                raise Aborted()
            turns = diarize.speaker_turns(audio_path, self.settings.hf_token)
            segments = diarize.assign_speakers(segments, turns)

        return segments
