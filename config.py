import os
from dotenv import load_dotenv

load_dotenv()  # 加载.env文件中的环境变量，可存放API密钥等敏感信息

#############################################
# 账户与API设置
#############################################

# 敏感信息从环境变量读取
API_KEY = os.getenv("API_KEY", "")  # 设置API密钥，从环境变量读取，为空则需要手动配置
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")  # 设置API私钥，从环境变量读取，为空则需要手动配置
PRIVATE_KEY_PASS = os.getenv("PRIVATE_KEY_PASS")  # 私钥密码，如使用加密私钥则必须填写
USE_TESTNET = os.getenv("USE_TESTNET", "false").lower() == "true"  # 是否使用测试网络，改为"true"可切换到测试环境

# API配置
BASE_URL = "https://api1.binance.com"  # API基础URL，可修改为其他区域节点如api2.binance.com

# WebSocket API配置
PREFER_WEBSOCKET = True  # 是否优先使用WebSocket API，改为False则只用REST API
WS_PING_INTERVAL = 20  # WebSocket心跳间隔(秒)，增大可减少网络开销，但增加连接断开风险
WS_TIMEOUT = 10  # WebSocket请求超时时间(秒)，增大可提高网络不稳定时的成功率
WS_AUTO_RECONNECT = True  # 是否自动重连，改为False则连接断开后不自动恢复
WS_RECONNECT_MAX_ATTEMPTS = 5  # 最大重连尝试次数，增加数值可提高恢复能力
WS_RECONNECT_DELAY = 5  # 重连间隔时间(秒)，增大可避免频繁重连导致API封禁

#############################################
# 交易基本设置
#############################################

SYMBOL = "GUNUSDT"  # 默认交易对为"GUNUSDT"，可修改为其他如"BTCUSDT"或"ETHUSDT"等
CAPITAL_PER_LEVEL = 15  # 每个网格的资金(USDT)，增加可提高利润但需要更多总资金
CAPITAL_SIZE = "standard"  # 资金规模，可选值："small"(小资金优化) 或 "standard"(标准资金)

#############################################
# 网格参数 (全部以小数形式表示)
#############################################

GRID_LEVELS = 12  # 网格数量，增加数值可提高交易频率但需要更多资金
GRID_SPACING = 0.0015  # 网格间距 (0.15%)
GRID_RANGE_PERCENT = 0.02  # 总网格价格范围 (2.0%)
MAX_GRID_SPACING = 0.03  # 最大单网格间距 (3%)
MAX_GRID_RANGE = 0.05  # 最大网格总范围 (5%)
RECALCULATION_PERIOD = 1  # 网格重新计算周期(天)，减小可更频繁更新网格位置

# ATR相关设置 (波动性指标)
ATR_PERIOD = 14  # ATR指标周期，增大可减少敏感度，减小可对短期波动更敏感
ATR_RATIO = 0.2  # ATR比例系数，增大可设置更宽的网格间距

# 非对称网格参数（核心区域优化）
CORE_ZONE_PERCENTAGE = 0.7  # 核心区域占总范围的比例，增大可集中更多资金在中心价格附近
CORE_CAPITAL_RATIO = 0.7  # 核心区域资金比例，增大可增强中心区域交易能力
CORE_GRID_RATIO = 0.7  # 核心区域网格点比例，增大可在中心区域创建更多订单

#############################################
# 订单和交易参数 (全部以小数形式表示)
#############################################

# 订单管理
MAX_ORDER_AGE_HOURS = 4  # 订单最长存在时间(小时)，减小可更频繁更新长期未成交订单
PRICE_DEVIATION_THRESHOLD = 0.015  # 过期订单距离当前价格阈值，减小可更激进地更新订单

# 交易参数
TRADING_FEE_RATE = 0.0006  # 单向交易手续费率 (0.06%)
PROFIT_MARGIN_MULTIPLIER = 1.2  # 要求利润必须是手续费的倍数
BUY_SELL_SPREAD = 0.0025  # 买卖价差 (0.25%)
MIN_NOTIONAL_VALUE = 6  # 最小订单价值(USDT)，低于此值的订单将被跳过

#############################################
# 风险管理设置
#############################################

TRAILING_STOP_LOSS_PERCENT = 0.5  # 追踪止损百分比（以百分数填写，例如0.5表示0.5%）
TRAILING_TAKE_PROFIT_PERCENT = 0.8  # 追踪止盈百分比（以百分数填写，例如0.8表示0.8%）
RISK_UPDATE_THRESHOLD_PERCENT = 0.0025  # 风险阈值更新百分比，减小可更灵敏地调整止损位
RISK_UPDATE_INTERVAL_MINUTES = 5  # 风险更新间隔(分钟)，减小可更频繁更新止损止盈

#############################################
# 通知与应用设置
#############################################

# Telegram设置
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")  # Telegram机器人token，填入token可启用通知功能
ALLOWED_TELEGRAM_USERS = [int(user_id.strip()) for user_id in os.getenv("ALLOWED_TELEGRAM_USERS", "").split(",") if user_id.strip()]  # 授权用户ID列表
ENABLE_TELEGRAM = True  # 是否启用Telegram通知，改为False可关闭通知
TELEGRAM_NOTIFICATION_LEVEL = "normal"  # 通知级别，可选"minimal"、"normal"、"verbose"

# 应用设置
LOG_LEVEL = "INFO"  # 日志级别，可改为"DEBUG"、"WARNING"、"ERROR"以调整日志详细程度
DATA_DIR = "./data"  # 数据存储目录，修改为其他路径可更改数据保存位置

#############################################
# 配置验证函数
#############################################

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
    
    if CAPITAL_PER_LEVEL <= 0:
        errors.append(f"CAPITAL_PER_LEVEL必须大于0，当前值: {CAPITAL_PER_LEVEL}")
    
    if GRID_RANGE_PERCENT <= 0:
        errors.append(f"GRID_RANGE_PERCENT必须大于0，当前值: {GRID_RANGE_PERCENT}")
    
    # 验证交易参数
    if TRADING_FEE_RATE <= 0:
        errors.append(f"TRADING_FEE_RATE必须大于0，当前值: {TRADING_FEE_RATE}")
    
    if PROFIT_MARGIN_MULTIPLIER <= 1:
        errors.append(f"PROFIT_MARGIN_MULTIPLIER必须大于1，当前值: {PROFIT_MARGIN_MULTIPLIER}")
        
    if BUY_SELL_SPREAD <= 0:
        errors.append(f"BUY_SELL_SPREAD必须大于0，当前值: {BUY_SELL_SPREAD}")
    
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
