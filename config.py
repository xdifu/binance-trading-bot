import os
from dotenv import load_dotenv

load_dotenv()

# API设置
API_KEY = os.getenv("API_KEY", "")
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
PRIVATE_KEY_PASS = os.getenv("PRIVATE_KEY_PASS")

# API配置
BASE_URL = "https://api1.binance.com"
USE_TESTNET = os.getenv("USE_TESTNET", "false").lower() == "true"

# WebSocket API配置
WS_PING_INTERVAL = int(os.getenv("WS_PING_INTERVAL", "20"))  # WebSocket ping间隔（秒）
WS_TIMEOUT = int(os.getenv("WS_TIMEOUT", "10"))  # WebSocket请求超时（秒）
WS_AUTO_RECONNECT = os.getenv("WS_AUTO_RECONNECT", "true").lower() == "true"  # 自动重连
PREFER_WEBSOCKET = os.getenv("PREFER_WEBSOCKET", "true").lower() == "true"  # 优先使用WebSocket API

# 交易设置
SYMBOL = "ACTUSDT"
GRID_LEVELS = 8  # 网格数量
GRID_SPACING = 1.0  # 网格间距（百分比）
CAPITAL_PER_LEVEL = 6  # 每个网格的资金（USDT）
GRID_RANGE_PERCENT = 2.5  # 网格范围（百分比）
RECALCULATION_PERIOD = 7  # 重新计算网格的周期（天）
ATR_PERIOD = 14  # ATR计算周期

# 风险管理设置
TRAILING_STOP_LOSS_PERCENT = 4.5  # 追踪止损百分比
TRAILING_TAKE_PROFIT_PERCENT = 1.5  # 追踪止盈百分比

# Telegram设置
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
# 添加此行，将字符串转换为整数列表
ALLOWED_TELEGRAM_USERS = [int(user_id.strip()) for user_id in os.getenv("ALLOWED_TELEGRAM_USERS", "").split(",") if user_id.strip()]
ENABLE_TELEGRAM = True