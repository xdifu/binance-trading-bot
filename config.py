import os
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()

# API设置
API_KEY = os.getenv("API_KEY", "")
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
PRIVATE_KEY_PASS = os.getenv("PRIVATE_KEY_PASS")

# API配置
BASE_URL = os.getenv("BASE_URL", "https://api1.binance.com")  # 允许通过环境变量覆盖
USE_TESTNET = os.getenv("USE_TESTNET", "false").lower() == "true"  # 是否使用测试网络

# WebSocket API配置
PREFER_WEBSOCKET = os.getenv("PREFER_WEBSOCKET", "true").lower() == "true"  # 优先使用WebSocket API
WS_PING_INTERVAL = int(os.getenv("WS_PING_INTERVAL", "20"))  # WebSocket ping间隔（秒）
WS_TIMEOUT = int(os.getenv("WS_TIMEOUT", "10"))  # WebSocket请求超时（秒）
WS_AUTO_RECONNECT = os.getenv("WS_AUTO_RECONNECT", "true").lower() == "true"  # 自动重连
WS_RECONNECT_MAX_ATTEMPTS = int(os.getenv("WS_RECONNECT_MAX_ATTEMPTS", "5"))  # 最大重连尝试次数
WS_RECONNECT_DELAY = int(os.getenv("WS_RECONNECT_DELAY", "5"))  # 重连延迟（秒）

# 交易设置
SYMBOL = os.getenv("SYMBOL", "ACTUSDT")  # 交易对
GRID_LEVELS = int(os.getenv("GRID_LEVELS", "5"))  # 网格数量
GRID_SPACING = float(os.getenv("GRID_SPACING", "1.5"))  # 网格间距（百分比）
CAPITAL_PER_LEVEL = float(os.getenv("CAPITAL_PER_LEVEL", "6"))  # 每个网格的资金（USDT）
GRID_RANGE_PERCENT = float(os.getenv("GRID_RANGE_PERCENT", "8"))  # 网格范围（百分比）
RECALCULATION_PERIOD = int(os.getenv("RECALCULATION_PERIOD", "2"))  # 重新计算网格的周期（天）
ATR_PERIOD = int(os.getenv("ATR_PERIOD", "14"))  # ATR计算周期

# 非对称网格参数（核心区域优化）
CORE_ZONE_PERCENTAGE = float(os.getenv("CORE_ZONE_PERCENTAGE", "0.5"))  # 核心区域占总范围的百分比
CORE_CAPITAL_RATIO = float(os.getenv("CORE_CAPITAL_RATIO", "0.7"))  # 核心区域占用资金比例
CORE_GRID_RATIO = float(os.getenv("CORE_GRID_RATIO", "0.6"))  # 核心区域的网格点比例

# 订单管理
MAX_ORDER_AGE_HOURS = int(os.getenv("MAX_ORDER_AGE_HOURS", "24"))  # 订单最大存在时间（小时）
PRICE_DEVIATION_THRESHOLD = float(os.getenv("PRICE_DEVIATION_THRESHOLD", "0.03"))  # 价格偏离阈值，用于判断订单是否过时
PROFIT_MARGIN_MULTIPLIER = float(os.getenv("PROFIT_MARGIN_MULTIPLIER", "2.0"))  # 盈利与手续费的倍数要求
BUY_SELL_SPREAD = float(os.getenv("BUY_SELL_SPREAD", "1.5"))  # 买卖差价百分比

# 风险管理设置
TRAILING_STOP_LOSS_PERCENT = float(os.getenv("TRAILING_STOP_LOSS_PERCENT", "4.5"))  # 追踪止损百分比
TRAILING_TAKE_PROFIT_PERCENT = float(os.getenv("TRAILING_TAKE_PROFIT_PERCENT", "1.5"))  # 追踪止盈百分比
RISK_UPDATE_THRESHOLD_PERCENT = float(os.getenv("RISK_UPDATE_THRESHOLD_PERCENT", "0.01"))  # 风险更新阈值（价格变化百分比）
RISK_UPDATE_INTERVAL_MINUTES = float(os.getenv("RISK_UPDATE_INTERVAL_MINUTES", "10"))  # 风险更新间隔（分钟）

# 高级交易设置
TRADING_FEE_RATE = float(os.getenv("TRADING_FEE_RATE", "0.075"))  # 交易手续费率（百分比，使用BNB支付）
MIN_NOTIONAL_VALUE = float(os.getenv("MIN_NOTIONAL_VALUE", CAPITAL_PER_LEVEL))  # 最小订单价值，默认等于每级资金

# Telegram设置
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
ALLOWED_TELEGRAM_USERS = [int(user_id.strip()) for user_id in os.getenv("ALLOWED_TELEGRAM_USERS", "").split(",") if user_id.strip()]
ENABLE_TELEGRAM = os.getenv("ENABLE_TELEGRAM", "true").lower() == "true"
TELEGRAM_NOTIFICATION_LEVEL = os.getenv("TELEGRAM_NOTIFICATION_LEVEL", "normal")  # 可选: minimal, normal, verbose

# 应用设置
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()  # 日志级别: DEBUG, INFO, WARNING, ERROR
DATA_DIR = os.getenv("DATA_DIR", "./data")  # 数据存储目录

# 参数验证
def validate_config():
    """验证配置参数的有效性"""
    errors = []
    
    # 验证API设置
    if not API_KEY and not USE_TESTNET:
        errors.append("API_KEY未设置且未启用测试网络")
    
    if not PRIVATE_KEY and not USE_TESTNET:
        errors.append("PRIVATE_KEY未设置且未启用测试网络")
    
    # 验证网格参数
    if GRID_LEVELS < 3:
        errors.append(f"GRID_LEVELS必须至少为3，当前值: {GRID_LEVELS}")
    
    if GRID_SPACING <= 0:
        errors.append(f"GRID_SPACING必须大于0，当前值: {GRID_SPACING}")
    
    if CAPITAL_PER_LEVEL <= 0:
        errors.append(f"CAPITAL_PER_LEVEL必须大于0，当前值: {CAPITAL_PER_LEVEL}")
    
    if GRID_RANGE_PERCENT <= 0:
        errors.append(f"GRID_RANGE_PERCENT必须大于0，当前值: {GRID_RANGE_PERCENT}")
    
    # 验证非对称网格参数
    if not 0 < CORE_ZONE_PERCENTAGE < 1:
        errors.append(f"CORE_ZONE_PERCENTAGE必须在0和1之间，当前值: {CORE_ZONE_PERCENTAGE}")
    
    if not 0 < CORE_CAPITAL_RATIO < 1:
        errors.append(f"CORE_CAPITAL_RATIO必须在0和1之间，当前值: {CORE_CAPITAL_RATIO}")
    
    if not 0 < CORE_GRID_RATIO < 1:
        errors.append(f"CORE_GRID_RATIO必须在0和1之间，当前值: {CORE_GRID_RATIO}")
    
    # 验证风险管理参数
    if TRAILING_STOP_LOSS_PERCENT <= 0:
        errors.append(f"TRAILING_STOP_LOSS_PERCENT必须大于0，当前值: {TRAILING_STOP_LOSS_PERCENT}")
    
    if TRAILING_TAKE_PROFIT_PERCENT <= 0:
        errors.append(f"TRAILING_TAKE_PROFIT_PERCENT必须大于0，当前值: {TRAILING_TAKE_PROFIT_PERCENT}")
    
    return errors

# 启动时验证配置（可选，取消注释以启用）
# config_errors = validate_config()
# if config_errors:
#     print("配置验证失败:")
#     for error in config_errors:
#         print(f" - {error}")
#     print("请修正以上错误后重新启动程序。")
#     import sys
#     sys.exit(1)