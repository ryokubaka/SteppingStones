#!/bin/bash

# Generate self-signed SSL certificates for localhost development
# This script creates certificates suitable for development environments

CERT_DIR="./certs"
CERT_FILE="$CERT_DIR/steppingstones.cer"
KEY_FILE="$CERT_DIR/steppingstones.key"

# Create certs directory if it doesn't exist
mkdir -p "$CERT_DIR"

# Check if certificates already exist
if [ -f "$CERT_FILE" ] && [ -f "$KEY_FILE" ]; then
    echo "Certificates already exist in $CERT_DIR"
    echo "Certificate: $CERT_FILE"
    echo "Private Key: $KEY_FILE"
    exit 0
fi

echo "Generating self-signed SSL certificates for localhost..."

# Generate private key
openssl genrsa -out "$KEY_FILE" 2048

# Generate certificate signing request and self-signed certificate
openssl req -new -x509 -key "$KEY_FILE" -out "$CERT_FILE" -days 365 -subj "/C=US/ST=Development/L=Local/O=SteppingStones/OU=Development/CN=localhost"

# Set appropriate permissions
chmod 644 "$CERT_FILE"
chmod 600 "$KEY_FILE"

echo "Certificates generated successfully!"
echo "Certificate: $CERT_FILE"
echo "Private Key: $KEY_FILE"
echo ""
echo "Note: These are self-signed certificates for development only."
echo "For production, use proper SSL certificates from a trusted CA." 