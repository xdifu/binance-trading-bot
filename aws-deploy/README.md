# ACTUSDT 网格交易系统 AWS 部署文档

本文档提供在 AWS Ubuntu Server 上部署和维护 ACTUSDT 网格交易系统的必要步骤。

## 1. 文件概述

这个部署框架包含5个关键文件:

- **grid_bot.service**: systemd服务配置，确保交易机器人开机自启动和崩溃后自动重启
- **watchdog.sh**: 进程监控脚本，检查机器人是否正常运行
- **network_monitor.sh**: 网络连接监控脚本，检测与Binance API的连接状态
- **cleanup.sh**: 系统资源管理脚本，防止日志过大占满磁盘
- **setup.sh**: 一键式部署脚本，自动配置整个环境

## 2. 部署步骤

### 2.1 在AWS上创建实例

1. 登录AWS控制台，启动EC2实例
   - 推荐配置: t4g.micro (2GB RAM, ARM架构)或t2.micro
   - 操作系统: Ubuntu Server 22.04 LTS
   - 存储: 8GB SSD
   - 安全组设置: 仅开放SSH端口(22)

2. 连接到实例
   ```bash
   ssh -i your-key.pem ubuntu@your-instance-ip
   ```

### 2.2 设置交易机器人

1. 安装Git和克隆代码库
   ```bash
   sudo apt update
   sudo apt install -y git
   git clone https://your-repository-url.git ~/Binance
   ```

2. 准备部署脚本
   ```bash
   mkdir -p ~/grid-service
   cp ~/Binance/grid_bot.service ~/grid-service/
   cp ~/Binance/watchdog.sh ~/grid-service/
   cp ~/Binance/network_monitor.sh ~/grid-service/
   cp ~/Binance/cleanup.sh ~/grid-service/
   cp ~/Binance/setup.sh ~/grid-service/
   
   chmod +x ~/grid-service/*.sh
   ```

3. 运行安装脚本
   ```bash
   cd ~/grid-service
   ./setup.sh
   ```

## 3. 监控与维护

### 3.1 检查服务状态
```bash
sudo systemctl status grid_bot.service
```

### 3.2 查看日志
```bash
# 查看服务日志
sudo journalctl -u grid_bot.service -f

# 查看机器人应用日志
tail -f ~/Binance/grid_trading_bot/grid_bot.log

# 查看监控脚本日志
tail -f ~/Binance/watchdog.log
tail -f ~/Binance/network.log
```

### 3.3 手动重启服务
```bash
sudo systemctl restart grid_bot.service
```

## 4. 常见问题解决

### 4.1 服务无法启动
检查配置文件和API密钥是否正确:
```bash
grep -r "API_KEY" ~/Binance/grid_trading_bot/.env
ls -la ~/Binance/grid_trading_bot/key/
```

### 4.2 内存使用过高
调整grid_bot.service中的内存限制:
```bash
sudo nano /etc/systemd/system/grid_bot.service
# 修改 MemoryMax=900M 为更低的值，如 MemoryMax=700M
sudo systemctl daemon-reload
sudo systemctl restart grid_bot.service
```

### 4.3 更新机器人配置
如果需要更新交易参数:
```bash
# 编辑配置文件
nano ~/Binance/grid_trading_bot/config.py

# 重启服务
sudo systemctl restart grid_bot.service
```

### 4.4 失去网络连接后恢复
系统已配置自动恢复，但如需手动操作:
```bash
# 运行网络监控脚本
~/grid-service/network_monitor.sh

# 重启网络服务
sudo systemctl restart systemd-networkd
```

## 5. 安全提示

- 定期更新系统: `sudo apt update && sudo apt upgrade -y`
- 监控SSH登录尝试: `sudo lastb`
- 保持API密钥安全: 确保密钥文件权限设置正确 `chmod 600 ~/Binance/grid_trading_bot/key/*`

## 6. 系统资源要求

- **CPU**: 低负载，t4g.nano/micro足够应对
- **内存**: 至少512MB，推荐1GB以上
- **磁盘**: 至少8GB，主要用于系统和日志
- **网络**: 稳定连接，带宽要求低

---

最后更新日期: 2025-03-23