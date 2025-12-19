#!/bin/bash
# Generate self-signed SSL certificate for development/testing
# For production, use Let's Encrypt (certbot) or a commercial CA

set -e

echo "==================================="
echo "SSL Certificate Generation Script"
echo "==================================="
echo ""
echo "This will generate a self-signed SSL certificate for development/testing."
echo "For production, use Let's Encrypt or a trusted Certificate Authority."
echo ""

# Create ssl directory if it doesn't exist
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SSL_DIR="$SCRIPT_DIR"

# Ask for domain name
read -p "Enter your domain name (e.g., localhost or your-domain.com): " DOMAIN
if [ -z "$DOMAIN" ]; then
    DOMAIN="localhost"
fi

echo ""
echo "Generating self-signed certificate for: $DOMAIN"
echo ""

# Generate private key
openssl genrsa -out "$SSL_DIR/key.pem" 2048

# Generate certificate signing request (CSR)
openssl req -new -key "$SSL_DIR/key.pem" -out "$SSL_DIR/csr.pem" \
    -subj "/C=CN/ST=State/L=City/O=Organization/CN=$DOMAIN"

# Generate self-signed certificate (valid for 365 days)
openssl x509 -req -days 365 -in "$SSL_DIR/csr.pem" \
    -signkey "$SSL_DIR/key.pem" -out "$SSL_DIR/cert.pem"

# Clean up CSR
rm "$SSL_DIR/csr.pem"

echo ""
echo "✓ SSL certificate generated successfully!"
echo ""
echo "Files created:"
echo "  - Private key: $SSL_DIR/key.pem"
echo "  - Certificate: $SSL_DIR/cert.pem"
echo ""
echo "Next steps:"
echo "1. Update nginx.conf to uncomment SSL configuration lines"
echo "2. Restart nginx or docker-compose"
echo ""
echo "For Let's Encrypt (production), run:"
echo "  sudo certbot --nginx -d your-domain.com"
echo ""
