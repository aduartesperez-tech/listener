#!/usr/bin/env bash
#
# Tabla de proveedores de DNS para el desafio ACME DNS-01 de Caddy.
#
# El binario de Caddy que se instala por apt NO trae ningun plugin de DNS: hay
# que agregarlo con `caddy add-package`, que descarga un binario nuevo con el
# plugin compilado dentro.
#
# Este archivo se incluye (source) desde setup-lan.sh y detect-dns.sh.
# No hace nada por si mismo.

# provider|paquete de caddy|directiva tls|variables de entorno necesarias
DNS_PROVIDERS="
cloudflare|github.com/caddy-dns/cloudflare|dns cloudflare {env.CF_API_TOKEN}|CF_API_TOKEN
route53|github.com/caddy-dns/route53|dns route53|AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY
digitalocean|github.com/caddy-dns/digitalocean|dns digitalocean {env.DO_AUTH_TOKEN}|DO_AUTH_TOKEN
googleclouddns|github.com/caddy-dns/googleclouddns|dns googleclouddns {env.GCE_PROJECT}|GCE_PROJECT GOOGLE_APPLICATION_CREDENTIALS
azure|github.com/caddy-dns/azure|dns azure|AZURE_TENANT_ID AZURE_CLIENT_ID AZURE_CLIENT_SECRET AZURE_SUBSCRIPTION_ID AZURE_RESOURCE_GROUP_NAME
namecheap|github.com/caddy-dns/namecheap|dns namecheap {env.NAMECHEAP_API_KEY} {env.NAMECHEAP_API_USER}|NAMECHEAP_API_KEY NAMECHEAP_API_USER
godaddy|github.com/caddy-dns/godaddy|dns godaddy {env.GODADDY_API_TOKEN}|GODADDY_API_TOKEN
gandi|github.com/caddy-dns/gandi|dns gandi {env.GANDI_BEARER_TOKEN}|GANDI_BEARER_TOKEN
hetzner|github.com/caddy-dns/hetzner|dns hetzner {env.HETZNER_API_TOKEN}|HETZNER_API_TOKEN
linode|github.com/caddy-dns/linode|dns linode {env.LINODE_API_TOKEN}|LINODE_API_TOKEN
vultr|github.com/caddy-dns/vultr|dns vultr {env.VULTR_API_TOKEN}|VULTR_API_TOKEN
ovh|github.com/caddy-dns/ovh|dns ovh {env.OVH_APPLICATION_KEY} {env.OVH_APPLICATION_SECRET} {env.OVH_CONSUMER_KEY}|OVH_APPLICATION_KEY OVH_APPLICATION_SECRET OVH_CONSUMER_KEY
porkbun|github.com/caddy-dns/porkbun|dns porkbun {env.PORKBUN_API_KEY} {env.PORKBUN_API_SECRET_KEY}|PORKBUN_API_KEY PORKBUN_API_SECRET_KEY
desec|github.com/caddy-dns/desec|dns desec {env.DESEC_TOKEN}|DESEC_TOKEN
dnsimple|github.com/caddy-dns/dnsimple|dns dnsimple {env.DNSIMPLE_API_TOKEN}|DNSIMPLE_API_TOKEN
netcup|github.com/caddy-dns/netcup|dns netcup {env.NETCUP_CUSTOMER_NUMBER} {env.NETCUP_API_KEY} {env.NETCUP_API_PASSWORD}|NETCUP_CUSTOMER_NUMBER NETCUP_API_KEY NETCUP_API_PASSWORD
powerdns|github.com/caddy-dns/powerdns|dns powerdns {env.PDNS_SERVER_URL} {env.PDNS_API_TOKEN}|PDNS_SERVER_URL PDNS_API_TOKEN
rfc2136|github.com/caddy-dns/rfc2136|dns rfc2136 {env.RFC2136_SERVER} {env.RFC2136_KEY_NAME} {env.RFC2136_KEY_ALG} {env.RFC2136_KEY} |RFC2136_SERVER RFC2136_KEY_NAME RFC2136_KEY_ALG RFC2136_KEY
"

# Patrones de los registros NS -> nombre del proveedor.
# Sirve para adivinar quien gestiona la zona sin preguntar.
NS_PATTERNS="
cloudflare.com|cloudflare
awsdns|route53
digitalocean.com|digitalocean
googledomains.com|googleclouddns
google.com|googleclouddns
azure-dns|azure
registrar-servers.com|namecheap
domaincontrol.com|godaddy
gandi.net|gandi
hetzner|hetzner
linode.com|linode
vultr.com|vultr
ovh.net|ovh
porkbun.com|porkbun
desec.io|desec
dnsimple.com|dnsimple
netcup.net|netcup
nsone.net|nsone
dnsmadeeasy.com|dnsmadeeasy
"

# TLDs de segundo nivel frecuentes: en estos, la zona registrable tiene tres
# etiquetas (mined.go.cr) y no dos.
SECOND_LEVEL_TLDS="co ac go or ed com net org gob gov edu mil"

zone_of() {
  # zone_of listener.mined.go.cr -> mined.go.cr
  # Los registros NS viven en la zona registrable, no en el subdominio.
  local domain="$1" labels sld_pattern
  labels="$(awk -F. '{print NF}' <<< "$domain")"
  if (( labels <= 2 )); then
    echo "$domain"
    return
  fi
  sld_pattern="$(tr ' ' '|' <<< "$SECOND_LEVEL_TLDS")"
  if [[ "$domain" =~ \.($sld_pattern)\.[a-z]{2,3}$ ]]; then
    awk -F. '{print $(NF-2)"."$(NF-1)"."$NF}' <<< "$domain"
  else
    awk -F. '{print $(NF-1)"."$NF}' <<< "$domain"
  fi
}

provider_field() {
  # provider_field <provider> <1=paquete|2=directiva|3=variables>
  local wanted="$1" field="$2" line
  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    if [[ "${line%%|*}" == "$wanted" ]]; then
      echo "$line" | cut -d'|' -f$((field + 1))
      return 0
    fi
  done <<< "$DNS_PROVIDERS"
  return 1
}

provider_known() { provider_field "$1" 1 >/dev/null 2>&1; }

list_providers() {
  local line
  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    printf '  %s\n' "${line%%|*}"
  done <<< "$DNS_PROVIDERS"
}

guess_provider_from_ns() {
  # guess_provider_from_ns <texto con los registros NS>
  local ns_text="$1" line pattern provider
  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    pattern="${line%%|*}"
    provider="${line##*|}"
    if grep -qi -- "$pattern" <<< "$ns_text"; then
      echo "$provider"
      return 0
    fi
  done <<< "$NS_PATTERNS"
  return 1
}
