#!/usr/bin/env bash
# gen-certs.sh — local CA + server certificate for the web UI over HTTPS (needed for microphone
# access and PWA install from a laptop/phone; browsers allow those only on secure origins).
# Idempotent: keeps an existing CA, re-issues the server cert when the SAN list changed.
# Files (root, 0600 keys): /etc/support/tls/{ca.crt,ca.key,server.crt,server.key}
# The CA certificate is served unauthenticated at http://<gx10>:8080/ca.crt for operators to install.
set -euo pipefail
TLS=${TLS_DIR:-/etc/support/tls}
HN=$(hostname)
# SANs: every name/address an operator may type. Hotspot and direct-cable addresses are fixed.
DNS="localhost $HN $HN.local"
IPS="127.0.0.1 10.42.0.1 10.77.0.1 $(hostname -I 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9.]+$' | tr '\n' ' ')"
SAN=$(for d in $DNS; do printf 'DNS:%s,' "$d"; done; for i in $IPS; do printf 'IP:%s,' "$i"; done); SAN=${SAN%,}

sudo install -d -m 755 "$TLS"
if ! sudo test -s "$TLS/ca.key"; then
  echo "  creating local CA (valid 10 years)"
  sudo openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 3650 \
    -keyout "$TLS/ca.key" -out "$TLS/ca.crt" -subj "/CN=Workbench $HN local CA" \
    -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" 2>/dev/null
  sudo chmod 600 "$TLS/ca.key"; sudo chmod 644 "$TLS/ca.crt"
fi
if sudo test -s "$TLS/server.crt" && [ "$(sudo cat "$TLS/server.san" 2>/dev/null)" = "$SAN" ]; then
  echo "  server certificate up to date ($SAN)"; exit 0
fi
echo "  issuing server certificate for: $SAN"
tmp=$(mktemp -d)
openssl req -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -keyout "$tmp/server.key" -out "$tmp/server.csr" -subj "/CN=$HN" 2>/dev/null
printf 'subjectAltName=%s\nextendedKeyUsage=serverAuth\nbasicConstraints=CA:FALSE\n' "$SAN" > "$tmp/ext"
sudo openssl x509 -req -in "$tmp/server.csr" -CA "$TLS/ca.crt" -CAkey "$TLS/ca.key" -CAcreateserial \
  -out "$tmp/server.crt" -days 825 -extfile "$tmp/ext" 2>/dev/null
sudo install -m 600 "$tmp/server.key" "$TLS/server.key"
sudo install -m 644 "$tmp/server.crt" "$TLS/server.crt"
echo "$SAN" | sudo tee "$TLS/server.san" >/dev/null
rm -rf "$tmp"
# the UI runs as the operator user; let it read the server key
sudo chown "$(id -un)" "$TLS/server.key"
