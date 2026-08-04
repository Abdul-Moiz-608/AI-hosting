# AI Hosting Platform — Phase 1: Server Onboarding

This first service registers a VPS **without collecting its password**, generates a dedicated SSH deploy key, creates a short-lived one-time bootstrap token, and gathers basic machine facts after the server runs the bootstrap command.

## Run locally

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# Put that value in VAULT_MASTER_KEY in .env.
# Set PUBLIC_API_BASE_URL to an externally reachable HTTPS domain before onboarding a VPS.
# Bind to all interfaces when the API is reached from another machine.
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Open http://localhost:8000 locally. For a DigitalOcean VPS, configure a public DNS name
or HTTPS tunnel/reverse proxy and set `PUBLIC_API_BASE_URL` to that URL. Also allow inbound
traffic to port 8000 (or, preferably, terminate TLS at a reverse proxy) and add the dashboard
origin to `CORS_ORIGINS` when it is hosted on a different origin.

## Pinggy tunnels for deployed apps

The reusable `app.tunnels.PinggyTunnel` provider starts a managed SSH tunnel with
`ssh -p 443 -R0:localhost:<PORT> free.pinggy.io`. Pass the detected application
port to it only after the application's local health check succeeds. The provider
captures SSH output, extracts Pinggy's generated HTTPS URL, checks
`<public-url>/api/health`, and raises `TunnelError` if the URL or health check
fails. Use `TunnelManager.start_for(deployment_id, tunnel)` and call
`TunnelManager.stop_for(deployment_id)` when that deployment stops, is cancelled,
or is replaced.

## Production packages / services to add later

- PostgreSQL + `psycopg[binary]` and Alembic rather than SQLite.
- A cloud KMS (AWS KMS, Google Cloud KMS, Azure Key Vault, or HashiCorp Vault) for per-server envelope encryption.
- Redis with Celery or RQ for deploy jobs and retries.
- Authentication/organizations, audit logs, OpenTelemetry, and Sentry.

Never commit `.env`, reveal private keys in a browser, or expose this development server directly to the Internet.
