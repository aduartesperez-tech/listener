#!/usr/bin/env bash
#
# Actualiza LISTENER en el servidor desde el repo.
#
#   sudo ./deploy/update.sh
#
# Se niega a actuar si hay una reunion en curso: cortar una reunion en vivo
# para desplegar codigo no es aceptable.

set -euo pipefail

APP_USER="${APP_USER:-listener}"
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_NAME="listener"
PORT="$(grep -E '^PORT=' "$APP_DIR/.env" 2>/dev/null | cut -d= -f2 | tr -d '[:space:]')"
PORT="${PORT:-8000}"

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "corré esto con sudo"

# --- Verificar que no haya nadie grabando ------------------------------------
if systemctl is-active --quiet "$SERVICE_NAME"; then
  BUSY="$(curl -fsS --max-time 5 "http://127.0.0.1:$PORT/api/status" 2>/dev/null \
          | python3 -c 'import json,sys; print(json.load(sys.stdin)["busy"])' 2>/dev/null || echo unknown)"
  if [[ "$BUSY" == "True" ]]; then
    die "hay una reunión en curso. Esperá a que termine o pará el servicio a mano."
  fi
fi

log "trayendo los cambios del repo"
sudo -u "$(stat -c '%U' "$APP_DIR/.git" 2>/dev/null || echo root)" \
  git -C "$APP_DIR" pull --ff-only

log "actualizando dependencias"
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade -r "$APP_DIR/requirements.txt"

# Si .env.example gano claves nuevas, avisar en vez de sobreescribir.
if [[ -f "$APP_DIR/.env" ]]; then
  MISSING="$(comm -23 \
    <(grep -oE '^[A-Z_]+=' "$APP_DIR/.env.example" | sort -u) \
    <(grep -oE '^[A-Z_]+=' "$APP_DIR/.env"         | sort -u) || true)"
  if [[ -n "$MISSING" ]]; then
    printf '\033[1;33m!!\033[0m claves nuevas en .env.example que no están en tu .env:\n%s\n' "$MISSING"
  fi
fi

chown -R "$APP_USER":"$APP_USER" "$APP_DIR/data"

log "reiniciando el servicio"
systemctl restart "$SERVICE_NAME"
sleep 3
systemctl is-active --quiet "$SERVICE_NAME" || {
  journalctl -u "$SERVICE_NAME" -n 40 --no-pager
  die "el servicio no volvió a arrancar"
}

log "listo — $(curl -fsS "http://127.0.0.1:$PORT/healthz" || echo 'sin respuesta de /healthz')"
