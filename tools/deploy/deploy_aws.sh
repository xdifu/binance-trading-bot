#!/usr/bin/env bash
# One-click/idempotent deployment script for grid_trading_bot on AWS EC2.
# Performs: package install (apt), code fetch/update, venv setup, dependency install,
# optional SSM secrets fetch, .env generation, systemd unit creation, and service restart.
# This script is safe to re-run; state is converged on every execution.

set -euo pipefail

# ==== Configurable defaults (override with env vars when calling) ====
APP_DIR="${APP_DIR:-/opt/grid_trading_bot}"              # Target install path on server
REPO_URL="${REPO_URL:-git@github.com:xdifu/binance-trading-bot.git}"  # Public repo URL
BRANCH="${BRANCH:-main}"                                # Branch or tag to deploy
RUN_USER="${RUN_USER:-${SUDO_USER:-$(whoami)}}"         # Service user
PYTHON_BIN="${PYTHON_BIN:-python3}"                     # Python interpreter
USE_SSM="${USE_SSM:-false}"                             # true to pull secrets from AWS SSM
SSM_PREFIX="${SSM_PREFIX:-/gridbot}"                    # Prefix for SSM parameters
ENV_FILE="${ENV_FILE:-$APP_DIR/.env}"                   # .env destination
VENV_DIR="${VENV_DIR:-$APP_DIR/venv}"                   # venv path
SERVICE_NAME="${SERVICE_NAME:-gridbot}"                 # systemd service name

# ==== Helpers ====
log() { printf '[deploy] %s\n' "$*"; }

install_packages() {
  if command -v apt >/dev/null 2>&1; then
    sudo apt update
    sudo apt install -y git "$PYTHON_BIN" "${PYTHON_BIN}-venv" awscli >/dev/null 2>&1 || true
  fi
}

ensure_app_dir() {
  sudo mkdir -p "$APP_DIR"
  sudo chown "$RUN_USER":"$RUN_USER" "$APP_DIR"
}

fetch_code() {
  cd "$APP_DIR"
  if [ ! -d .git ]; then
    sudo -u "$RUN_USER" git clone "$REPO_URL" "$APP_DIR"
  fi
  sudo -u "$RUN_USER" git -C "$APP_DIR" fetch --all --prune
  sudo -u "$RUN_USER" git -C "$APP_DIR" checkout "$BRANCH"
  sudo -u "$RUN_USER" git -C "$APP_DIR" pull --ff-only origin "$BRANCH"
}

setup_venv() {
  if [ ! -d "$VENV_DIR" ]; then
    sudo -u "$RUN_USER" "$PYTHON_BIN" -m venv "$VENV_DIR"
  fi
  sudo -u "$RUN_USER" "$VENV_DIR/bin/pip" install --upgrade pip
  sudo -u "$RUN_USER" "$VENV_DIR/bin/pip" install -r "$APP_DIR/requirements.txt"
}

write_env_from_ssm() {
  API_KEY=$(aws ssm get-parameter --with-decryption --name "$SSM_PREFIX/API_KEY" --query Parameter.Value --output text)
  API_SECRET=$(aws ssm get-parameter --with-decryption --name "$SSM_PREFIX/API_SECRET" --query Parameter.Value --output text)
  TELEGRAM_TOKEN=$(aws ssm get-parameter --with-decryption --name "$SSM_PREFIX/TELEGRAM_TOKEN" --query Parameter.Value --output text || echo "")
  ALLOWED_USERS=$(aws ssm get-parameter --name "$SSM_PREFIX/ALLOWED_TELEGRAM_USERS" --query Parameter.Value --output text || echo "")
  cat > "$ENV_FILE" <<EOF
API_KEY=$API_KEY
API_SECRET=$API_SECRET
PRIVATE_KEY=
PRIVATE_KEY_PASS=
TELEGRAM_TOKEN=$TELEGRAM_TOKEN
ALLOWED_TELEGRAM_USERS=$ALLOWED_USERS
EOF
}

write_env_placeholder() {
  # Preserve existing secrets if the file already exists.
  if [ -f "$ENV_FILE" ]; then
    log ".env file already exists. Skipping placeholder generation to preserve secrets."
    return
  fi

  cat > "$ENV_FILE" <<'EOF'
# Fill these before running in production or enable SSM via USE_SSM=true
API_KEY=
API_SECRET=
PRIVATE_KEY=
PRIVATE_KEY_PASS=
TELEGRAM_TOKEN=
ALLOWED_TELEGRAM_USERS=
EOF
}

render_systemd_unit() {
  local exec_cmd="$VENV_DIR/bin/python $APP_DIR/main.py"
  sudo tee "/etc/systemd/system/${SERVICE_NAME}.service" >/dev/null <<EOF
[Unit]
Description=Binance Grid Trading Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${exec_cmd}
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
}

reload_and_start() {
  sudo systemctl daemon-reload
  sudo systemctl enable --now "${SERVICE_NAME}.service"
  sudo systemctl status "${SERVICE_NAME}.service" --no-pager
}

# ==== Main flow ====
install_packages
ensure_app_dir
fetch_code
setup_venv

if $USE_SSM; then
  write_env_from_ssm
else
  write_env_placeholder
fi
sudo chmod 600 "$ENV_FILE"
sudo chown "$RUN_USER":"$RUN_USER" "$ENV_FILE"

render_systemd_unit
reload_and_start

log "Deployment finished. Tail logs with: sudo journalctl -u ${SERVICE_NAME} -f"
