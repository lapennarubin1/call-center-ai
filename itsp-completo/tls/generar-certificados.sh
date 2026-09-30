#!/usr/bin/env bash
# =====================================================================
#  MODULO 1: TLS + SRTP  — cifrado de señalización y de audio
#  Genera una CA propia + certificado de servidor para Asterisk y deja
#  listo el material para clientes. Ejecutar como root en el servidor.
#
#  Resultado en /etc/asterisk/keys/:
#     ca.crt          -> CA que firma todo (la instalas/confias en clientes)
#     asterisk.crt    -> certificado del servidor
#     asterisk.key    -> clave privada del servidor
# =====================================================================
set -euo pipefail

KEYDIR=/etc/asterisk/keys
# CAMBIA esto por el hostname/FQDN publico de TU servidor (debe resolver):
SERVER_CN="sip.tudominio.com"
DAYS=3650

mkdir -p "$KEYDIR"
cd "$KEYDIR"

# 1) CA propia
openssl genrsa -out ca.key 4096
openssl req -x509 -new -nodes -key ca.key -sha256 -days $DAYS \
  -subj "/CN=ITSP-CA/O=MiITSP" -out ca.crt

# 2) Certificado del servidor firmado por la CA
openssl genrsa -out asterisk.key 2048
openssl req -new -key asterisk.key \
  -subj "/CN=${SERVER_CN}/O=MiITSP" -out asterisk.csr
openssl x509 -req -in asterisk.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
  -sha256 -days $DAYS -out asterisk.crt

chown -R asterisk:asterisk "$KEYDIR"
chmod 600 "$KEYDIR"/*.key

echo "Certificados generados en $KEYDIR"
echo "CN del servidor: ${SERVER_CN} (asegurate que resuelve por DNS a tu IP publica)"
echo "Reparte ca.crt a los clientes para que confien en tu servidor."
