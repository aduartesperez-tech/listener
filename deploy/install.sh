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

# --- 1. Paquetes del sistema -------------------------------------------------
log "instalando dependencias del sistema"
apt-get update -qq
apt-get install -y --no-install-recommends \
  python3 python3-venv python3-dev \
  build-essential \
  ffmpeg \
  alsa-utils \
  ca-certificates

# ffmpeg lo usa faster-whisper para leer WAV/otros formatos.
# alsa-utils trae arecord, util para grabar la muestra del benchmark.

# --- 2. Usuario de servicio --------------------------------------------------
if ! id -u "$APP_USER" >/dev/null 2>&1; then
  log "creando usuario de sistema '$APP_USER'"
  useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"
else
  log "el usuario '$APP_USER' ya existe"
fi

# --- 3. Entorno virtual ------------------------------------------------------
log "creando entorno virtual en $APP_DIR/.venv"
if [[ ! -d "$APP_DIR/.venv" ]]; then
  python3 -m venv "$APP_DIR/.venv"
fi
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip wheel
log "instalando dependencias de Python (tarda unos minutos)"
"$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

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
