This directory stores private key material (e.g., Ed25519 PEM files) required for Binance API access.

- Keys must **never** be committed to source control.
- The application reads key paths from environment/config (`PRIVATE_KEY`), keep them on the host only.
- Use strict file permissions (e.g., `chmod 600`) for private keys.

Placeholder files `.gitkeep` and this README keep the directory in version control without the secrets.
