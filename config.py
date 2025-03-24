import os
from dotenv import load_dotenv

load_dotenv()  # 加载.env文件中的环境变量

# API设置
API_KEY = os.getenv("API_KEY", "")  # API密钥，优先从环境变量读取
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")  # API私钥，优先从环境变量读取
PRIVATE_KEY_PASS = os.getenv("PRIVATE_KEY_PASS")  # 私钥密码

# API配置
BASE_URL = os.getenv("BASE_URL", "https://api1.binance.com")  # API地址，支持环境变量覆盖
USE_TESTNET = os.getenv("USE_TESTNET", "false").lower() == "true"  # 是否使用测试网络

# WebSocket API配置
WS_PING_INTERVAL = int(os.getenv("WS_PING_INTERVAL", "20"))  # WebSocket ping间隔（秒）
WS_TIMEOUT = int(os.getenv("WS_TIMEOUT", "10"))  # WebSocket请求超时（秒）
WS_AUTO_RECONNECT = os.getenv("WS_AUTO_RECONNECT", "true").lower() == "true"  # 自动重连
PREFER_WEBSOCKET = os.getenv("PREFER_WEBSOCKET", "true").lower() == "true"  # 优先使用WebSocket API

# 交易设置
SYMBOL = os.getenv("SYMBOL", "ACTUSDT")  # 交易对，支持环境变量覆盖
GRID_LEVELS = int(os.getenv("GRID_LEVELS", "5"))  # 网格数量
GRID_SPACING = float(os.getenv("GRID_SPACING", "1.5"))  # 网格间距（百分比）
CAPITAL_PER_LEVEL = float(os.getenv("CAPITAL_PER_LEVEL", "6"))  # 每个网格的资金（USDT）
GRID_RANGE_PERCENT = float(os.getenv("GRID_RANGE_PERCENT", "8"))  # 网格范围（百分比）
RECALCULATION_PERIOD = int(os.getenv("RECALCULATION_PERIOD", "2"))  # 重新计算网格的周期（天）
ATR_PERIOD = int(os.getenv("ATR_PERIOD", "14"))  # ATR计算周期

# 非对称网格参数（核心区域优化）- 配合grid_trader.py的优化
CORE_ZONE_PERCENTAGE = float(os.getenv("CORE_ZONE_PERCENTAGE", "0.5"))  # 核心区域占总范围的百分比
CORE_CAPITAL_RATIO = float(os.getenv("CORE_CAPITAL_RATIO", "0.7"))  # 核心区域占用资金比例
CORE_GRID_RATIO = float(os.getenv("CORE_GRID_RATIO", "0.6"))  # 核心区域的网格点比例

# 订单管理 - 配合_check_for_stale_orders方法
MAX_ORDER_AGE_HOURS = int(os.getenv("MAX_ORDER_AGE_HOURS", "24"))  # 订单最大存在时间（小时）
PRICE_DEVIATION_THRESHOLD = float(os.getenv("PRICE_DEVIATION_THRESHOLD", "0.03"))  # 价格偏离阈值
TRADING_FEE_RATE = float(os.getenv("TRADING_FEE_RATE", "0.075"))  # 交易手续费率（百分比）

# 风险管理设置
TRAILING_STOP_LOSS_PERCENT = float(os.getenv("TRAILING_STOP_LOSS_PERCENT", "4.5"))  # 追踪止损百分比
TRAILING_TAKE_PROFIT_PERCENT = float(os.getenv("TRAILING_TAKE_PROFIT_PERCENT", "1.5"))  # 追踪止盈百分比
RISK_UPDATE_THRESHOLD_PERCENT = float(os.getenv("RISK_UPDATE_THRESHOLD_PERCENT", "0.01"))  # 风险更新阈值
RISK_UPDATE_INTERVAL_MINUTES = float(os.getenv("RISK_UPDATE_INTERVAL_MINUTES", "10"))  # 风险更新间隔（分钟）

# Telegram设置
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")  # Telegram机器人token
ALLOWED_TELEGRAM_USERS = [int(user_id.strip()) for user_id in os.getenv("ALLOWED_TELEGRAM_USERS", "").split(",") if user_id.strip()]  # 授权用户ID列表
ENABLE_TELEGRAM = os.getenv("ENABLE_TELEGRAM", "true").lower() == "true"  # 是否启用Telegram通知