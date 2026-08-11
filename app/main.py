"""LISTENER - API HTTP + WebSocket.

Se sirve solo en loopback: Tailscale Serve hace de frontal TLS y aporta la
identidad del usuario en las cabeceras Tailscale-User-*.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from urllib.parse import quote, urlparse

from fastapi import FastAPI, Form, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles

from . import __version__
from .asr import AsrEngine, format_transcript
from .auth import COOKIE_NAME, Auth, client_ip, load_secret
from .config import settings
from .db import Database
from .postprocess import PostProcessor
from .session import LiveSession, SessionManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("listener")

db = Database(settings.db_path)
asr = AsrEngine(settings)
sessions = SessionManager(settings, asr, db)
post = PostProcessor(settings, asr, db, sessions)
auth = Auth(
    settings.auth_password,
    load_secret(settings.session_secret, settings.data_dir),
    settings.session_hours,
)

# Rutas alcanzables sin sesion. Todo lo demas queda detras del login.
PUBLIC_PATHS = frozenset({"/login", "/logout", "/healthz"})

STATUS_LABELS = {
    "live": "Grabando",
    "pending_final": "En cola para el acta final",
    "processing_final": "Generando acta final",
    "done": "Lista",
    "failed": "Acta final fallida",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    log.info(
        "LISTENER %s | vivo=%s | acta=%s | diarizacion=%s | auth=%s",
        __version__,
        settings.live_model,
        settings.final_model if settings.enable_final else "off",
        settings.enable_diarization,
        "contrasena" if auth.enabled else "DESACTIVADA",
    )
    if not auth.enabled:
        log.warning(
            "AUTH_PASSWORD esta vacio: cualquiera que alcance el puerto entra y"
            " puede leer todas las actas. Aceptable solo si el unico camino de"
            " entrada es Tailscale."
        )
    # Precarga en segundo plano: el server responde ya, y la primera frase de
    # la reunion no paga el arranque del modelo.
    warm = asyncio.create_task(asyncio.to_thread(_safe_warmup))
    post.start()
    try:
        yield
    finally:
        warm.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await warm
        await post.stop()
        sessions.shutdown()


def _safe_warmup() -> None:
    try:
        asr.warmup_live()
        log.info("modelo en vivo precargado")
    except Exception:  # noqa: BLE001
        log.exception("no se pudo precargar el modelo en vivo")


app = FastAPI(title="LISTENER", version=__version__, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=settings.static_dir), name="static")


def _user(request_or_ws) -> str:
    """Nombre para ATRIBUIR una reunion. Nunca para autorizar.

    Tailscale Serve inyecta estas cabeceras, pero un cliente de la LAN podria
    falsificarlas y ambos frontales proxean desde 127.0.0.1, asi que la app no
    puede distinguir el origen. El acceso lo decide la cookie de sesion; esto
    es solo una etiqueta. Caddy las borra de las peticiones entrantes.
    """
    headers = request_or_ws.headers
    name = headers.get("tailscale-user-login") or headers.get("tailscale-user-name")
    if name:
        return name[:120]
    return "anonimo"


def _safe_next(raw: str | None) -> str:
    """Evita open redirect: solo se acepta una ruta interna."""
    if not raw:
        return "/"
    parsed = urlparse(raw)
    if parsed.scheme or parsed.netloc or not raw.startswith("/"):
        return "/"
    return raw


@app.middleware("http")
async def require_auth(request: Request, call_next):
    """Puerta de entrada de todo el HTTP. El WebSocket se valida por separado
    porque el middleware HTTP de Starlette no lo intercepta."""
    path = request.url.path
    if (
        not auth.enabled
        or path in PUBLIC_PATHS
        or path.startswith("/static/")
        or path == "/favicon.ico"
    ):
        return await call_next(request)

    if auth.authenticated(request):
        return await call_next(request)

    if path.startswith("/api/"):
        return JSONResponse({"error": "no autenticado"}, status_code=401)
    return RedirectResponse(f"/login?next={quote(path)}", status_code=303)


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


@app.get("/login", include_in_schema=False)
async def login_page(request: Request) -> Response:
    if not auth.enabled or auth.authenticated(request):
        return RedirectResponse(_safe_next(request.query_params.get("next")), status_code=303)
    return FileResponse(settings.static_dir / "login.html")


@app.post("/login", include_in_schema=False)
async def login_submit(
    request: Request,
    password: str = Form(""),
    next: str = Form("/"),
) -> Response:
    target = _safe_next(next)
    if not auth.enabled:
        return RedirectResponse(target, status_code=303)

    ip = client_ip(request)
    locked = auth.limiter.locked_for(ip)
    if locked > 0:
        log.warning("intento de login desde %s bloqueado (%.0f s restantes)", ip, locked)
        return RedirectResponse(
            f"/login?error=locked&wait={int(locked)}&next={quote(target)}", status_code=303
        )

    if not auth.check_password(password):
        auth.limiter.record_failure(ip)
        log.warning("contrasena incorrecta desde %s", ip)
        return RedirectResponse(f"/login?error=bad&next={quote(target)}", status_code=303)

    auth.limiter.record_success(ip)
    response = RedirectResponse(target, status_code=303)
    response.set_cookie(
        COOKIE_NAME,
        auth.issue_token(),
        max_age=auth.session_seconds,
        httponly=True,
        samesite="lax",
        # secure=True romperia el acceso por http://localhost, que es como se
        # prueba con un tunel SSH. El transporte real siempre es HTTPS por
        # Caddy o Tailscale Serve.
        secure=False,
        path="/",
    )
    log.info("login correcto desde %s", ip)
    return response


@app.get("/logout", include_in_schema=False)
async def logout() -> Response:
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE_NAME, path="/")
    return response


# ---------------------------------------------------------------------------
# Paginas
# ---------------------------------------------------------------------------


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(settings.static_dir / "index.html")


@app.get("/m/{meeting_id}", include_in_schema=False)
async def meeting_page(meeting_id: int) -> FileResponse:
    return FileResponse(settings.static_dir / "meeting.html")


@app.get("/healthz", include_in_schema=False)
async def healthz() -> PlainTextResponse:
    return PlainTextResponse("ok")


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@app.get("/api/status")
async def api_status(request: Request) -> dict:
    return {
        "version": __version__,
        "user": _user(request),
        "auth_enabled": auth.enabled,
        "busy": sessions.busy,
        "active": sessions.current(),
        "language": settings.language,
        "asr": asr.status(),
        "post": post.status(),
        "limits": {
            "max_meeting_hours": settings.max_meeting_hours,
            "concurrent_meetings": 1,
        },
    }


@app.get("/api/meetings")
async def api_meetings() -> list[dict]:
    out = []
    for row in db.list_meetings():
        out.append(
            {
                "id": row["id"],
                "title": row["title"],
                "created_by": row["created_by"],
                "started_at": row["started_at"],
                "ended_at": row["ended_at"],
                "status": row["status"],
                "status_label": STATUS_LABELS.get(row["status"], row["status"]),
                "duration_sec": row["duration_sec"],
                "segments": row["live_segments"],
                "has_final": bool(row["final_text"]),
            }
        )
    return out


@app.get("/api/meetings/{meeting_id}")
async def api_meeting(meeting_id: int) -> dict:
    row = db.get_meeting(meeting_id)
    if row is None:
        raise HTTPException(404, "reunion no encontrada")
    live = db.get_segments(meeting_id, "live")
    final = db.get_segments(meeting_id, "final")
    return {
        "id": row["id"],
        "title": row["title"],
        "created_by": row["created_by"],
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "status": row["status"],
        "status_label": STATUS_LABELS.get(row["status"], row["status"]),
        "duration_sec": row["duration_sec"],
        "live_model": row["live_model"],
        "final_model": row["final_model"],
        "error": row["error"],
        "has_audio": bool(row["audio_path"] and Path(row["audio_path"]).is_file()),
        "live_segments": live,
        "final_segments": final,
        "processing": post.status(),
    }


@app.get("/api/meetings/{meeting_id}/transcript.txt", response_class=PlainTextResponse)
async def api_transcript(meeting_id: int, kind: str = "auto") -> PlainTextResponse:
    row = db.get_meeting(meeting_id)
    if row is None:
        raise HTTPException(404, "reunion no encontrada")

    if kind == "auto":
        kind = "final" if row["final_text"] else "live"
    if kind not in ("live", "final"):
        raise HTTPException(400, "kind debe ser 'live', 'final' o 'auto'")

    segments = db.get_segments(meeting_id, kind)
    if not segments:
        raise HTTPException(404, f"no hay transcripcion '{kind}' para esta reunion")

    header = [
        f"{row['title']}",
        f"Inicio: {row['started_at']}   Duracion: {int(row['duration_sec'] // 60)} min",
        f"Fuente: {'acta final (' + str(row['final_model']) + ')' if kind == 'final' else 'transcripcion en vivo (' + str(row['live_model']) + ')'}",
        "-" * 68,
        "",
    ]
    body = format_transcript(
        [
            {
                "start": s["start_sec"],
                "end": s["end_sec"],
                "speaker": s["speaker"],
                "text": s["text"],
            }
            for s in segments
        ]
    )
    return PlainTextResponse(
        "\n".join(header) + body,
        headers={
            "Content-Disposition": f'attachment; filename="reunion-{meeting_id:06d}-{kind}.txt"'
        },
    )


@app.get("/api/meetings/{meeting_id}/audio.wav")
async def api_audio(meeting_id: int) -> FileResponse:
    row = db.get_meeting(meeting_id)
    if row is None or not row["audio_path"]:
        raise HTTPException(404, "reunion no encontrada")
    path = Path(row["audio_path"])
    if not path.is_file():
        raise HTTPException(404, "grabacion no encontrada en disco")
    return FileResponse(path, media_type="audio/wav", filename=path.name)


@app.post("/api/meetings/{meeting_id}/reprocess")
async def api_reprocess(meeting_id: int) -> dict:
    row = db.get_meeting(meeting_id)
    if row is None:
        raise HTTPException(404, "reunion no encontrada")
    if row["status"] in ("live", "processing_final"):
        raise HTTPException(409, "la reunion esta en curso o ya se esta procesando")
    if not row["audio_path"] or not Path(row["audio_path"]).is_file():
        raise HTTPException(400, "no queda grabacion para reprocesar")
    if not settings.enable_final:
        raise HTTPException(400, "ENABLE_FINAL esta desactivado en la configuracion")
    db.set_status(meeting_id, "pending_final")
    return {"ok": True, "status": "pending_final"}


@app.delete("/api/meetings/{meeting_id}")
async def api_delete(meeting_id: int) -> dict:
    row = db.get_meeting(meeting_id)
    if row is None:
        raise HTTPException(404, "reunion no encontrada")
    if row["status"] in ("live", "processing_final"):
        raise HTTPException(409, "no se puede borrar una reunion en curso")
    audio_path = db.delete_meeting(meeting_id)
    if audio_path:
        with contextlib.suppress(OSError):
            Path(audio_path).unlink(missing_ok=True)
    return {"ok": True}


# ---------------------------------------------------------------------------
# WebSocket de la sesion en vivo
# ---------------------------------------------------------------------------


@app.websocket("/ws/live")
async def ws_live(ws: WebSocket) -> None:
    # El middleware HTTP no ve los WebSocket: hay que validar aca a mano, antes
    # de aceptar. Sin esto, la ruta de grabacion quedaria abierta aunque el
    # resto de la app pida contrasena.
    if not auth.authenticated(ws):
        log.warning("WebSocket rechazado sin sesion desde %s", client_ip(ws))
        await ws.close(code=1008)  # 1008 = policy violation
        return

    await ws.accept()
    user = _user(ws)

    # Primer mensaje: {"type":"start","title":"..."}
    try:
        hello = json.loads(await asyncio.wait_for(ws.receive_text(), timeout=15))
    except (asyncio.TimeoutError, json.JSONDecodeError, WebSocketDisconnect, RuntimeError):
        with contextlib.suppress(Exception):
            await ws.close(code=1002)
        return

    if hello.get("type") != "start":
        with contextlib.suppress(Exception):
            await ws.send_json({"type": "error", "message": "se esperaba un mensaje 'start'"})
            await ws.close(code=1002)
        return

    session = await sessions.acquire(str(hello.get("title", "")), user)
    if session is None:
        # 1013 = try again later. El cliente muestra quien la tiene ocupada.
        with contextlib.suppress(Exception):
            await ws.send_json(
                {
                    "type": "busy",
                    "message": "Ya hay una reunion en curso. El servidor admite una a la vez.",
                    "active": sessions.current(),
                }
            )
            await ws.close(code=1013)
        return

    async def emit(payload: dict) -> None:
        await ws.send_json(payload)

    session.start_worker(emit)
    heartbeat = asyncio.create_task(_heartbeat(ws, session))

    await ws.send_json(
        {
            "type": "ready",
            "meeting_id": session.meeting_id,
            "title": session.info.title,
            "user": user,
            "model": settings.live_model,
            "language": settings.language,
        }
    )

    try:
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                break

            chunk = message.get("bytes")
            if chunk:
                await session.feed(chunk)
                if session.limit_reached:
                    with contextlib.suppress(Exception):
                        await ws.send_json(
                            {
                                "type": "limit",
                                "message": f"Se alcanzo el limite de {settings.max_meeting_hours} h. Cerrando.",
                            }
                        )
                    break
                continue

            raw = message.get("text")
            if raw:
                with contextlib.suppress(json.JSONDecodeError):
                    data = json.loads(raw)
                    if data.get("type") == "stop":
                        break
    except WebSocketDisconnect:
        log.info("el cliente de la reunion %s se desconecto", session.meeting_id)
    except Exception:  # noqa: BLE001
        log.exception("error en el WebSocket de la reunion %s", session.meeting_id)
    finally:
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat

        with contextlib.suppress(Exception):
            await ws.send_json({"type": "closing", "message": "Procesando lo que falta..."})

        await sessions.release(session)

        with contextlib.suppress(Exception):
            await ws.send_json(
                {
                    "type": "ended",
                    "meeting_id": session.meeting_id,
                    "segments": session.info.segments,
                    "will_reprocess": settings.enable_final,
                }
            )
            await ws.close()


async def _heartbeat(ws: WebSocket, session: LiveSession) -> None:
    """Estado cada 2 s: es lo que deja ver si el ASR se esta quedando atras."""
    try:
        while True:
            await asyncio.sleep(2.0)
            await ws.send_json({"type": "status", **session.info.as_dict()})
    except (asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
        return
    except Exception:  # noqa: BLE001
        return


@app.exception_handler(HTTPException)
async def http_error(_request: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
