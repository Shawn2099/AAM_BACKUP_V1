# Phase 1: Dashboard Authentication ✅

**Status:** Complete

**Goal:** Secure the FastAPI dashboard behind API key authentication.

## Changes Made

### `models/config.py`
- Added `DashboardConfig` (auth_enabled, api_key, bind_address, port)
- Added `dashboard` field to `AppConfig`

### `config.yaml`
- Added `dashboard:` section with auth_enabled: true, bind_address: 127.0.0.1

### `ui.py`
- Added session store (secrets.token_hex, 24h TTL, in-memory dict)
- Added login page at GET /login
- Added login handler at POST /login (hmac.compare_digest)
- Added logout at GET /logout
- Added _require_auth(request) middleware
- Added X-API-Key header support for programmatic clients
- All endpoints protected (/ , /status, /trigger/cloud, /trigger/lan)
- Updated run() → cfg.dashboard.bind_address + cfg.dashboard.port

## Verification
- Syntax check: pass
- Import check: pass
