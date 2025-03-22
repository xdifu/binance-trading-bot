#!/bin/bash
LOG_FILE="/home/ubuntu/Binance/watchdog.log"

# 检查进程是否运行
if ! pgrep -f "python /home/ubuntu/Binance/grid_trading_bot/main.py" > /dev/null; then
    echo "$(date): Bot process not found, restarting service" >> $LOG_FILE
    sudo systemctl restart grid_bot.service
else
    # 检查内存使用（超过85%时重启）
    MEM_USAGE=$(ps -o pid,pmem -C python | grep $(pgrep -f "python /home/ubuntu/Binance/grid_trading_bot/main.py") | awk '{print $2}')
    if (( $(echo "$MEM_USAGE > 85" | bc -l) )); then
        echo "$(date): High memory usage: $MEM_USAGE%, restarting bot" >> $LOG_FILE
        sudo systemctl restart grid_bot.service
    fi
fi