#!/usr/bin/env bash
#
# Instalacion de LISTENER en Ubuntu Server.
#
#   sudo ./deploy/install.sh
#
# Idempotente: se puede volver a correr sin romper nada.

set -euo pipefail

APP_USER="${APP_USER:-listener}"
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_NAME="listener"

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "corré esto con sudo"

# Orden de preferencia si hay varias versiones instaladas. ctranslate2 se
# distribuye como wheel compilado, y las versiones con mas rodaje son las que
# con mas seguridad tienen wheel publicado. No es un filtro: si no hay ninguna
# de estas se usa `python3` sin mas, y si la instalacion falla se busca
# alternativa entonces. Verificado con Python 3.14 en Ubuntu 26.04.
PREFERRED_PYTHONS=(python3.12 python3.11 python3.13 python3.10)

# --- 1. Paquetes del sistema -------------------------------------------------
log "instalando dependencias del sistema"
apt-get update -qq
apt-get install -y --no-install-recommends \
  python3 python3-venv python3-dev \
  build-essential \
  ffmpeg \
  alsa-utils \
  ca-certificates \
  curl

# ffmpeg lo usa faster-whisper para leer WAV/otros formatos.
# alsa-utils trae arecord, util para grabar la muestra del benchmark.

# --- 1b. Elegir interprete -----------------------------------------------------
pick_python() {
  # Respetar una eleccion explicita: PYTHON_BIN=/usr/bin/python3.12 ./install.sh
  if [[ -n "${PYTHON_BIN:-}" ]]; then
    command -v "$PYTHON_BIN" >/dev/null 2>&1 || die "PYTHON_BIN no existe: $PYTHON_BIN"
    echo "$PYTHON_BIN"
    return
  fi
  for candidate in "${PREFERRED_PYTHONS[@]}"; do
    if command -v "$candidate" >/dev/null 2>&1; then
      echo "$candidate"
      return
    fi
  done
  echo "python3"
}

py_full() { "$1" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])'; }

# Intenta instalar del archivo de Ubuntu una version alternativa de Python.
# Solo se usa como RESPALDO si el pip install ya fallo: no se descarta la
# version del sistema por su numero. Ubuntu 26.04 trae 3.14 y funciona.
install_fallback_python() {
  local version
  for version in 3.12 3.13 3.11; do
    if apt-get install -y --no-install-recommends \
         "python$version" "python$version-venv" "python$version-dev" >/dev/null 2>&1; then
      echo "python$version"
      return 0
    fi
  done
  return 1
}

PY="$(pick_python)"
log "intérprete: $PY (Python $(py_full "$PY"))"

# --- 2. Usuario de servicio --------------------------------------------------
if ! id -u "$APP_USER" >/dev/null 2>&1; then
  log "creando usuario de sistema '$APP_USER'"
  useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"
else
  log "el usuario '$APP_USER' ya existe"
fi

# --- 3. Entorno virtual ------------------------------------------------------
VENV="$APP_DIR/.venv"

build_venv() {
  local python="$1"
  log "creando entorno virtual en $VENV con $python ($(py_full "$python"))"
  rm -rf "$VENV"
  "$python" -m venv "$VENV"
  "$VENV/bin/pip" install --quiet --upgrade pip wheel
  log "instalando dependencias de Python (tarda unos minutos)"
  "$VENV/bin/pip" install -r "$APP_DIR/requirements.txt"
}

# Se intenta con el intérprete elegido. Solo si falla se busca otro: la causa
# habitual es que ctranslate2 no publique wheel para esa versión de Python, y
# entonces pip trata de compilar CTranslate2 desde fuente y no termina bien.
if [[ -d "$VENV" ]] && "$VENV/bin/python" -c 'import faster_whisper, webrtcvad' 2>/dev/null; then
  log "el venv existente ya tiene las dependencias; se actualiza sin recrearlo"
  "$VENV/bin/pip" install --quiet --upgrade pip wheel
  "$VENV/bin/pip" install -r "$APP_DIR/requirements.txt"
elif ! build_venv "$PY"; then
  warn "falló la instalación con $(py_full "$PY")."
  if [[ -n "${PYTHON_BIN:-}" ]]; then
    die "PYTHON_BIN fue explícito, no se busca alternativa. Revisá el error de arriba."
  fi
  warn "buscando otra versión de Python en el archivo de Ubuntu…"
  if ALT="$(install_fallback_python)" && build_venv "$ALT"; then
    log "resuelto con $ALT"
  else
    echo
    die "$(cat <<'MSG'
no se pudieron instalar las dependencias con ninguna versión de Python disponible.

Si el error menciona 'building wheel', 'cmake' o 'no matching distribution', el
problema es que ctranslate2 no tiene wheel para esta versión. La salida limpia es
uv, que baja un CPython propio sin tocar el del sistema:

    curl -LsSf https://astral.sh/uv/install.sh -o /tmp/uv-install.sh
    less /tmp/uv-install.sh          # revisalo antes de ejecutarlo
    sh /tmp/uv-install.sh
    ~/.local/bin/uv python install 3.12
    cd /opt/listener
    sudo PYTHON_BIN="$(~/.local/bin/uv python find 3.12)" ./deploy/install.sh
MSG
)"
  fi
fi

# Verificacion real: que los modulos importen, no solo que pip diga que si.
log "verificando las dependencias"
if ! "$VENV/bin/python" - <<'PY'
import sys
faltan = []
for mod in ("faster_whisper", "webrtcvad", "fastapi", "uvicorn", "numpy"):
    try:
        __import__(mod)
    except Exception as exc:
        faltan.append(f"  {mod}: {exc}")
if faltan:
    print("no se pudieron importar:", *faltan, sep="\n")
    sys.exit(1)
import ctranslate2
print(f"  ctranslate2 {ctranslate2.__version__}")
PY
then
  die "las dependencias se instalaron pero no importan. Revisá el detalle de arriba."
fi

# --- 4. Configuracion --------------------------------------------------------
if [[ ! -f "$APP_DIR/.env" ]]; then
  log "creando .env a partir de .env.example"
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  warn "revisá $APP_DIR/.env — sobre todo VOCAB_PROMPT con los nombres de la institución"
else
  log ".env ya existe, no se toca"
fi

mkdir -p "$APP_DIR/data/recordings"
chown -R "$APP_USER":"$APP_USER" "$APP_DIR/data" "$APP_DIR/.venv"
chown "$APP_USER":"$APP_USER" "$APP_DIR/.env"
chmod 600 "$APP_DIR/.env"

# Cache de modelos de HuggingFace bajo el home del usuario de servicio.
mkdir -p "/home/$APP_USER/.cache"
chown -R "$APP_USER":"$APP_USER" "/home/$APP_USER/.cache"

# --- 5. Servicio systemd -----------------------------------------------------
log "instalando la unidad systemd"
sed -e "s|@APP_DIR@|$APP_DIR|g" -e "s|@APP_USER@|$APP_USER|g" \
  "$APP_DIR/deploy/listener.service" > "/etc/systemd/system/$SERVICE_NAME.service"

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"

sleep 3
if systemctl is-active --quiet "$SERVICE_NAME"; then
  log "servicio activo"
else
  warn "el servicio no arrancó. Log:"
  journalctl -u "$SERVICE_NAME" -n 40 --no-pager
  exit 1
fi

# --- 6. Publicar por Tailscale ----------------------------------------------
if command -v tailscale >/dev/null 2>&1; then
  log "publicando por Tailscale Serve (TLS con renovación automática)"
  PORT="$(grep -E '^PORT=' "$APP_DIR/.env" | cut -d= -f2 | tr -d '[:space:]')"
  PORT="${PORT:-8000}"
  tailscale serve --bg --https=443 "http://127.0.0.1:$PORT"
  echo
  tailscale serve status
  echo
  log "URL:  https://$(tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' 2>/dev/null || echo '<nombre>.<tailnet>.ts.net')"
else
  warn "tailscale no está instalado: la app queda solo en http://127.0.0.1:$PORT"
  warn "sin HTTPS válido el navegador NO deja usar el micrófono"
fi

cat <<'EOF'

--------------------------------------------------------------------------
Listo. Siguientes pasos:

  1. La primera vez, el modelo se descarga de HuggingFace (~500 MB para
     small, ~1.6 GB para large-v3-turbo). Seguilo con:
         journalctl -u listener -f

  2. Medí el rendimiento real de esta máquina antes de confiar en el
     modelo por defecto:
         arecord -f S16_LE -r 16000 -c 1 -d 60 /tmp/muestra.wav
         sudo -u listener .venv/bin/python bench.py /tmp/muestra.wav

  3. Restringí quién alcanza el servicio con las ACLs de Tailscale
     (ver la sección "Acceso" del README).

  IMPORTANTE: no uses `tailscale funnel`. Eso publicaría la página en el
  internet abierto. `tailscale serve` la deja solo dentro del tailnet.
--------------------------------------------------------------------------
EOF
