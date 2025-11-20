One-click AWS deployment helper
================================

What it does
------------
- Installs Python/git on the target host (apt-based).
- Clones/updates the repo, creates/updates a venv, installs `requirements.txt`.
- Generates `.env` (from AWS SSM if enabled, otherwise a placeholder).
- Creates/updates a systemd unit to run `main.py` from the venv.
- Enables and starts the service (idempotent; safe to rerun).

Script location
---------------
- `tools/deploy/deploy_aws.sh`

How to run (manual)
-------------------
```bash
sudo bash tools/deploy/deploy_aws.sh \
  APP_DIR=/opt/grid_trading_bot \
  REPO_URL=https://github.com/your-org/binance-grid-trading-bot.git \
  BRANCH=main \
  USE_SSM=true \
  SSM_PREFIX=/gridbot \
  RUN_USER=ubuntu
```
- If `USE_SSM=true`, the script fetches `API_KEY`, `API_SECRET`, `TELEGRAM_TOKEN` (optional), `ALLOWED_TELEGRAM_USERS` from Parameter Store (requires the instance IAM role).
- If `USE_SSM=false`, the script writes a placeholder `.env`; fill it before running in production.

How to run (CI-triggered)
-------------------------
- Trigger via SSH: run `sudo bash /opt/grid_trading_bot/tools/deploy/deploy_aws.sh ...` on the host.
- Trigger via SSM: `aws ssm send-command --document-name AWS-RunShellScript --parameters commands=["sudo bash /opt/grid_trading_bot/tools/deploy/deploy_aws.sh"] --targets Key=InstanceIds,Values=<your-instance-id>`.

Notes / Best practices
----------------------
- Keep secrets out of git; use SSM/Secrets Manager.
- Use a dedicated service user with least privilege; adjust `RUN_USER`.
- Verify system time sync and security group egress to Binance/Telegram.
- Logs: `journalctl -u gridbot -f` (default service name `gridbot`).
