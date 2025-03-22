#!/bin/bash
LOG_FILE="/home/ubuntu/Binance/network.log"

# 检查Binance API连接性
if ! curl -s --max-time 10 --head https://api.binance.com | grep "200 OK" > /dev/null; then
    echo "$(date): Network connectivity issue detected" >> $LOG_FILE
    
    # 等待30秒后再次检查
    sleep 30
    
    # 二次确认连接问题
    if ! curl -s --max-time 10 --head https://api.binance.com | grep "200 OK" > /dev/null; then
        echo "$(date): Network issue persists, restarting bot" >> $LOG_FILE
        sudo systemctl restart grid_bot.service
    fi
fi