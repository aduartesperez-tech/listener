"""Autenticacion por contrasena compartida.

Mientras la app solo se alcanzaba por Tailscale, la red era la autenticacion.
Al abrirla a la LAN de la institucion eso desaparece: cualquiera con un cable
en el switch llega. Aca se agrega una contrasena compartida con sesion por
cookie firmada.

Dos decisiones que importan:

1. La cabecera `Tailscale-User-Login` NO autentica. Un cliente de la LAN podria
   falsificarla, y ambos frontales (Tailscale Serve y Caddy) proxean desde
   127.0.0.1, asi que la app no puede distinguirlos por IP de origen. La
   cabecera se usa solo para ATRIBUIR quien creo una reunion.

2. Sin dependencias nuevas: la cookie se firma con HMAC-SHA256 de la stdlib.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("listener.auth")

COOKIE_NAME = "listener_session"
SESSION_VERSION = "v1"

# Proteccion contra fuerza bruta: con una sola contrasena compartida es lo
# minimo indispensable.
MAX_FAILURES = 8
FAILURE_WINDOW_SEC = 300.0
LOCKOUT_SEC = 300.0


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


class RateLimiter:
    """Bloqueo por IP tras varios intentos fallidos."""

    def __init__(self) -> None:
        self._state: dict[str, dict[str, float]] = {}

    def locked_for(self, ip: str) -> float:
        entry = self._state.get(ip)
        if entry is None:
            return 0.0
        remaining = entry.get("locked_until", 0.0) - time.monotonic()
        return max(0.0, remaining)

    def record_failure(self, ip: str) -> None:
        now = time.monotonic()
        entry = self._state.setdefault(ip, {"count": 0.0, "first": now, "locked_until": 0.0})
        # La ventana se reinicia si el ultimo fallo quedo lejos.
        if now - entry["first"] > FAILURE_WINDOW_SEC:
            entry["count"] = 0.0
            entry["first"] = now
        entry["count"] += 1
        if entry["count"] >= MAX_FAILURES:
            entry["locked_until"] = now + LOCKOUT_SEC
            entry["count"] = 0.0
            entry["first"] = now
            log.warning("bloqueando %s por %.0f s: demasiados intentos", ip, LOCKOUT_SEC)
        self._prune(now)

    def record_success(self, ip: str) -> None:
        self._state.pop(ip, None)

    def _prune(self, now: float) -> None:
        stale = [
            ip
            for ip, entry in self._state.items()
            if entry["locked_until"] < now and now - entry["first"] > FAILURE_WINDOW_SEC
        ]
        for ip in stale:
            del self._state[ip]


class Auth:
    def __init__(self, password: str, secret: bytes, session_hours: float = 12.0):
        self._password = password
        self._secret = secret
        self.session_seconds = int(session_hours * 3600)
        self.limiter = RateLimiter()

    @property
    def enabled(self) -> bool:
        return bool(self._password)

    # -- contrasena ---------------------------------------------------------

    def check_password(self, candidate: str) -> bool:
        if not self.enabled:
            return True
        # compare_digest evita filtrar la longitud por tiempo de respuesta.
        return hmac.compare_digest(candidate.encode(), self._password.encode())

    # -- cookie de sesion ---------------------------------------------------

    def issue_token(self, subject: str = "lan") -> str:
        expiry = int(time.time()) + self.session_seconds
        payload = f"{SESSION_VERSION}:{expiry}:{subject}".encode()
        signature = hmac.new(self._secret, payload, hashlib.sha256).digest()
        return f"{_b64e(payload)}.{_b64e(signature)}"

    def verify_token(self, token: str | None) -> str | None:
        """Devuelve el sujeto si la cookie es valida y vigente, si no None."""
        if not token or "." not in token:
            return None
        encoded_payload, _, encoded_signature = token.partition(".")
        try:
            payload = _b64d(encoded_payload)
            signature = _b64d(encoded_signature)
        except (ValueError, TypeError):
            return None

        expected = hmac.new(self._secret, payload, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            return None

        try:
            version, expiry_text, subject = payload.decode().split(":", 2)
        except ValueError:
            return None
        if version != SESSION_VERSION:
            return None
        try:
            if int(expiry_text) < int(time.time()):
                return None
        except ValueError:
            return None
        return subject

    # -- helpers para request / websocket -----------------------------------

    def authenticated(self, scope_like: Any) -> bool:
        """Acepta un Request o un WebSocket de Starlette: ambos traen .cookies."""
        if not self.enabled:
            return True
        return self.verify_token(scope_like.cookies.get(COOKIE_NAME)) is not None


def load_secret(explicit: str, data_dir: Path) -> bytes:
    """Secreto de firma: del entorno, o generado y persistido.

    Persistirlo importa: si cambia en cada arranque, toda sesion abierta se
    invalida cada vez que se reinicia el servicio.
    """
    if explicit:
        return hashlib.sha256(explicit.encode()).digest()

    key_path = data_dir / "session.key"
    if key_path.is_file():
        return key_path.read_bytes()

    secret = secrets.token_bytes(32)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(secret)
    try:
        key_path.chmod(0o600)
    except OSError:
        log.warning("no se pudo restringir los permisos de %s", key_path)
    log.info("generado un secreto de sesion nuevo en %s", key_path)
    return secret


def client_ip(scope_like: Any) -> str:
    """IP del cliente, mirando X-Forwarded-For porque siempre hay un proxy.

    Se toma el ULTIMO valor de la cadena, que es el que agrega nuestro propio
    frontal. Los anteriores los puede escribir el cliente y no son de fiar.
    """
    forwarded = scope_like.headers.get("x-forwarded-for")
    if forwarded:
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        if parts:
            return parts[-1]
    client = getattr(scope_like, "client", None)
    return client.host if client else "desconocido"
