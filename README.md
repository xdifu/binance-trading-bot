# Binance Grid Trading Bot

## Overview

This repository contains a production-ready grid trading system for Binance spot markets. The bot combines a volatility-aware grid engine, strict risk management, and full operational automation. It is written in Python and targets experienced crypto teams that need deterministic behaviour, clear observability, and fast iteration.

## Key Capabilities

- **Volatility-aware grids** – ATR-driven spacing, asymmetric level placement, and capital-weighted core zones keep the grid profitable in both ranging and trending regimes.
- **Resilient execution** – Seamless WebSocket/REST failover, strict balance locking, and automatic stale-order recovery prevent silent failures.
- **Institutional risk controls** – True trailing stop-loss/ take-profit managed via OCO orders, balance-aware throttling, and Telegram alerts on every protective action.
- **Operations toolkit** – Built-in diagnostics, configurable watchdog threads, simulation mode, and complete test coverage for critical flows.

## Architecture

```
├── main.py                 # Lifecycle orchestration, WebSocket routing, maintenance threads
├── core/
│   ├── grid_trader.py      # Grid calculus, order lifecycle, capital management
│   └── risk_manager.py     # OCO orchestration, trailing logic, emergency exits
├── binance_api/            # REST + WebSocket clients, market-data streams
├── tg_bot/                 # Telegram command and alert interface
├── utils/                  # Precision helpers and indicator calculations
├── diagnostics.py          # Comprehensive connectivity/time-sync checker
├── tools/                  # Operational/diagnostic scripts (time sync, health checks)
└── tests/                  # Pytest suites for trader and risk manager behaviour
```

- Market data streams go through `binance_api/websocket_manager.py`, which normalises payloads and feeds `main.py`.
- Orders are submitted via `binance_api/client.py`, which shares authentication and timestamp logic between REST and WebSocket APIs.
- `core/grid_trader.py` owns capital allocation, order reconciliation, and balance locking; `core/risk_manager.py` owns the protective OCO lifecycle.

## Prerequisites

- Python 3.10+ (the bot relies on modern typing and `msgspec`)
- A Binance account with either Ed25519 API keys (recommended) or classic HMAC keys
- Telegram bot token and user IDs if you need remote control/alerts
- Linux environment with stable clock sync (Chrony or systemd-timesyncd)

## Installation

```bash
git clone https://github.com/your-org/binance-grid-trading-bot.git
cd binance-grid-trading-bot
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### Credentials

1. Copy `.env.example` to `.env` (or create a new file) and provide:
   - `API_KEY`
   - **Either** `PRIVATE_KEY` (path to PEM file for Ed25519 requests) **or** `API_SECRET` for classic HMAC signing.  
     If neither is set the bot can still read public market data, but all trading/account endpoints are disabled.
   - `PRIVATE_KEY_PASS` if the key is encrypted (use `None` for no passphrase)
   - Optional `TELEGRAM_TOKEN` and `ALLOWED_TELEGRAM_USERS`
2. Store private keys under `key/` with the correct permissions.

## Configuration

All trading/risk parameters live in `config.py`. Important sections:

- **Trading pair & capital**: `SYMBOL`, `CAPITAL_PER_LEVEL`, `CAPITAL_SIZE`
- **Grid definition**: `GRID_LEVELS`, `GRID_SPACING`, `GRID_RANGE_PERCENT`, `RECALCULATION_PERIOD`
- **Volatility inputs**: `ATR_PERIOD`, `ATR_RATIO`, asymmetric core ratios
- **Risk controls**: `TRAILING_STOP_LOSS_PERCENT`, `TRAILING_TAKE_PROFIT_PERCENT`, `RISK_UPDATE_*`
  (values are percentages; e.g. `0.5` means 0.5%)
- **Protective mode** (auto-on for non-major symbols by default): `ENABLE_PROTECTIVE_MODE`, `AUTO_PROTECTIVE_FOR_NON_MAJOR`, `MAJOR_SYMBOLS`,
  `MIN_EXPECTED_PROFIT_BUFFER`, `MAX_CENTER_DEVIATION`, `PROTECTIVE_PAUSE_STRONG_TREND`, `PROTECTIVE_TREND_LEVEL_REDUCTION`
- **Order hygiene**: `MAX_ORDER_AGE_HOURS`, `PRICE_DEVIATION_THRESHOLD`, `MIN_NOTIONAL_VALUE`
- **Telemetry**: `LOG_LEVEL`, `ENABLE_TELEGRAM`, `DATA_DIR`

Call `config.validate_config()` during deployments to ensure parameters remain sane.

## Usage

```bash
source venv/bin/activate
python main.py
```

Startup sequence:

1. Initialise REST/WebSocket clients and sync server time.
2. Fetch exchange metadata and build ATR-informed grid levels.
3. Assess balances, lock capital per level, and place initial orders.
4. Start watchdog threads for grid recalculation, stale-order sweeps, and user-data keep-alives.
5. Launch optional Telegram bot for `/status`, `/startgrid`, `/stopgrid`, `/risk`, and `/setsymbol`.

To run in dry mode, call `grid_trader.start(simulation=True)` via a custom script or REPL.

## Monitoring & Control

- **Logs**: `grid_bot.log` (info level) plus console output during development.
- **Telegram**: Real-time fills, grid adjustments, risk alerts, and manual commands.
- **Diagnostics**: `diagnostics.py` validates API connectivity, symbol filters, ATR calculation, and OCO readiness.
- **WebSocket resilience**: Automatic reconnect with exponential backoff; REST fallbacks on every critical path.

## Testing

Run the unit test suite (after installing dev dependencies if needed):

```bash
pytest
```

`tests/test_grid_trader.py`, `tests/test_grid_levels.py`, and `tests/test_risk_manager.py` focus on order recycling, OCO placement, and capital restoration logic. Extend these when adding new behaviour.

## Deployment (AWS/systemd)

- Use the idempotent helper at `tools/deploy/deploy_aws.sh` to provision/update on an EC2 host (creates venv, installs deps, writes `.env`, and configures a systemd service).
- Store secrets in AWS SSM Parameter Store/Secrets Manager (e.g., `/gridbot/API_KEY`, `/gridbot/API_SECRET`, `/gridbot/TELEGRAM_TOKEN`, `/gridbot/ALLOWED_TELEGRAM_USERS`) and give the instance role `ssm:GetParameter` (with decryption). Run with `USE_SSM=true`.
- Example manual run on EC2:
  ```bash
  sudo bash tools/deploy/deploy_aws.sh \
    APP_DIR=/opt/grid_trading_bot \
    REPO_URL=https://github.com/your-org/binance-grid-trading-bot.git \
    BRANCH=main \
    USE_SSM=true \
    SSM_PREFIX=/gridbot \
    RUN_USER=ubuntu
  ```
- For CI-driven auto-updates, trigger the same script via SSH or SSM (e.g., GitHub Actions calling `aws ssm send-command ...`) after each push/tag. The script is safe to rerun; it pulls latest code, updates deps, and restarts the systemd service.

## Operational Notes

- Ensure the system clock stays within ±500 ms of Binance; `diagnosis_time_sync.py` can be used for NTP audits.
- When switching symbols via Telegram or config, the bot automatically cancels existing orders, refreshes precision filters, and rebalances capital.
- Always validate new parameters on the Binance testnet before pointing to production keys.
- Review local regulations and exchange terms before deploying automated trading strategies.
