#!/usr/bin/env bash
# =====================================================================
#  FIREWALL nftables para servidor ITSP Asterisk
#  Filosofia: DENY por defecto. Solo se permite:
#    - SSH desde TUS IPs de administracion.
#    - SIP (5060/5061) SOLO desde IPs de proveedores y de clientes.
#    - RTP (10000-20000/udp) para media.
#  Ajusta las variables de abajo antes de ejecutar.
#  Ejecutar como root:  bash nftables-sip.sh && nft list ruleset
# =====================================================================
set -euo pipefail

# --- IPs de administracion SSH (pon las tuyas, separadas por espacio) ---
ADMIN_IPS="TU_IP_ADMIN_1 TU_IP_ADMIN_2"

# --- IPs / rangos de PROVEEDORES (senalizacion SIP entrante) ---
#   Telnyx, ProveedorB, ProveedorC ... verifica en cada portal.
PROVIDER_NETS="192.76.120.0/24 64.16.250.0/24 REEMPLAZA_IP_PROVEEDOR_B/32 REEMPLAZA_IP_PROVEEDOR_C/32"

# --- IPs / rangos de CLIENTES (call centers que se conectan a ti) ---
#   Si los clientes tienen IP fija, listalas. Si no, ver nota TLS en README.
CLIENT_NETS="REEMPLAZA_IP_CLIENTE_1/32 REEMPLAZA_IP_CLIENTE_2/32"

SIP_PORTS="5060,5061"
RTP_RANGE="10000-20000"

nft flush ruleset

nft -f - <<EOF
table inet filter {
  set admin_ips {
    type ipv4_addr
    flags interval
    elements = { $(echo $ADMIN_IPS | sed 's/ /, /g') }
  }
  set provider_nets {
    type ipv4_addr
    flags interval
    elements = { $(echo $PROVIDER_NETS | sed 's/ /, /g') }
  }
  set client_nets {
    type ipv4_addr
    flags interval
    elements = { $(echo $CLIENT_NETS | sed 's/ /, /g') }
  }

  chain input {
    type filter hook input priority 0; policy drop;

    # Trafico ya establecido / relacionado
    ct state established,related accept
    ct state invalid drop

    # Loopback
    iif "lo" accept

    # ICMP basico (util para diagnostico), con limite
    ip protocol icmp icmp type echo-request limit rate 5/second accept

    # SSH solo desde admin
    ip saddr @admin_ips tcp dport 22 accept

    # SIP solo desde proveedores y clientes
    ip saddr @provider_nets udp dport { $SIP_PORTS } accept
    ip saddr @provider_nets tcp dport { $SIP_PORTS } accept
    ip saddr @client_nets   udp dport { $SIP_PORTS } accept
    ip saddr @client_nets   tcp dport { $SIP_PORTS } accept

    # RTP (media). Idealmente restringido a proveedores+clientes,
    # pero la media puede venir de IPs distintas; se abre el rango.
    udp dport $RTP_RANGE accept

    # Todo lo demas: registrar y descartar
    limit rate 5/minute log prefix "nft-drop: "
    drop
  }

  chain forward { type filter hook forward priority 0; policy drop; }
  chain output  { type filter hook output  priority 0; policy accept; }
}
EOF

echo "Reglas nftables aplicadas. Revisa con: nft list ruleset"
echo "Para persistir en Debian/Ubuntu: nft list ruleset > /etc/nftables.conf && systemctl enable nftables"
