"""Tests de la autenticacion.

Es la parte donde un error se paga caro: si la cookie se puede falsificar o el
WebSocket queda abierto, las actas de todas las reuniones quedan expuestas.
"""

from __future__ import annotations

import time
import types

import pytest

from app.auth import (
    COOKIE_NAME,
    LOCKOUT_SEC,
    MAX_FAILURES,
    Auth,
    RateLimiter,
    client_ip,
    load_secret,
)

SECRET = b"0" * 32
OTHER_SECRET = b"1" * 32


def make_auth(password: str = "clave-de-prueba", hours: float = 12.0) -> Auth:
    return Auth(password, SECRET, hours)


# -- contrasena --------------------------------------------------------------


def test_contrasena_correcta_e_incorrecta():
    auth = make_auth("secreta")
    assert auth.check_password("secreta")
    assert not auth.check_password("Secreta")
    assert not auth.check_password("secreta ")
    assert not auth.check_password("")
    assert not auth.check_password("secreta-mas-larga")


def test_sin_contrasena_la_auth_queda_desactivada():
    auth = make_auth("")
    assert not auth.enabled
    assert auth.check_password("cualquier-cosa")
    assert auth.authenticated(_fake_request())


# -- cookie de sesion --------------------------------------------------------


def test_token_ida_y_vuelta():
    auth = make_auth()
    token = auth.issue_token("adrian@institucion.cr")
    assert auth.verify_token(token) == "adrian@institucion.cr"


def test_token_manipulado_se_rechaza():
    auth = make_auth()
    token = auth.issue_token()
    payload, _, signature = token.partition(".")

    # Firma cambiada
    assert auth.verify_token(f"{payload}.{'A' * len(signature)}") is None
    # Payload cambiado, firma original
    assert auth.verify_token(f"{'A' * len(payload)}.{signature}") is None
    # Sin separador
    assert auth.verify_token(payload) is None
    # Basura
    assert auth.verify_token("no-es-un-token") is None
    assert auth.verify_token("") is None
    assert auth.verify_token(None) is None


def test_token_firmado_con_otro_secreto_se_rechaza():
    """Rotar SESSION_SECRET tiene que invalidar las sesiones anteriores."""
    emisor = Auth("x", SECRET, 12.0)
    verificador = Auth("x", OTHER_SECRET, 12.0)
    assert verificador.verify_token(emisor.issue_token()) is None


def test_token_expirado_se_rechaza():
    auth = make_auth(hours=-1.0)  # ya vencido al emitirse
    assert auth.verify_token(auth.issue_token()) is None


def test_token_a_punto_de_vencer_sigue_valido():
    auth = make_auth(hours=1.0 / 3600)  # 1 segundo
    token = auth.issue_token()
    assert auth.verify_token(token) is not None


def test_authenticated_lee_la_cookie():
    auth = make_auth()
    assert not auth.authenticated(_fake_request())
    assert not auth.authenticated(_fake_request(cookies={COOKIE_NAME: "falso"}))
    good = auth.issue_token()
    assert auth.authenticated(_fake_request(cookies={COOKIE_NAME: good}))


# -- fuerza bruta ------------------------------------------------------------


def test_bloqueo_tras_intentos_fallidos():
    limiter = RateLimiter()
    ip = "10.10.2.99"
    assert limiter.locked_for(ip) == 0.0

    for _ in range(MAX_FAILURES - 1):
        limiter.record_failure(ip)
    assert limiter.locked_for(ip) == 0.0, "no debe bloquear antes del umbral"

    limiter.record_failure(ip)
    remaining = limiter.locked_for(ip)
    assert remaining > 0
    assert remaining <= LOCKOUT_SEC


def test_el_bloqueo_es_por_ip():
    limiter = RateLimiter()
    for _ in range(MAX_FAILURES):
        limiter.record_failure("10.10.2.99")
    assert limiter.locked_for("10.10.2.99") > 0
    assert limiter.locked_for("10.10.2.100") == 0.0


def test_un_login_correcto_limpia_los_fallos():
    limiter = RateLimiter()
    ip = "10.10.2.99"
    for _ in range(MAX_FAILURES - 1):
        limiter.record_failure(ip)
    limiter.record_success(ip)
    for _ in range(MAX_FAILURES - 1):
        limiter.record_failure(ip)
    assert limiter.locked_for(ip) == 0.0


# -- IP del cliente ----------------------------------------------------------


def test_client_ip_toma_el_ultimo_valor_de_forwarded_for():
    """El ultimo lo pone nuestro proxy; los anteriores los puede falsear el
    cliente para evadir el bloqueo por intentos."""
    request = _fake_request(
        headers={"x-forwarded-for": "1.2.3.4, 5.6.7.8, 10.10.2.50"},
        client_host="127.0.0.1",
    )
    assert client_ip(request) == "10.10.2.50"


def test_client_ip_cae_al_socket_sin_forwarded_for():
    assert client_ip(_fake_request(client_host="10.10.2.7")) == "10.10.2.7"


def test_client_ip_sin_datos():
    request = _fake_request()
    request.client = None
    assert client_ip(request) == "desconocido"


# -- secreto persistido ------------------------------------------------------


def test_load_secret_persiste_entre_arranques(tmp_path):
    """Si el secreto cambiara en cada reinicio, todas las sesiones se caerian."""
    first = load_secret("", tmp_path)
    second = load_secret("", tmp_path)
    assert first == second
    assert (tmp_path / "session.key").is_file()


def test_load_secret_respeta_el_valor_explicito(tmp_path):
    a = load_secret("mi-secreto", tmp_path)
    b = load_secret("mi-secreto", tmp_path)
    c = load_secret("otro-secreto", tmp_path)
    assert a == b
    assert a != c
    # Con valor explicito no se crea el archivo.
    assert not (tmp_path / "session.key").exists()


# -- utilidades --------------------------------------------------------------


def _fake_request(cookies=None, headers=None, client_host="127.0.0.1"):
    """Imita lo poco que auth.py usa de Request/WebSocket de Starlette."""
    request = types.SimpleNamespace()
    request.cookies = cookies or {}
    request.headers = headers or {}
    request.client = types.SimpleNamespace(host=client_host)
    return request
