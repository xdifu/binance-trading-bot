#!/bin/bash
# 清理旧日志文件（只保留最近7天）
find /home/ubuntu/Binance/binance-trading-bot -name "*.log" -type f -mtime +7 -delete

# 检查磁盘空间
DISK_USAGE=$(df / | tail -1 | awk '{print $5}' | sed 's/%//')
if [ $DISK_USAGE -gt 85 ]; then
    # 磁盘使用率超过85%时，执行紧急清理
    find /home/ubuntu/Binance/binance-trading-bot -name "*.log" -type f -mtime +1 -delete
    find /tmp -type f -mtime +1 -delete
    
    # 写入日志
    echo "$(date): Emergency disk cleanup performed, usage was at ${DISK_USAGE}%" >> /home/ubuntu/Binance/system.log
fi