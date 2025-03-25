# 在服务器上更新代码的完整维护指南

本指南详细说明了如何在服务器上安全地更新交易机器人代码，包括正确处理watchdog监控脚本和systemd服务。

## 1. 更新前的准备工作

在拉取新代码前，**必须先停止systemd服务**以防止冲突和潜在的交易问题。

```bash
# 查看当前服务状态
sudo systemctl status binance-bot

# 停止服务（这将同时停止watchdog和交易机器人）
sudo systemctl stop binance-bot
```

## 2. 代码拉取流程

确认服务已停止后，安全拉取最新代码：

```bash
# 进入项目目录
cd /home/ubuntu/Binance/binance-trading-bot

# 确保没有本地修改可能造成冲突
git status

# 拉取最新代码（指定分支）
git fetch origin
git checkout fix-from-fe7ff69
git pull origin fix-from-fe7ff69

# 检查拉取结果
git log -1 --oneline
```

## 3. 依赖更新（如需要）

如果更新包含新的依赖项，需要更新虚拟环境：

```bash
# 激活虚拟环境
source /home/ubuntu/Binance/venv/bin/activate

# 更新依赖
pip install -r requirements.txt

# 退出虚拟环境
deactivate
```

## 4. 检查和更新配置文件

检查是否需要更新配置文件（如`.env`）：

```bash
# 查看配置文件是否有更新
cat .env

# 如需编辑
nano .env
```

## 5. 重新启动服务

代码更新完成后，重启systemd服务：

```bash
# 重新启动服务
sudo systemctl start binance-bot

# 检查服务状态
sudo systemctl status binance-bot
```

## 6. 验证正常运行

```bash
# 检查watchdog日志
tail -f /home/ubuntu/Binance/watchdog.log

# 检查交易机器人日志
tail -f /home/ubuntu/Binance/bot_output.log
```

## 7. 特殊情况处理

### 当代码变更包含watchdog脚本修改

如果更新了watchdog.py脚本本身：

```bash
# 确保服务已停止
sudo systemctl stop binance-bot

# 确保脚本有执行权限
chmod +x /home/ubuntu/Binance/watchdog.py

# 重启服务
sudo systemctl start binance-bot
```

### 当systemd服务配置需要更新

如果需要更新systemd服务配置：

```bash
# 编辑服务配置文件
sudo nano /etc/systemd/system/binance-bot.service

# 重新加载systemd配置
sudo systemctl daemon-reload

# 重启服务
sudo systemctl restart binance-bot
```

## 8. 回滚流程（出现问题时）

如果新代码导致问题，执行回滚操作：

```bash
# 停止有问题的服务
sudo systemctl stop binance-bot

# 切换回之前的稳定版本
cd /home/ubuntu/Binance/binance-trading-bot
git checkout 之前的稳定commit或分支

# 重启服务
sudo systemctl start binance-bot
```

## 9. 完整操作流程示例

下面是一个完整的更新流程命令序列：

```bash
# 1. 停止服务
sudo systemctl stop binance-bot

# 2. 更新代码
cd /home/ubuntu/Binance/binance-trading-bot
git fetch origin
git checkout fix-from-fe7ff69
git pull origin fix-from-fe7ff69

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

## 10. 注意事项

- **核心原则**：先停止服务，再更新代码，最后重启服务
- **避免直接修改**：不要在服务器上直接编辑代码文件，应在本地WSL中修改后提交到Git
- **保留备份**：重大更新前考虑备份关键配置文件
- **监控更新后的行为**：更新后至少观察10-15分钟，确保系统正常运行
- **API密钥安全**：确保更新过程中不会意外暴露或修改API密钥文件

按照此指南操作，可以确保代码更新过程中交易机器人能够安全停止，并在更新后正确重启和运行。