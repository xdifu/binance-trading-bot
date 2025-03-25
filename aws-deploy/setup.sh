#!/bin/bash
echo "Installing Grid Trading Bot..."

# 安装系统依赖
sudo apt update
sudo apt install -y python3-pip python3-venv bc curl git

# 创建目录结构
mkdir -p ~/Binance/scripts

# 复制脚本到相应位置
cp watchdog.sh ~/Binance/scripts/
cp network_monitor.sh ~/Binance/scripts/
cp cleanup.sh ~/Binance/scripts/
chmod +x ~/Binance/scripts/*.sh

# 创建Python虚拟环境
python3 -m venv ~/Binance/venv
source ~/Binance/venv/bin/activate
pip install --no-cache-dir -r ~/Binance/binance-trading-bot/requirements.txt

# 设置systemd服务
sudo cp grid_bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable grid_bot.service

# 设置cron任务
(crontab -l 2>/dev/null; echo "*/3 * * * * ~/Binance/scripts/watchdog.sh") | crontab -
(crontab -l 2>/dev/null; echo "*/10 * * * * ~/Binance/scripts/network_monitor.sh") | crontab -
(crontab -l 2>/dev/null; echo "0 2 * * * ~/Binance/scripts/cleanup.sh") | crontab -

# 启动服务
sudo systemctl start grid_bot.service

echo "Installation complete! Bot is now running."
echo "Check status with: sudo systemctl status grid_bot.service"