"""Persistencia en SQLite.

Se abre una conexion por operacion (WAL activo). Con un solo escritor real
—la sesion en vivo— esto es de sobra y evita problemas de hilos entre el
event loop, el executor de ASR y el worker de post-proceso.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

# Estados de una reunion:
#   live              -> grabando ahora mismo
#   pending_final     -> termino, espera el reproceso de calidad
#   processing_final  -> el worker la esta reprocesando
#   done              -> lista
#   failed            -> el reproceso fallo (el texto en vivo sigue disponible)
STATUSES = ("live", "pending_final", "processing_final", "done", "failed")

SCHEMA = """
CREATE TABLE IF NOT EXISTS meetings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    title         TEXT    NOT NULL,
    created_by    TEXT    NOT NULL DEFAULT '',
    started_at    TEXT    NOT NULL,
    ended_at      TEXT,
    status        TEXT    NOT NULL DEFAULT 'live',
    duration_sec  REAL    NOT NULL DEFAULT 0,
    audio_path    TEXT,
    live_model    TEXT,
    final_model   TEXT,
    final_text    TEXT,
    error         TEXT,
    finalized_at  TEXT
);

CREATE TABLE IF NOT EXISTS segments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id  INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    kind        TEXT    NOT NULL,          -- 'live' | 'final'
    start_sec   REAL    NOT NULL,
    end_sec     REAL    NOT NULL,
    speaker     TEXT,
    text        TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_segments_meeting
    ON segments(meeting_id, kind, start_sec);

CREATE INDEX IF NOT EXISTS idx_meetings_status
    ON meetings(status, id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(self.path, timeout=30.0)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute("PRAGMA foreign_keys=ON")
        try:
            yield con
            con.commit()
        finally:
            con.close()

    def init(self) -> None:
        with self._conn() as con:
            con.executescript(SCHEMA)
        # Una reunion que quedo en 'live' o 'processing_final' significa que el
        # proceso murio a media faena. Se reencola para el reproceso.
        with self._conn() as con:
            con.execute(
                "UPDATE meetings SET status='pending_final', ended_at=COALESCE(ended_at, ?)"
                " WHERE status IN ('live', 'processing_final')",
                (_now(),),
            )

    # -- reuniones ----------------------------------------------------------

    def create_meeting(self, title: str, created_by: str, live_model: str) -> int:
        with self._conn() as con:
            cur = con.execute(
                "INSERT INTO meetings (title, created_by, started_at, status, live_model)"
                " VALUES (?, ?, ?, 'live', ?)",
                (title, created_by, _now(), live_model),
            )
            return int(cur.lastrowid)

    def set_audio_path(self, meeting_id: int, path: str) -> None:
        with self._conn() as con:
            con.execute(
                "UPDATE meetings SET audio_path=? WHERE id=?", (path, meeting_id)
            )

    def end_meeting(self, meeting_id: int, duration_sec: float, status: str) -> None:
        with self._conn() as con:
            con.execute(
                "UPDATE meetings SET ended_at=?, duration_sec=?, status=? WHERE id=?",
                (_now(), duration_sec, status, meeting_id),
            )

    def set_status(self, meeting_id: int, status: str, error: str | None = None) -> None:
        with self._conn() as con:
            con.execute(
                "UPDATE meetings SET status=?, error=? WHERE id=?",
                (status, error, meeting_id),
            )

    def save_final(self, meeting_id: int, model: str, text: str) -> None:
        with self._conn() as con:
            con.execute(
                "UPDATE meetings SET status='done', final_model=?, final_text=?,"
                " finalized_at=?, error=NULL WHERE id=?",
                (model, text, _now(), meeting_id),
            )

    def get_meeting(self, meeting_id: int) -> dict[str, Any] | None:
        with self._conn() as con:
            row = con.execute(
                "SELECT * FROM meetings WHERE id=?", (meeting_id,)
            ).fetchone()
            return dict(row) if row else None

    def list_meetings(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._conn() as con:
            rows = con.execute(
                "SELECT m.*,"
                " (SELECT COUNT(*) FROM segments s WHERE s.meeting_id=m.id"
                "  AND s.kind='live') AS live_segments"
                " FROM meetings m ORDER BY m.id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def next_pending(self) -> dict[str, Any] | None:
        with self._conn() as con:
            row = con.execute(
                "SELECT * FROM meetings WHERE status='pending_final'"
                " ORDER BY id ASC LIMIT 1"
            ).fetchone()
            return dict(row) if row else None

    def delete_meeting(self, meeting_id: int) -> str | None:
        """Borra la reunion y devuelve la ruta del audio para que la limpie el caller."""
        with self._conn() as con:
            row = con.execute(
                "SELECT audio_path FROM meetings WHERE id=?", (meeting_id,)
            ).fetchone()
            if row is None:
                return None
            con.execute("DELETE FROM segments WHERE meeting_id=?", (meeting_id,))
            con.execute("DELETE FROM meetings WHERE id=?", (meeting_id,))
            return row["audio_path"]

    # -- segmentos ----------------------------------------------------------

    def add_segment(
        self,
        meeting_id: int,
        kind: str,
        start_sec: float,
        end_sec: float,
        text: str,
        speaker: str | None = None,
    ) -> int:
        with self._conn() as con:
            cur = con.execute(
                "INSERT INTO segments (meeting_id, kind, start_sec, end_sec, speaker, text)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (meeting_id, kind, start_sec, end_sec, speaker, text),
            )
            return int(cur.lastrowid)

    def replace_segments(
        self, meeting_id: int, kind: str, segments: list[dict[str, Any]]
    ) -> None:
        with self._conn() as con:
            con.execute(
                "DELETE FROM segments WHERE meeting_id=? AND kind=?", (meeting_id, kind)
            )
            con.executemany(
                "INSERT INTO segments (meeting_id, kind, start_sec, end_sec, speaker, text)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        meeting_id,
                        kind,
                        s["start"],
                        s["end"],
                        s.get("speaker"),
                        s["text"],
                    )
                    for s in segments
                ],
            )

    def get_segments(self, meeting_id: int, kind: str) -> list[dict[str, Any]]:
        with self._conn() as con:
            rows = con.execute(
                "SELECT start_sec, end_sec, speaker, text FROM segments"
                " WHERE meeting_id=? AND kind=? ORDER BY start_sec, id",
                (meeting_id, kind),
            ).fetchall()
            return [dict(r) for r in rows]
