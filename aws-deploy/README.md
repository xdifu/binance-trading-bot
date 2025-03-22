# aws-deploy

> Deployment tools for running the grid trading bot on low-resource AWS instances

This folder contains the essential scripts to deploy the grid trading bot as a systemd service on AWS Ubuntu servers, with automatic recovery mechanisms for network failures and process crashes.

## Quick Start

Deploy the trading bot to your AWS instance with a single command:

```bash
cd ~/Binance/grid_trading_bot/aws-deploy
chmod +x *.sh
./setup.sh
```

## Features

- **Fault Tolerance**: Automatic recovery from network outages and process crashes
- **Resource Efficient**: Optimized for t4g.nano/micro and t2.micro instances
- **Self-monitoring**: Watchdog scripts ensure continuous operation
- **Zero Maintenance**: Auto-cleans logs and manages system resources

## Monitoring & Control

### Service Status

```bash
# Check if the bot is running
sudo systemctl status grid_bot.service
```

### View Logs

```bash
# Service logs
sudo journalctl -u grid_bot.service -f

# Application logs
tail -f ~/Binance/grid_trading_bot/grid_bot.log

# Monitor logs
tail -f ~/Binance/watchdog.log
tail -f ~/Binance/network.log
```

### Control Commands

```bash
# Stop the trading bot
sudo systemctl stop grid_bot.service

# Start the trading bot
sudo systemctl start grid_bot.service 

# Restart the trading bot (after config changes)
sudo systemctl restart grid_bot.service
```

## Troubleshooting

### Bot Fails to Start

Check logs for specific errors:
```bash
sudo journalctl -u grid_bot.service -n 50
```

Common issues:
- API key permissions: Ensure API keys have correct trading permissions
- Network connectivity: Run network_monitor.sh to test
- Python dependencies: Try reinstalling with `pip install -r requirements.txt`

### High Memory Usage

Adjust memory limits in the service configuration:
```bash
sudo nano /etc/systemd/system/grid_bot.service
# Modify MemoryHigh=768M and MemoryMax=900M values
sudo systemctl daemon-reload
sudo systemctl restart grid_bot.service
```

### Network Connectivity Issues

Force a connectivity check:
```bash
~/Binance/scripts/network_monitor.sh
```

## Technical Details

The deployment consists of:

- grid_bot.service: Systemd service definition with auto-restart and resource limits
- watchdog.sh: Process monitoring script (runs every 3 minutes via cron)
- network_monitor.sh: Binance API connectivity checker (runs every 10 minutes)
- `cleanup.sh`: Log rotation and disk space management (runs daily)
- setup.sh: One-click deployment script

## System Requirements

- **OS**: Ubuntu Server 20.04+ LTS
- **CPU**: ARM64 or x86 (single core is sufficient)
- **RAM**: 512MB minimum, 1GB recommended
- **Disk**: 8GB minimum (primarily for OS)
- **Network**: Stable internet connection