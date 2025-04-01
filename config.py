import os
from dotenv import load_dotenv

load_dotenv()  # 加载.env文件中的环境变量，可存放API密钥等敏感信息

# API设置
API_KEY = os.getenv("API_KEY", "")  # 设置API密钥，从环境变量读取，为空则需要手动配置
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")  # 设置API私钥，从环境变量读取，为空则需要手动配置
PRIVATE_KEY_PASS = os.getenv("PRIVATE_KEY_PASS")  # 私钥密码，如使用加密私钥则必须填写

# API配置
BASE_URL = os.getenv("BASE_URL", "https://api1.binance.com")  # API基础URL，可修改为其他区域节点如api2.binance.com
USE_TESTNET = os.getenv("USE_TESTNET", "false").lower() == "true"  # 是否使用测试网络，改为"true"可切换到测试环境

# WebSocket API配置
PREFER_WEBSOCKET = os.getenv("PREFER_WEBSOCKET", "true").lower() == "true"  # 是否优先使用WebSocket API，改为"false"则只用REST API
WS_PING_INTERVAL = int(os.getenv("WS_PING_INTERVAL", "20"))  # WebSocket心跳间隔(秒)，增大可减少网络开销，但增加连接断开风险
WS_TIMEOUT = int(os.getenv("WS_TIMEOUT", "10"))  # WebSocket请求超时时间(秒)，增大可提高网络不稳定时的成功率
WS_AUTO_RECONNECT = os.getenv("WS_AUTO_RECONNECT", "true").lower() == "true"  # 是否自动重连，改为"false"则连接断开后不自动恢复
WS_RECONNECT_MAX_ATTEMPTS = int(os.getenv("WS_RECONNECT_MAX_ATTEMPTS", "5"))  # 最大重连尝试次数，增加数值可提高恢复能力
WS_RECONNECT_DELAY = int(os.getenv("WS_RECONNECT_DELAY", "5"))  # 重连间隔时间(秒)，增大可避免频繁重连导致API封禁

# 交易设置
SYMBOL = os.getenv("SYMBOL", "TRXUSDT")  # 默认交易对为"TRXUSDT"，可修改为其他如"BTCUSDT"或"ETHUSDT"等
GRID_LEVELS = int(os.getenv("GRID_LEVELS", "5"))  # 网格数量，增加数值可提高交易频率但需要更多资金
GRID_SPACING = float(os.getenv("GRID_SPACING", "0.15"))  # 网格间距(%)，增大可捕获更大波动，减小可提高交易频次

# 注意：CAPITAL_PER_LEVEL 参数已被移除，替换为动态资金分配系统
# 系统现在会根据可用资金自动计算每个网格的资金分配

GRID_RANGE_PERCENT = float(os.getenv("GRID_RANGE_PERCENT", "1.0"))  # 总网格价格范围(%)，增大可覆盖更大波动，减小则集中在小范围
RECALCULATION_PERIOD = int(os.getenv("RECALCULATION_PERIOD", "1"))  # 网格重新计算周期(天)，减小可更频繁更新网格位置
ATR_PERIOD = int(os.getenv("ATR_PERIOD", "14"))  # ATR指标周期，增大可减少敏感度，减小可对短期波动更敏感

# 动态资金分配参数
MIN_GRID_LEVELS = int(os.getenv("MIN_GRID_LEVELS", "3"))  # 最小网格数量，确保至少有这么多网格
MAX_GRID_LEVELS = int(os.getenv("MAX_GRID_LEVELS", "10"))  # 最大网格数量，根据可用资金可能会降低
CAPITAL_RESERVE_PERCENT = float(os.getenv("CAPITAL_RESERVE_PERCENT", "0"))  # 预留资金百分比，用于应对市场波动

# 资金分配策略参数
ALLOCATION_STRATEGY = os.getenv("ALLOCATION_STRATEGY", "balanced")  # 资金分配策略：'conservative', 'balanced', 'aggressive'
CORE_ZONE_BOOST = float(os.getenv("CORE_ZONE_BOOST", "1.5"))  # 核心区域资金提升系数，增加可使核心区域资金更集中

# 非对称网格参数（核心区域优化）
CORE_ZONE_PERCENTAGE = float(os.getenv("CORE_ZONE_PERCENTAGE", "0.7"))  # 核心区域占总范围的比例，增大可集中更多资金在中心价格附近
CORE_CAPITAL_RATIO = float(os.getenv("CORE_CAPITAL_RATIO", "0.8"))  # 核心区域资金比例，增大可增强中心区域交易能力
CORE_GRID_RATIO = float(os.getenv("CORE_GRID_RATIO", "0.7"))  # 核心区域网格点比例，增大可在中心区域创建更多订单

# 订单管理
MAX_ORDER_AGE_HOURS = int(os.getenv("MAX_ORDER_AGE_HOURS", "4"))  # 订单最长存在时间(小时)，减小可更频繁更新长期未成交订单
PRICE_DEVIATION_THRESHOLD = float(os.getenv("PRICE_DEVIATION_THRESHOLD", "0.015"))  # 过期订单距离当前价格阈值，减小可更激进地更新订单
PROFIT_MARGIN_MULTIPLIER = float(os.getenv("PROFIT_MARGIN_MULTIPLIER", "1.2"))  # 利润与手续费比率要求，增大可提高每笔交易利润要求
BUY_SELL_SPREAD = float(os.getenv("BUY_SELL_SPREAD", "0.25"))  # 买卖差价百分比，增大可提高利润但降低成交概率

# 风险管理设置
TRAILING_STOP_LOSS_PERCENT = float(os.getenv("TRAILING_STOP_LOSS_PERCENT", "0.5"))  # 追踪止损百分比，减小可更早触发止损保护资金
TRAILING_TAKE_PROFIT_PERCENT = float(os.getenv("TRAILING_TAKE_PROFIT_PERCENT", "0.8"))  # 追踪止盈百分比，减小可更早锁定利润
RISK_UPDATE_THRESHOLD_PERCENT = float(os.getenv("RISK_UPDATE_THRESHOLD_PERCENT", "0.0025"))  # 风险阈值更新百分比，减小可更灵敏地调整止损位
RISK_UPDATE_INTERVAL_MINUTES = float(os.getenv("RISK_UPDATE_INTERVAL_MINUTES", "5"))  # 风险更新间隔(分钟)，减小可更频繁更新止损止盈

# 高级交易设置
TRADING_FEE_RATE = float(os.getenv("TRADING_FEE_RATE", "0.06"))  # 交易手续费率(%)，根据实际费率调整，影响利润计算
MIN_NOTIONAL_VALUE = float(os.getenv("MIN_NOTIONAL_VALUE", "6"))  # 最小订单价值(USDT)，低于此值的订单将被跳过

# Telegram设置
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")  # Telegram机器人token，填入token可启用通知功能
ALLOWED_TELEGRAM_USERS = [int(user_id.strip()) for user_id in os.getenv("ALLOWED_TELEGRAM_USERS", "").split(",") if user_id.strip()]  # 授权用户ID列表，添加用户ID以获得授权
ENABLE_TELEGRAM = os.getenv("ENABLE_TELEGRAM", "true").lower() == "true"  # 是否启用Telegram通知，改为"false"可关闭通知
TELEGRAM_NOTIFICATION_LEVEL = os.getenv("TELEGRAM_NOTIFICATION_LEVEL", "normal")  # 通知级别，可选"minimal"、"normal"、"verbose"

# 应用设置
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()  # 日志级别，可改为"DEBUG"、"WARNING"、"ERROR"以调整日志详细程度
DATA_DIR = os.getenv("DATA_DIR", "./data")  # 数据存储目录，修改为其他路径可更改数据保存位置

# 参数验证函数
def validate_config():
    """验证配置参数的有效性，检测不合理设置"""
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
    
    # 注意：移除了对 CAPITAL_PER_LEVEL 的验证，因为该参数已不再使用
    
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
    
    # 验证动态资金分配参数
    if MIN_GRID_LEVELS < 2:
        errors.append(f"MIN_GRID_LEVELS必须至少为2，当前值: {MIN_GRID_LEVELS}")
    
    if MAX_GRID_LEVELS < MIN_GRID_LEVELS:
        errors.append(f"MAX_GRID_LEVELS必须大于等于MIN_GRID_LEVELS，当前值: {MAX_GRID_LEVELS}，最小值: {MIN_GRID_LEVELS}")
    
    if not 0 <= CAPITAL_RESERVE_PERCENT <= 50:
        errors.append(f"CAPITAL_RESERVE_PERCENT必须在0和50之间，当前值: {CAPITAL_RESERVE_PERCENT}")
    
    # 验证最小订单价值
    if MIN_NOTIONAL_VALUE <= 0:
        errors.append(f"MIN_NOTIONAL_VALUE必须大于0，当前值: {MIN_NOTIONAL_VALUE}")
    
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