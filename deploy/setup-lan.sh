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
# shellcheck source=deploy/dns-providers.sh
source "$APP_DIR/deploy/dns-providers.sh"

DOMAIN="${1:-}"
PROVIDER="${2:-}"
EMAIL="${ACME_EMAIL:-admin@${DOMAIN#*.}}"

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "corré esto con sudo"

if [[ -z "$DOMAIN" ]]; then
  cat >&2 <<'MSG'
uso: sudo ./deploy/setup-lan.sh <dominio> [proveedor-dns]

  sudo ./deploy/setup-lan.sh listener.institucion.cr cloudflare

Si no sabés el proveedor, averigualo primero (no cambia nada del sistema):

  ./deploy/detect-dns.sh institucion.cr

Sin proveedor, el script deja el bloque tls para completar a mano.
MSG
  exit 1
fi

if [[ -n "$PROVIDER" ]] && ! provider_known "$PROVIDER"; then
  echo "proveedor no reconocido: $PROVIDER" >&2
  echo "Soportados:" >&2
  list_providers >&2
  echo >&2
  echo "Si el tuyo tiene plugin en https://github.com/caddy-dns pero no está en" >&2
  echo "la lista, agregalo a deploy/dns-providers.sh o editá el Caddyfile a mano." >&2
  exit 1
fi

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
  if [[ -n "$PROVIDER" ]]; then
    DNS_DIRECTIVE="$(provider_field "$PROVIDER" 2)"
    ENVVARS="$(provider_field "$PROVIDER" 3)"
  else
    DNS_DIRECTIVE="# COMPLETAR: dns <proveedor> {env.TOKEN}  -- ver dns-providers.sh"
    ENVVARS=""
    warn "sin proveedor: hay que completar el bloque tls a mano"
  fi

  log "escribiendo /etc/caddy/Caddyfile para $DOMAIN"
  sed -e "s|@DOMAIN@|$DOMAIN|g" \
      -e "s|@EMAIL@|$EMAIL|g" \
      -e "s|@DNS_DIRECTIVE@|$DNS_DIRECTIVE|g" \
      "$APP_DIR/deploy/Caddyfile.example" > /etc/caddy/Caddyfile
else
  log "el Caddyfile ya menciona $DOMAIN, no se sobrescribe"
  ENVVARS="$([[ -n "$PROVIDER" ]] && provider_field "$PROVIDER" 3 || echo "")"
fi

# --- 3b. Plugin del proveedor ------------------------------------------------
if [[ -n "$PROVIDER" ]]; then
  PACKAGE="$(provider_field "$PROVIDER" 1)"
  # `caddy list-modules` muestra los plugins compilados en el binario actual.
  if caddy list-modules 2>/dev/null | grep -q "dns.providers.$PROVIDER"; then
    log "el plugin de $PROVIDER ya está en el binario de Caddy"
  else
    log "instalando el plugin $PACKAGE (descarga un binario nuevo de Caddy)"
    if caddy add-package "$PACKAGE"; then
      log "plugin instalado"
    else
      warn "no se pudo instalar el plugin automáticamente. A mano:"
      warn "    sudo caddy add-package $PACKAGE"
      warn "    sudo systemctl restart caddy"
    fi
  fi
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

echo
echo "--------------------------------------------------------------------------"
echo "Falta lo que depende de tu proveedor de DNS:"
echo
echo "  1. Registro A:   $DOMAIN  ->  $LAN_IP"
echo "     Un registro público apuntando a una IP privada es correcto y habitual:"
echo "     solo resuelve, no expone nada."
echo
if [[ -n "${ENVVARS:-}" ]]; then
  echo "  2. Token de $PROVIDER en /etc/caddy/caddy.env (ya está en chmod 600):"
  echo "         sudo nano /etc/caddy/caddy.env"
  for v in $ENVVARS; do
    echo "         $v=..."
  done
  echo "     El token necesita permiso para EDITAR los registros de la zona."
  echo "     Después:  sudo systemctl restart caddy"
else
  echo "  2. Completá el bloque tls de /etc/caddy/Caddyfile con tu proveedor."
  echo "     Para saber cuál es:   ./deploy/detect-dns.sh $DOMAIN"
fi
echo
echo "  3. Seguí la emisión del certificado:"
echo "         sudo journalctl -u caddy -f"
echo "     Buscá 'certificate obtained successfully'. Tarda 1-2 minutos."
echo
echo "Cuando termine, desde cualquier equipo del switch:"
echo
echo "     https://$DOMAIN"
echo
echo "  La contraseña de AUTH_PASSWORD es lo único que se necesita: nadie tiene"
echo "  que instalar Tailscale."
echo
echo "  Tailscale sigue funcionando en paralelo para el acceso remoto. Esto NO"
echo "  publica nada en internet: el nombre resuelve a una IP privada."
echo "--------------------------------------------------------------------------"
