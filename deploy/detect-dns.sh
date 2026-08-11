#!/usr/bin/env bash
#
# Averigua quien gestiona el DNS de un dominio y que hace falta para el
# certificado.
#
#   ./deploy/detect-dns.sh institucion.cr
#
# No cambia nada en el sistema: solo consulta y explica. No necesita sudo
# (salvo para instalar dnsutils si falta dig).

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/dns-providers.sh
source "$HERE/dns-providers.sh"

DOMAIN="${1:-}"

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }

[[ -n "$DOMAIN" ]] || die "uso: ./deploy/detect-dns.sh institucion.cr"

# Los registros NS viven en la zona registrable, no en el subdominio.
ZONE="$(zone_of "$DOMAIN")"

if ! command -v dig >/dev/null 2>&1; then
  warn "falta 'dig'. Instalalo con:  sudo apt install -y dnsutils"
  warn "Mientras, se intenta con los comandos disponibles."
fi

echo
log "dominio consultado: $DOMAIN"
log "zona donde viven los NS: $ZONE"
echo

# --- Registros NS ------------------------------------------------------------
NS_TEXT=""
if command -v dig >/dev/null 2>&1; then
  NS_TEXT="$(dig +short NS "$ZONE" @1.1.1.1 2>/dev/null)"
  [[ -z "$NS_TEXT" ]] && NS_TEXT="$(dig +short NS "$ZONE" 2>/dev/null)"
elif command -v nslookup >/dev/null 2>&1; then
  NS_TEXT="$(nslookup -type=NS "$ZONE" 2>/dev/null | grep -i 'nameserver' | awk '{print $NF}')"
fi

if [[ -z "$NS_TEXT" ]]; then
  warn "no se pudieron obtener los registros NS de $ZONE."
  warn "Puede ser que el dominio no exista, o que este servidor no resuelva"
  warn "hacia afuera. Probalo desde otra maquina:   dig NS $ZONE"
else
  echo "Servidores de nombres de $ZONE:"
  sed 's/^/  /' <<< "$NS_TEXT"
  echo
fi

# --- Adivinar el proveedor ---------------------------------------------------
PROVIDER=""
if [[ -n "$NS_TEXT" ]]; then
  PROVIDER="$(guess_provider_from_ns "$NS_TEXT" || true)"
fi

if [[ -z "$PROVIDER" ]]; then
  warn "no se reconoció el proveedor a partir de los NS."
  echo
  echo "Mirá los nombres de arriba: normalmente dicen quién es. Después buscá si"
  echo "hay plugin en  https://github.com/caddy-dns  y usalo con setup-lan.sh."
  echo
  echo "Proveedores ya soportados por estos scripts:"
  list_providers
  echo
  echo "Ojo con una confusión habitual: el REGISTRADOR donde compraste el dominio"
  echo "no siempre es quien gestiona el DNS. Lo que importa es lo que dicen los NS."
  exit 0
fi

PACKAGE="$(provider_field "$PROVIDER" 1)"
ENVVARS="$(provider_field "$PROVIDER" 3)"

log "proveedor detectado: $PROVIDER"
echo
echo "Para emitir el certificado por DNS-01 hacen falta tres cosas:"
echo
echo "  1. Un token de API de $PROVIDER con permiso para EDITAR los registros"
echo "     de la zona $ZONE. Variables que espera Caddy:"
for v in $ENVVARS; do
  echo "         $v"
done
echo
echo "  2. El plugin, porque el Caddy de apt no lo trae:"
echo "         sudo caddy add-package $PACKAGE"
echo
echo "  3. El registro A del subdominio apuntando a la IP interna del servidor:"
LAN_IP="$(ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -1)"
echo "         ${DOMAIN}   A   ${LAN_IP:-<IP-del-servidor>}"
echo
echo "Con eso, todo lo demás lo arma setup-lan.sh:"
echo
echo "         sudo ./deploy/setup-lan.sh $DOMAIN $PROVIDER"
echo
