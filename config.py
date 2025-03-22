import os
from dotenv import load_dotenv

load_dotenv()

# API设置
API_KEY = os.getenv("API_KEY", "")
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
PRIVATE_KEY_PASS = os.getenv("PRIVATE_KEY_PASS")

# 其他配置
BASE_URL = "https://api.binance.com"
USE_TESTNET = os.getenv("USE_TESTNET", "false").lower() == "true"

# 交易设置
SYMBOL = "BTCUSDT"
GRID_LEVELS = 8  # 网格数量
GRID_SPACING = 0.5  # 网格间距（百分比）
CAPITAL_PER_LEVEL = 5  # 每个网格的资金（USDT）
GRID_RANGE_PERCENT = 4  # 网格范围（百分比）
RECALCULATION_PERIOD = 7  # 重新计算网格的周期（天）
ATR_PERIOD = 14  # ATR计算周期

# 风险管理设置
TRAILING_STOP_LOSS_PERCENT = 5  # 追踪止损百分比
TRAILING_TAKE_PROFIT_PERCENT = 2  # 追踪止盈百分比

# Telegram设置
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
# 添加此行，将字符串转换为整数列表
ALLOWED_TELEGRAM_USERS = [int(user_id.strip()) for user_id in os.getenv("ALLOWED_TELEGRAM_USERS", "").split(",") if user_id.strip()]
ENABLE_TELEGRAM = True