#!/usr/bin/env bash
#
# Publica LISTENER en la LAN de la institucion con HTTPS valido.
#
#   sudo ./deploy/setup-lan.sh listener.institucion.cr
#
# El servidor YA esta en la red interna (eno1). No hace falta ningun puente ni
# subnet router de Tailscale: lo unico que faltaba era un certificado que los
# dispositivos de la institucion confien, y que algo escuche en la interfaz de
# la LAN. La app sigue escuchando solo en 127.0.0.1; Caddy es el unico frontal.

set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DOMAIN="${1:-}"

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "corré esto con sudo"
[[ -n "$DOMAIN" ]] || die "uso: sudo ./deploy/setup-lan.sh listener.institucion.cr"

# --- 0. La contrasena es obligatoria en la LAN -------------------------------
# Sin esto, publicar en la LAN significa que cualquiera con un cable en el
# switch puede grabar y leer todas las actas anteriores.
if ! grep -qE '^AUTH_PASSWORD=.+' "$APP_DIR/.env" 2>/dev/null; then
  echo
  die "$(cat <<'MSG'
AUTH_PASSWORD está vacío en .env.

Publicar en la LAN sin contraseña deja las actas de todas las reuniones al
alcance de cualquiera que enchufe un cable al switch.

Generá una y volvé a correr este script:

    openssl rand -base64 18
    sudo nano /opt/listener/.env      # pegala en AUTH_PASSWORD=
    sudo systemctl restart listener
MSG
)"
fi

# --- 1. Instalar Caddy -------------------------------------------------------
if ! command -v caddy >/dev/null 2>&1; then
  log "instalando Caddy desde el repositorio oficial"
  apt-get install -y --no-install-recommends debian-keyring debian-archive-keyring \
    apt-transport-https curl gnupg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    | tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
  apt-get update -qq
  apt-get install -y caddy
else
  log "Caddy ya está instalado ($(caddy version | head -1))"
fi

# --- 2. Comprobar que el nombre resuelve a este servidor ---------------------
LAN_IP="$(ip -4 -o addr show scope global | awk '{print $4}' | cut -d/ -f1 | head -1)"
log "IP de este servidor en la LAN: $LAN_IP"

RESOLVED="$(getent hosts "$DOMAIN" 2>/dev/null | awk '{print $1}' | head -1 || true)"
if [[ -z "$RESOLVED" ]]; then
  warn "$DOMAIN todavía no resuelve."
  warn "Agregá un registro A:  $DOMAIN  ->  $LAN_IP"
  warn "Se sigue igual: Caddy reintentará cuando el DNS propague."
elif [[ "$RESOLVED" != "$LAN_IP" ]]; then
  warn "$DOMAIN resuelve a $RESOLVED, no a $LAN_IP."
  warn "Los dispositivos de la LAN no van a llegar. Corregí el registro A."
else
  log "$DOMAIN resuelve correctamente a $LAN_IP"
fi

# --- 3. Caddyfile ------------------------------------------------------------
if [[ -f /etc/caddy/Caddyfile ]] && ! grep -q 'LISTENER' /etc/caddy/Caddyfile; then
  cp /etc/caddy/Caddyfile "/etc/caddy/Caddyfile.bak.$(date +%s)"
  log "respaldado el Caddyfile anterior"
fi

if [[ ! -f /etc/caddy/Caddyfile ]] || ! grep -q "$DOMAIN" /etc/caddy/Caddyfile; then
  log "escribiendo /etc/caddy/Caddyfile para $DOMAIN"
  sed "s|listener\.institucion\.cr|$DOMAIN|g" \
    "$APP_DIR/deploy/Caddyfile.example" > /etc/caddy/Caddyfile
  warn "editá /etc/caddy/Caddyfile: el correo y el proveedor de DNS del bloque tls"
else
  log "el Caddyfile ya menciona $DOMAIN, no se sobrescribe"
fi

touch /etc/caddy/caddy.env
chmod 600 /etc/caddy/caddy.env
chown root:root /etc/caddy/caddy.env

# systemd de Caddy no lee caddy.env por defecto: se agrega con un override.
mkdir -p /etc/systemd/system/caddy.service.d
cat > /etc/systemd/system/caddy.service.d/override.conf <<'EOF'
[Service]
# Token del proveedor de DNS para el desafio ACME DNS-01.
EnvironmentFile=-/etc/caddy/caddy.env
EOF

mkdir -p /var/log/caddy
chown caddy:caddy /var/log/caddy
systemctl daemon-reload

# --- 4. Validar y arrancar ---------------------------------------------------
if ! caddy validate --config /etc/caddy/Caddyfile 2>&1 | tail -5; then
  die "el Caddyfile no es válido. Revisá el bloque tls y el nombre del dominio."
fi

systemctl enable caddy >/dev/null 2>&1 || true
systemctl restart caddy
sleep 3

if ! systemctl is-active --quiet caddy; then
  journalctl -u caddy -n 30 --no-pager
  die "Caddy no arrancó"
fi
log "Caddy activo"

cat <<EOF

--------------------------------------------------------------------------
Falta lo que depende de tu proveedor de DNS:

  1. Registro A:   $DOMAIN  ->  $LAN_IP
     Un registro público apuntando a una IP privada es correcto y habitual:
     solo resuelve, no expone nada.

  2. Token del proveedor para el desafío DNS-01:
         sudo nano /etc/caddy/caddy.env
         # CF_API_TOKEN=...        (permiso Zone:DNS:Edit sobre la zona)

  3. Plugin del proveedor (el Caddy de serie no lo trae):
         sudo caddy add-package github.com/caddy-dns/cloudflare
         sudo systemctl restart caddy

  4. Seguí la emisión del certificado:
         sudo journalctl -u caddy -f
     Buscá "certificate obtained successfully". Tarda 1-2 minutos.

Cuando termine, desde cualquier equipo del switch:

     https://$DOMAIN

  La contraseña de AUTH_PASSWORD es lo único que se necesita: nadie tiene que
  instalar Tailscale.

  Tailscale sigue funcionando en paralelo para acceso desde fuera.
--------------------------------------------------------------------------
EOF
