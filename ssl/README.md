# SSL/TLS Certificate Setup

This directory contains SSL/TLS certificates for HTTPS support.

## Quick Start

### Option 1: Self-Signed Certificate (Development/Testing)

```bash
# Generate self-signed certificate
chmod +x generate-ssl-cert.sh
./generate-ssl-cert.sh
```

This will create:
- `key.pem` - Private key
- `cert.pem` - Self-signed certificate

**⚠️ Warning**: Self-signed certificates will show browser warnings. Only use for development/testing.

### Option 2: Let's Encrypt (Production - Recommended)

For production environments, use Let's Encrypt with certbot:

```bash
# Install certbot
sudo apt-get update
sudo apt-get install certbot python3-certbot-nginx

# Generate certificate (replace your-domain.com)
sudo certbot --nginx -d your-domain.com -d www.your-domain.com

# Auto-renewal is configured by default
# Test renewal:
sudo certbot renew --dry-run
```

Certbot will automatically:
- Generate certificates
- Update nginx configuration
- Set up auto-renewal

### Option 3: Commercial Certificate Authority

If you have certificates from a commercial CA:

1. Place your certificate files here:
   - `cert.pem` - Your SSL certificate
   - `key.pem` - Your private key
   - `chain.pem` - Certificate chain (if provided)

2. Update nginx configuration to use these files

## Enabling HTTPS

After obtaining certificates:

1. **For Docker deployment**: Uncomment SSL lines in `nginx.conf`:
   ```nginx
   listen 443 ssl http2;
   ssl_certificate /etc/nginx/ssl/cert.pem;
   ssl_certificate_key /etc/nginx/ssl/key.pem;
   ```

2. **For Vite dev server**: Uncomment HTTPS section in `vite.config.mjs`:
   ```javascript
   https: {
     key: fs.readFileSync('path/to/key.pem'),
     cert: fs.readFileSync('path/to/cert.pem'),
   }
   ```

3. **Optional**: Enable HTTP to HTTPS redirect by uncommenting the redirect server block in nginx.conf

4. Restart services:
   ```bash
   # Docker
   docker-compose restart web
   
   # Or rebuild
   docker-compose up -d --build web
   ```

## Security Best Practices

1. **Never commit private keys to version control**
   - Add `*.pem` to `.gitignore`
   - Use environment-specific certificate management

2. **Use strong TLS versions**
   - TLSv1.2 and TLSv1.3 only
   - Disable SSLv3, TLSv1, TLSv1.1

3. **Use strong cipher suites**
   - Modern browsers support strong ciphers by default
   - Example: `HIGH:!aNULL:!MD5`

4. **Enable HSTS** (HTTP Strict Transport Security)
   - `add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;`

5. **Keep certificates up to date**
   - Let's Encrypt certificates expire after 90 days
   - Certbot auto-renewal handles this automatically

## Troubleshooting

### Certificate Not Found Error
- Ensure files are in `/Users/shuyonglin/code/quantFinance/ssl/`
- Check file permissions: `chmod 600 key.pem`
- Verify docker-compose mounts: `./ssl:/etc/nginx/ssl:ro`

### Browser Certificate Warning
- Self-signed certificates will show warnings
- For production, use Let's Encrypt or commercial CA
- For testing, you can add exception in browser

### Port 443 Already in Use
```bash
# Check what's using port 443
sudo lsof -i :443

# Stop conflicting service or change nginx port
```

## Certificate Renewal

### Let's Encrypt Auto-Renewal
Certbot sets up auto-renewal via systemd timer:
```bash
# Check renewal status
sudo systemctl status certbot.timer

# Manual renewal test
sudo certbot renew --dry-run
```

### Manual Renewal
If auto-renewal fails:
```bash
sudo certbot renew
docker-compose restart web
```

## Files in This Directory

- `generate-ssl-cert.sh` - Script to generate self-signed certificates
- `README.md` - This file
- `cert.pem` - SSL certificate (not in version control)
- `key.pem` - Private key (not in version control)
- `.gitignore` - Excludes certificates from git

## Additional Resources

- [Let's Encrypt Documentation](https://letsencrypt.org/docs/)
- [Mozilla SSL Configuration Generator](https://ssl-config.mozilla.org/)
- [SSL Labs Server Test](https://www.ssllabs.com/ssltest/)
