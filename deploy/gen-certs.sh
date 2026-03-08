#!/usr/bin/env bash
# gen-certs.sh — Generate self-signed CA and service certificates for Klaxxon
#
# Creates a CA, then issues certs for: comms (.10), api (.11), web (.12).
# All services trust the CA cert for mutual TLS.
#
# Run from the workstation. Copies certs to pve1 LXCs via setup-*.sh scripts.
#
# Usage: ./deploy/gen-certs.sh [output_dir]

set -euo pipefail

CERT_DIR="${1:-$(dirname "$0")/certs}"
DAYS=3650  # 10 years (internal-only, not public)
KEY_BITS=4096

# Subnet and hostnames
declare -A HOSTS=(
    [ca]="Klaxxon CA"
    [comms]="10.10.10.10"
    [api]="10.10.10.11"
    [web]="10.10.10.12"
)

mkdir -p "$CERT_DIR"
echo "Generating certificates in: $CERT_DIR"

# --- CA ---
if [ ! -f "$CERT_DIR/ca.key" ]; then
    echo "Creating CA..."
    openssl genrsa -out "$CERT_DIR/ca.key" "$KEY_BITS" 2>/dev/null
    openssl req -new -x509 -days "$DAYS" \
        -key "$CERT_DIR/ca.key" \
        -out "$CERT_DIR/ca.crt" \
        -subj "/CN=Klaxxon CA/O=Klaxxon/C=GB" 2>/dev/null
    echo "  CA created: $CERT_DIR/ca.crt"
else
    echo "  CA already exists, skipping."
fi

# --- Service certs ---
for svc in comms api web; do
    ip="${HOSTS[$svc]}"

    if [ -f "$CERT_DIR/${svc}.crt" ]; then
        echo "  $svc cert already exists, skipping."
        continue
    fi

    echo "Creating cert for $svc ($ip)..."

    # Generate key
    openssl genrsa -out "$CERT_DIR/${svc}.key" "$KEY_BITS" 2>/dev/null

    # Create CSR
    openssl req -new \
        -key "$CERT_DIR/${svc}.key" \
        -out "$CERT_DIR/${svc}.csr" \
        -subj "/CN=klaxxon-${svc}/O=Klaxxon/C=GB" 2>/dev/null

    # SAN extension (IP and hostname)
    cat > "$CERT_DIR/${svc}.ext" <<EOF
authorityKeyIdentifier=keyid,issuer
basicConstraints=CA:FALSE
keyUsage=digitalSignature, keyEncipherment
extendedKeyUsage=serverAuth, clientAuth
subjectAltName=@alt_names

[alt_names]
IP.1=${ip}
DNS.1=klaxxon-${svc}
DNS.2=klaxxon-${svc}.local
EOF

    # Sign with CA
    openssl x509 -req -days "$DAYS" \
        -in "$CERT_DIR/${svc}.csr" \
        -CA "$CERT_DIR/ca.crt" \
        -CAkey "$CERT_DIR/ca.key" \
        -CAcreateserial \
        -out "$CERT_DIR/${svc}.crt" \
        -extfile "$CERT_DIR/${svc}.ext" 2>/dev/null

    # Cleanup CSR and ext
    rm -f "$CERT_DIR/${svc}.csr" "$CERT_DIR/${svc}.ext"

    echo "  $svc cert created: $CERT_DIR/${svc}.crt"
done

# Summary
echo ""
echo "Certificates generated:"
ls -la "$CERT_DIR"/*.crt "$CERT_DIR"/*.key 2>/dev/null
echo ""
echo "CA cert (distribute to all services): $CERT_DIR/ca.crt"
echo "Keep ca.key secure. Do not deploy it to LXCs."
