"""Tests de integracion del login sobre la app real.

Cubren lo que los tests unitarios de auth.py no pueden: el middleware, las
rutas, la cookie que viaja de verdad y el rechazo del WebSocket.

El TestClient se usa SIN `with`, a proposito: asi no corre el lifespan y no se
dispara la precarga del modelo (que descargaria cientos de MB).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.auth import COOKIE_NAME, Auth

PASSWORD = "clave-de-prueba"
SECRET = b"7" * 32


@pytest.fixture
def client(monkeypatch, tmp_path):
    import app.main as main

    # El middleware resuelve `auth` como global del modulo en cada peticion,
    # asi que sustituirlo alcanza para activar la autenticacion.
    monkeypatch.setattr(main, "auth", Auth(PASSWORD, SECRET, 12.0))
    main.db.init()
    return TestClient(main.app, follow_redirects=False)


@pytest.fixture
def logged_in(client):
    response = client.post("/login", data={"password": PASSWORD, "next": "/"})
    assert COOKIE_NAME in response.cookies
    return client


# -- sin sesion --------------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/m/1"])
def test_las_paginas_redirigen_al_login(client, path):
    response = client.get(path)
    assert response.status_code == 303
    assert "/login" in response.headers["location"]


@pytest.mark.parametrize("path", ["/api/status", "/api/meetings", "/api/meetings/1"])
def test_la_api_responde_401(client, path):
    assert client.get(path).status_code == 401


@pytest.mark.parametrize("path", ["/healthz", "/login", "/static/style.css"])
def test_rutas_publicas(client, path):
    assert client.get(path).status_code == 200


# -- la cabecera de Tailscale no autoriza ------------------------------------


def test_cabecera_tailscale_falsificada_no_da_acceso(client):
    """Un cliente de la LAN puede enviar esta cabecera a mano. Ambos frontales
    proxean desde 127.0.0.1, asi que la app no puede distinguir el origen: la
    cabecera solo sirve para atribuir, nunca para autorizar."""
    forged = {"Tailscale-User-Login": "director@institucion.cr"}
    assert client.get("/", headers=forged).status_code == 303
    assert client.get("/api/meetings", headers=forged).status_code == 401


# -- websocket ---------------------------------------------------------------


def test_websocket_rechazado_sin_sesion(client):
    """El middleware HTTP no ve los WebSocket. Si esta comprobacion se rompe,
    la ruta de grabacion queda abierta aunque el resto pida contrasena."""
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/live") as ws:
            ws.send_json({"type": "start", "title": "intruso"})
            ws.receive_json()


# -- login -------------------------------------------------------------------


def test_contrasena_incorrecta_no_deja_cookie(client):
    response = client.post("/login", data={"password": "incorrecta", "next": "/"})
    assert response.status_code == 303
    assert "error=bad" in response.headers["location"]
    assert COOKIE_NAME not in response.cookies


def test_login_correcto_deja_cookie_httponly(client):
    response = client.post("/login", data={"password": PASSWORD, "next": "/"})
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert COOKIE_NAME in response.cookies
    # Sin HttpOnly, cualquier XSS se lleva la sesion.
    assert "httponly" in response.headers["set-cookie"].lower()


@pytest.mark.parametrize(
    "hostile", ["https://evil.com/x", "//evil.com", "http://evil.com", "javascript:alert(1)"]
)
def test_no_hay_open_redirect(client, hostile):
    response = client.post("/login", data={"password": PASSWORD, "next": hostile})
    assert response.headers["location"] == "/"


def test_el_destino_interno_se_respeta(client):
    response = client.post("/login", data={"password": PASSWORD, "next": "/m/7"})
    assert response.headers["location"] == "/m/7"


# -- con sesion --------------------------------------------------------------


def test_la_pagina_carga_con_sesion(logged_in):
    response = logged_in.get("/")
    assert response.status_code == 200
    assert b"LISTEN" in response.content


def test_la_api_responde_con_sesion(logged_in):
    response = logged_in.get("/api/status")
    assert response.status_code == 200
    body = response.json()
    assert body["auth_enabled"] is True
    assert body["busy"] is False
    assert logged_in.get("/api/meetings").status_code == 200


def test_logout_cierra_la_sesion(logged_in):
    assert logged_in.get("/logout").status_code == 303
    logged_in.cookies.clear()
    assert logged_in.get("/").status_code == 303


# -- auth desactivada --------------------------------------------------------


def test_sin_password_la_app_queda_abierta(monkeypatch):
    """Compatibilidad con el despliegue de solo-Tailscale, donde la red es la
    autenticacion. Arranca con un warning en el log."""
    import app.main as main

    monkeypatch.setattr(main, "auth", Auth("", SECRET, 12.0))
    main.db.init()
    open_client = TestClient(main.app, follow_redirects=False)
    assert open_client.get("/").status_code == 200
    assert open_client.get("/api/meetings").status_code == 200
