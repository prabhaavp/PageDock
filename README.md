# PageDock

A self-hosted web app for scanning, editing, and printing documents. Includes a PWA, undo history, print previews, ink levels, printer status, and queue management. Uses eSCL, HP LEDM, and CUPS.

## Getting started

1. Copy `.env.example` to `.env` and configure your printer, driver, and credentials. Generate a session secret with `openssl rand -hex 32`.
2. Find your printer's driver ID:
   ```bash
   docker run --rm --entrypoint sh ghcr.io/prabhaavp/pagedock:latest -c 'cupsd && lpinfo -m'
   ```
3. Start with `docker compose up --build -d`.
4. Open http://localhost:8080 and sign in.

Scan and edit pages, undo changes, export PDFs or images, and preview print jobs. Supports PDF, JPEG, PNG, and text printing. Previews may differ from physical output.

Documents, settings, undo history, and CUPS data persist in one Docker volume at `/data`. Files are retained for 30 days by default (configurable from 1–365 days). Keep `CUPS_QUEUE` unchanged across updates. Undo history is configurable with `UNDO_DEPTH` (1–10, default 3).

## Docker Deployment

GitHub Actions tests, builds, and publishes multi-architecture images to `ghcr.io/prabhaavp/pagedock` on pushes to `main`, version tags, and a weekly rebuild.

## Security

Localhost access is the default. For remote access, use HTTPS behind an authenticated reverse proxy with MFA or a private VPN. Never expose the HTTP port directly.

For HTTPS, set `PUBLIC_ORIGIN` to your public URL, enable `SECURE_COOKIE=1`, and use a strong password (20+ characters). Preserve the `Host` header through your proxy.

Keep your printer on a trusted network, protect `.env` and `/data`, and back up your volume.

## Install as an app

Use your browser's **Install app** option, or **Add to Home Screen**. Devices may require trusted HTTPS.