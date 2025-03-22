# grid-service

> Deployment and service management tools for ACTUSDT grid trading bot on AWS servers

This folder contains the minimal set of scripts and configuration files needed to deploy and maintain the grid trading bot as a resilient service on low-resource AWS instances like t4g.nano, t4g.micro or t2.micro.

## Contents

- `grid_bot.service` - Systemd service configuration with resource limits
- `watchdog.sh` - Process monitoring and automatic recovery script
- `network_monitor.sh` - Network connectivity monitoring for Binance API
- `cleanup.sh` - Log rotation and disk space management
- `setup.sh` - One-click deployment script

## Installation

1. Copy this folder to your AWS instance:

```bash
# From your local machine
scp -r -i your-key.pem grid-service ubuntu@your-aws-ip:~/
```

2. SSH into your AWS instance:

```bash
ssh -i your-key.pem ubuntu@your-aws-ip
```

3. Run the setup script:

```bash
cd ~/grid-service
chmod +x *.sh
./setup.sh
```

The setup script will:
- Install required dependencies
- Configure the systemd service
- Set up cron jobs for monitoring
- Start the trading bot service

## Monitoring & Maintenance

### Check Service Status

```bash
sudo systemctl status grid_bot.service
```

### View Live Logs

```bash
# Service logs
sudo journalctl -u grid_bot.service -f

# Application logs
tail -f ~/Binance/grid_trading_bot/grid_bot.log

# Monitoring logs
tail -f ~/Binance/watchdog.log
tail -f ~/Binance/network.log
```

### Restart Service

```bash
sudo systemctl restart grid_bot.service
```

### Update Configuration

After changing trading parameters in config.py:

```bash
sudo systemctl restart grid_bot.service
```

## System Requirements

- **OS**: Ubuntu Server 22.04 LTS or newer
- **CPU**: ARM64 (t4g series) or x86 (t2/t3 series)
- **RAM**: Minimum 512MB, recommended 1GB
- **Disk**: 8GB minimum (mostly for OS)
- **Network**: Stable internet connection

## Troubleshooting

### Bot Not Starting

Check logs for errors:
```bash
sudo journalctl -u grid_bot.service -n 100
```

Verify API keys:
```bash
grep -r "API_KEY" ~/Binance/grid_trading_bot/.env
ls -la ~/Binance/grid_trading_bot/key/
```

### Out of Memory Issues

Adjust memory limits in the service file:
```bash
sudo nano /etc/systemd/system/grid_bot.service
# Modify MemoryHigh and MemoryMax values
sudo systemctl daemon-reload
sudo systemctl restart grid_bot.service
```

### Network Connectivity Problems

Force a network connectivity check:
```bash
~/grid-service/network_monitor.sh
```

---

## Performance Notes

- The monitoring scripts are designed to be extremely lightweight
- Resource usage is carefully limited to ensure stability on nano/micro instances
- Log rotation prevents disk space issues during long-term operation

## Security

- Private keys are protected with proper file permissions
- The service runs under a non-root user
- Only essential ports are used (no incoming connections required)