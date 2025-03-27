下面是一个完整的更新流程命令序列：

```bash
# 1. 停止服务
sudo systemctl stop binance-bot

# 2. 更新代码
cd /home/ubuntu/Binance/binance-trading-bot
git fetch origin
git checkout hft-marketmaking
git pull origin hft-marketmaking

# 3. 更新依赖（如需要）
source /home/ubuntu/Binance/venv/bin/activate
pip install -r requirements.txt  # 如果存在requirements.txt
deactivate

# 4. 确保watchdog脚本有执行权限
chmod +x /home/ubuntu/Binance/watchdog.py

# 5. 重启服务
sudo systemctl start binance-bot

# 6. 检查状态
sudo systemctl status binance-bot
tail -f /home/ubuntu/Binance/watchdog.log
tail -f /home/ubuntu/Binance/bot_output.log
```