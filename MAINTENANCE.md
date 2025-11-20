# AWS Grid Trading Bot 维护指南

本指南记录了在本地环境（如 WSL2）下，针对 AWS EC2 服务器的日常更新、监控和维护流程。

## 1. 环境准备

为了方便后续命令执行，避免重复输入 IP 和密钥路径，请先在本地终端中设置以下环境变量。

**注意：请将以下变量替换为您实际的服务器 IP 和密钥路径。**

```bash
# === 必需变量 (请复制到本地终端并修改后执行) ===
export SERVER_IP="x.x.x.x"                      # 您的 AWS 服务器公网 IP
export KEY_PEM="/path/to/your/private_key.pem"  # 您的 SSH 登录私钥路径
export REMOTE_DIR="/opt/grid_trading_bot"       # 服务器上的部署目录

# 验证连接 (可选)
# ssh -i "$KEY_PEM" ubuntu@$SERVER_IP "echo Connection success"
````

-----

## 2\. 代码更新流程 (标准 CI/CD)

当您在本地修改了策略代码（如 config.py 或 core/ 逻辑）后，请按以下步骤发布：

### 第一步：推送到 GitHub

在项目根目录执行：

```bash
git add .
git commit -m "更新描述: 修改了网格参数或优化了策略"
git push origin main
```

### 第二步：一键触发远程更新

此命令会让服务器自动拉取最新代码、更新依赖并重启机器人。

```bash
ssh -i "$KEY_PEM" ubuntu@$SERVER_IP "bash $REMOTE_DIR/deploy_aws.sh"
```

### 第三步：确认运行状态

查看实时日志，确认机器人已成功重启并加载了新配置。

```bash
ssh -i "$KEY_PEM" ubuntu@$SERVER_IP "sudo journalctl -u gridbot -f"
```

(按 Ctrl + C 退出日志查看)

-----

## 3\. 修改敏感配置 (.env)

注意：.env 文件被 .gitignore 忽略，不会随 Git 同步。如果您修改了 API Key 或其他环境变量，必须手动上传。

```bash
# 1. 上传新的 .env 文件
scp -i "$KEY_PEM" .env ubuntu@$SERVER_IP:$REMOTE_DIR/

# 2. 重启服务以应用更改
ssh -i "$KEY_PEM" ubuntu@$SERVER_IP "sudo systemctl restart gridbot"
```

-----

## 4\. 监控与日志

### 查看实时控制台日志 (Systemd)

```bash
ssh -i "$KEY_PEM" ubuntu@$SERVER_IP "sudo journalctl -u gridbot -f"
```

### 查看详细应用日志文件

```bash
ssh -i "$KEY_PEM" ubuntu@$SERVER_IP "tail -n 50 $REMOTE_DIR/grid_bot.log"
```

### 检查服务运行状态

```bash
ssh -i "$KEY_PEM" ubuntu@$SERVER_IP "sudo systemctl status gridbot"
```

-----

## 5\. 启停控制 (紧急操作)

### 停止机器人 (暂停交易)

```bash
ssh -i "$KEY_PEM" ubuntu@$SERVER_IP "sudo systemctl stop gridbot"
```

### 启动机器人

```bash
ssh -i "$KEY_PEM" ubuntu@$SERVER_IP "sudo systemctl start gridbot"
```

### 重启机器人

```bash
ssh -i "$KEY_PEM" ubuntu@$SERVER_IP "sudo systemctl restart gridbot"
```

-----

## 6\. 故障排查备忘

  * **Git 拉取失败 (Permission denied):**

      * 检查服务器上的 SSH Key 是否有效：

    <!-- end list -->

    ```bash
    ssh -i "$KEY_PEM" ubuntu@$SERVER_IP "ssh -T git@github.com"
    ```

  * **WSL2 连接失败 (Unprotected private key):**

      * 确保密钥权限正确（仅所有者可读）：

    <!-- end list -->

    ```bash
    chmod 400 /path/to/your/private_key.pem
    ```

  * **部署脚本报错 (Directory not empty):**

      * 这是 Git 初始化问题，通常只在首次部署时出现。如果再次发生，需登录服务器手动修复 Git 状态。

<!-- end list -->