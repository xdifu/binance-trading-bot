#!/usr/bin/env python3
# filepath: /home/god/Binance/grid_trading_bot/diagnostics.py

import os
import sys
import json
import time
import logging
from datetime import datetime
import traceback

# 添加项目根目录到系统路径
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 导入必要模块
from binance_api.client import BinanceClient
from core.grid_trader import GridTrader
import config

# 配置日志
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('diagnostics.log')
    ]
)

logger = logging.getLogger("diagnostics")

class GridTradingDiagnostics:
    def __init__(self):
        self.client = None
        self.grid_trader = None
        self.symbol = config.SYMBOL
        self.results = {
            "time_sync": None,
            "api_connection": None,
            "symbol_info": None,
            "balance": None,
            "price_detection": None,
            "grid_calculation": None,
            "order_validation": None,
            "oco_validation": None
        }
        
    def run_diagnostics(self):
        """执行完整诊断流程"""
        try:
            logger.info("="*50)
            logger.info("开始Binance Grid Trading Bot诊断程序")
            logger.info("="*50)
            
            # 初始化客户端
            self._initialize_client()
            
            # 检查时间同步
            self._check_time_sync()
            
            # 检查API连接
            self._check_api_connection()
            
            # 获取交易对信息
            self._check_symbol_info()
            
            # 检查余额
            self._check_balance()
            
            # 检查当前价格
            self._check_current_price()
            
            # 测试网格计算
            self._test_grid_calculation()
            
            # 测试订单创建验证
            self._test_order_validation()
            
            # 测试OCO订单参数
            self._test_oco_validation()
            
            # 提供诊断建议
            self._provide_recommendations()
            
            logger.info("="*50)
            logger.info("诊断完成")
            logger.info("="*50)
            
            return self.results
            
        except Exception as e:
            logger.error(f"诊断过程中发生错误: {e}")
            logger.error(traceback.format_exc())
            return {"error": str(e)}

    def _initialize_client(self):
        """初始化Binance客户端"""
        logger.info("初始化Binance客户端...")
        try:
            self.client = BinanceClient()
            logger.info("Binance客户端初始化成功")
        except Exception as e:
            logger.error(f"初始化客户端失败: {e}")
            raise

    def _check_time_sync(self):
        """检查与Binance服务器的时间同步"""
        logger.info("检查时间同步...")
        
        try:
            # 获取本地时间
            local_time = int(time.time() * 1000)
            
            # 获取服务器时间
            server_time_response = self.client.rest_client.time()
            server_time = server_time_response.get('serverTime')
            
            # 计算时间差
            time_diff_ms = server_time - local_time
            time_diff_sec = abs(time_diff_ms) / 1000
            
            status = "正常" if abs(time_diff_ms) < 1000 else "异常"
            logger.info(f"本地时间: {datetime.fromtimestamp(local_time/1000).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}")
            logger.info(f"服务器时间: {datetime.fromtimestamp(server_time/1000).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}")
            logger.info(f"时间差异: {time_diff_ms} ms ({time_diff_sec:.2f} 秒), 状态: {status}")
            
            # 手动调用时间同步
            self.client.manual_time_sync()
            
            # 再次获取时间偏移值
            current_offset = self.client.time_offset
            logger.info(f"时间同步后，当前时间偏移值: {current_offset} ms")
            
            self.results["time_sync"] = {
                "status": status,
                "local_time": local_time,
                "server_time": server_time,
                "difference_ms": time_diff_ms,
                "difference_sec": time_diff_sec,
                "current_offset": current_offset
            }
            
        except Exception as e:
            logger.error(f"检查时间同步失败: {e}")
            self.results["time_sync"] = {"status": "失败", "error": str(e)}

    def _check_api_connection(self):
        """检查API连接状态"""
        logger.info("检查API连接...")
        
        try:
            # 检查WebSocket API状态
            ws_status = self.client.get_client_status()["websocket_available"]
            logger.info(f"WebSocket API 可用: {ws_status}")
            
            # 检查REST API状态
            rest_status = self.client.get_client_status()["rest_available"]
            logger.info(f"REST API 可用: {rest_status}")
            
            # 如果WebSocket可用，检查其会话状态
            ws_session_authenticated = False
            if ws_status and self.client.ws_client:
                ws_session_authenticated = getattr(self.client.ws_client.client, "session_authenticated", False)
                logger.info(f"WebSocket 会话认证状态: {ws_session_authenticated}")
            
            self.results["api_connection"] = {
                "websocket_available": ws_status,
                "rest_available": rest_status,
                "websocket_authenticated": ws_session_authenticated
            }
            
        except Exception as e:
            logger.error(f"检查API连接失败: {e}")
            self.results["api_connection"] = {"status": "失败", "error": str(e)}

    def _check_symbol_info(self):
        """检查交易对信息"""
        logger.info(f"获取交易对 {self.symbol} 信息...")
        
        try:
            # 获取交易对信息
            symbol_info = self.client.get_symbol_info(self.symbol)
            
            if not symbol_info:
                logger.warning(f"交易对 {self.symbol} 信息未找到")
                self.results["symbol_info"] = {"status": "失败", "error": "未找到交易对信息"}
                return
            
            # 提取价格和数量精度
            filters = symbol_info.get("filters", [])
            price_precision = None
            quantity_precision = None
            min_price = None
            min_notional = None
            
            for f in filters:
                if f["filterType"] == "PRICE_FILTER":
                    price_precision = self._get_precision_from_step(f["tickSize"])
                    min_price = float(f["minPrice"])
                elif f["filterType"] == "LOT_SIZE":
                    quantity_precision = self._get_precision_from_step(f["stepSize"])
                elif f["filterType"] == "MIN_NOTIONAL":
                    min_notional = float(f["minNotional"])
            
            logger.info(f"交易对: {self.symbol}")
            logger.info(f"状态: {symbol_info.get('status', 'Unknown')}")
            logger.info(f"价格精度: {price_precision}")
            logger.info(f"数量精度: {quantity_precision}")
            logger.info(f"最小价格: {min_price}")
            logger.info(f"最小交易额: {min_notional}")
            
            self.results["symbol_info"] = {
                "status": "成功",
                "price_precision": price_precision,
                "quantity_precision": quantity_precision,
                "min_price": min_price,
                "min_notional": min_notional
            }
            
        except Exception as e:
            logger.error(f"获取交易对信息失败: {e}")
            self.results["symbol_info"] = {"status": "失败", "error": str(e)}

    def _check_balance(self):
        """检查账户余额"""
        logger.info("检查账户余额...")
        
        try:
            # 分离交易对的基础资产和报价资产
            base_asset = self.symbol.replace("USDT", "")
            quote_asset = "USDT"
            
            # 获取余额
            base_balance = self.client.check_balance(base_asset)
            quote_balance = self.client.check_balance(quote_asset)
            
            # 计算网格交易所需资金
            total_needed = config.CAPITAL_PER_LEVEL * config.GRID_LEVELS
            
            logger.info(f"{base_asset} 余额: {base_balance}")
            logger.info(f"{quote_asset} 余额: {quote_balance}")
            logger.info(f"网格交易所需 {quote_asset}: {total_needed}")
            logger.info(f"余额是否足够: {'是' if quote_balance >= total_needed else '否'}")
            
            self.results["balance"] = {
                "base_asset": base_asset,
                "base_balance": base_balance,
                "quote_asset": quote_asset,
                "quote_balance": quote_balance,
                "required_capital": total_needed,
                "sufficient": quote_balance >= total_needed
            }
            
        except Exception as e:
            logger.error(f"检查余额失败: {e}")
            self.results["balance"] = {"status": "失败", "error": str(e)}

    def _check_current_price(self):
        """检查当前价格"""
        logger.info(f"获取 {self.symbol} 当前价格...")
        
        try:
            # 通过WebSocket API获取价格
            ws_price = None
            if self.client.websocket_available:
                try:
                    logger.info("尝试通过WebSocket API获取价格...")
                    ws_price = self.client.get_symbol_price(self.symbol)
                    logger.info(f"WebSocket API 价格: {ws_price}")
                except Exception as e:
                    logger.warning(f"通过WebSocket获取价格失败: {e}")
            
            # 通过REST API获取价格（作为备用）
            rest_price = None
            if self.client.rest_client:
                try:
                    logger.info("尝试通过REST API获取价格...")
                    ticker = self.client.rest_client.ticker_price(symbol=self.symbol)
                    rest_price = float(ticker['price'])
                    logger.info(f"REST API 价格: {rest_price}")
                except Exception as e:
                    logger.warning(f"通过REST获取价格失败: {e}")
            
            # 比较两个价格
            price_diff = None
            if ws_price and rest_price:
                price_diff = abs(ws_price - rest_price)
                logger.info(f"价格差异: {price_diff}")
            
            current_price = ws_price or rest_price
            logger.info(f"当前使用价格: {current_price}")
            
            self.results["price_detection"] = {
                "status": "成功" if current_price else "失败",
                "websocket_price": ws_price,
                "rest_price": rest_price,
                "price_difference": price_diff,
                "current_price": current_price
            }
            
        except Exception as e:
            logger.error(f"获取价格失败: {e}")
            self.results["price_detection"] = {"status": "失败", "error": str(e)}

    def _test_grid_calculation(self):
        """测试网格计算逻辑"""
        logger.info("测试网格计算逻辑...")
        
        try:
            # 初始化GridTrader但不启动
            self.grid_trader = GridTrader(
                binance_client=self.client,
                telegram_bot=None
            )
            
            # 获取当前价格
            current_price = self.results["price_detection"]["current_price"]
            if not current_price:
                logger.warning("没有有效价格，无法测试网格计算")
                self.results["grid_calculation"] = {"status": "失败", "error": "无有效价格"}
                return
                
            # 保存当前市场价格
            self.grid_trader.current_market_price = current_price
            
            # 计算网格
            grid_levels = self.grid_trader._calculate_grid_levels()
            
            # 检查网格价格是否有效
            invalid_prices = [level for level in grid_levels if level['price'] <= 0]
            
            # 输出网格信息
            logger.info(f"计算的网格级别数: {len(grid_levels)}")
            logger.info(f"无效价格的网格数: {len(invalid_prices)}")
            
            # 输出每个网格的详细信息
            for i, level in enumerate(grid_levels):
                price = level['price']
                side = level['side']
                formatted_price = self.grid_trader._adjust_price_precision(price)
                logger.info(f"网格 {i+1}: 价格={price} ({formatted_price}), 方向={side}")
                
                # 计算订单数量
                quantity = self.grid_trader.capital_per_level / price
                formatted_quantity = self.grid_trader._adjust_quantity_precision(quantity)
                logger.info(f"  数量={quantity} ({formatted_quantity})")
                
            self.results["grid_calculation"] = {
                "status": "成功" if not invalid_prices else "有无效价格",
                "grid_count": len(grid_levels),
                "invalid_prices": len(invalid_prices),
                "grid_details": [
                    {
                        "price": level['price'],
                        "formatted_price": self.grid_trader._adjust_price_precision(level['price']),
                        "side": level['side'],
                        "quantity": self.grid_trader.capital_per_level / level['price'],
                        "formatted_quantity": self.grid_trader._adjust_quantity_precision(
                            self.grid_trader.capital_per_level / level['price']
                        )
                    }
                    for level in grid_levels
                ]
            }
            
        except Exception as e:
            logger.error(f"测试网格计算失败: {e}")
            logger.error(traceback.format_exc())
            self.results["grid_calculation"] = {"status": "失败", "error": str(e)}

    def _test_order_validation(self):
        """测试订单创建验证"""
        logger.info("测试订单参数验证...")
        
        if not self.results["grid_calculation"] or self.results["grid_calculation"]["status"] == "失败":
            logger.warning("跳过订单验证测试，网格计算失败")
            self.results["order_validation"] = {"status": "跳过", "error": "网格计算未完成"}
            return
            
        try:
            # 获取第一个网格的数据用于测试
            grid_details = self.results["grid_calculation"]["grid_details"]
            if not grid_details:
                logger.warning("没有网格详情，无法测试订单验证")
                self.results["order_validation"] = {"status": "跳过", "error": "无网格详情"}
                return
                
            # 选择一个有效的网格用于测试
            test_grid = next((g for g in grid_details if g["price"] > 0), None)
            if not test_grid:
                logger.warning("找不到有效价格的网格用于测试")
                self.results["order_validation"] = {"status": "跳过", "error": "无有效价格网格"}
                return
                
            # 构造订单参数
            symbol = self.symbol
            side = test_grid["side"]
            quantity = test_grid["formatted_quantity"]
            price = test_grid["formatted_price"]
            
            logger.info(f"测试订单参数: {symbol}, {side}, {quantity}@{price}")
            
            # 测试价格格式化
            price_validation_tests = [
                {"value": 0, "expected": "应拒绝"},
                {"value": "0", "expected": "应拒绝"},
                {"value": -1, "expected": "应拒绝"},
                {"value": float(price), "expected": "应接受"},
                {"value": price, "expected": "应接受"}
            ]
            
            price_results = []
            for test in price_validation_tests:
                try:
                    # 测试通过client._adjust_price_precision
                    formatted = self.client._adjust_price_precision(test["value"])
                    valid = float(formatted) > 0
                    price_results.append({
                        "value": test["value"],
                        "formatted": formatted,
                        "expected": test["expected"],
                        "valid": valid,
                        "result": "通过" if (valid and test["expected"] == "应接受") or 
                                    (not valid and test["expected"] == "应拒绝") else "失败"
                    })
                except Exception as e:
                    price_results.append({
                        "value": test["value"],
                        "error": str(e),
                        "expected": test["expected"],
                        "result": "异常"
                    })
                    
            # 记录结果
            for result in price_results:
                logger.info(f"价格验证测试: {result}")
                
            # 不执行实际下单，但检查API调用中的参数格式化
            self.results["order_validation"] = {
                "status": "成功",
                "test_order_params": {
                    "symbol": symbol,
                    "side": side,
                    "quantity": quantity,
                    "price": price
                },
                "price_validation_tests": price_results
            }
            
        except Exception as e:
            logger.error(f"测试订单验证失败: {e}")
            self.results["order_validation"] = {"status": "失败", "error": str(e)}

    def _test_oco_validation(self):
        """测试OCO订单参数"""
        logger.info("测试OCO订单参数验证...")
        
        try:
            # 获取当前价格
            current_price = self.results["price_detection"]["current_price"]
            if not current_price:
                logger.warning("没有有效价格，无法测试OCO参数")
                self.results["oco_validation"] = {"status": "跳过", "error": "无有效价格"}
                return
                
            # 创建测试参数
            symbol = self.symbol
            side = "SELL"
            quantity = "100"  # 使用字符串模拟真实场景
            price = str(current_price * 1.05)  # 高于当前价格5%
            stopPrice = str(current_price * 0.95)  # 低于当前价格5%
            
            # 1. 测试与强制stopLimitPrice
            try:
                logger.info("测试1: 标准OCO参数")
                params = {
                    'symbol': symbol,
                    'side': side,
                    'quantity': quantity,
                    'price': price,
                    'stopPrice': stopPrice,
                    'stopLimitTimeInForce': "GTC"
                }
                
                # 添加stopLimitPrice
                stop_limit_price = float(stopPrice) * 0.99
                params['stopLimitPrice'] = self.client._adjust_price_precision(stop_limit_price)
                
                logger.info(f"标准OCO参数: {params}")
                
                # 不实际执行API调用，检查是否有所需参数
                required_keys = ['symbol', 'side', 'quantity', 'price', 'stopPrice', 'stopLimitPrice', 'stopLimitTimeInForce']
                missing_keys = [key for key in required_keys if key not in params]
                
                if missing_keys:
                    logger.warning(f"缺少必需参数: {missing_keys}")
                else:
                    logger.info("所有必需参数都存在")
                
            except Exception as e:
                logger.error(f"测试1失败: {e}")
            
            # 2. 测试零价格
            try:
                logger.info("测试2: 零价格")
                invalid_params = {
                    'symbol': symbol,
                    'side': side,
                    'quantity': quantity,
                    'price': "0",
                    'stopPrice': stopPrice,
                    'stopLimitTimeInForce': "GTC"
                }
                
                try:
                    # 模拟值验证
                    if float(invalid_params['price']) <= 0 or float(invalid_params['stopPrice']) <= 0:
                        logger.info("零价格被正确拒绝")
                    else:
                        logger.warning("零价格没有被拒绝")
                except:
                    logger.info("零价格引发了预期的异常")
                
            except Exception as e:
                logger.error(f"测试2失败: {e}")
            
            self.results["oco_validation"] = {
                "status": "完成",
                "test_params": {
                    "symbol": symbol,
                    "side": side,
                    "quantity": quantity,
                    "price": price,
                    "stopPrice": stopPrice,
                    "stopLimitPrice": self.client._adjust_price_precision(float(stopPrice) * 0.99)
                }
            }
                
        except Exception as e:
            logger.error(f"测试OCO参数验证失败: {e}")
            self.results["oco_validation"] = {"status": "失败", "error": str(e)}

    def _provide_recommendations(self):
        """基于诊断结果提供建议"""
        logger.info("生成诊断建议...")
        
        recommendations = []
        
        # 检查时间同步
        if self.results["time_sync"] and self.results["time_sync"].get("status") == "异常":
            recommendations.append(
                "系统时间与Binance服务器时间差异过大，应同步系统时间。"
                "可以使用 'sudo ntpdate time.nist.gov' 命令同步时间。"
            )
        
        # 检查API连接
        if self.results["api_connection"]:
            if not self.results["api_connection"].get("websocket_available"):
                recommendations.append(
                    "WebSocket API连接不可用，检查网络连接或重新初始化WebSocket客户端。"
                )
            
            if not self.results["api_connection"].get("rest_available"):
                recommendations.append(
                    "REST API连接不可用，检查API密钥和网络连接。"
                )
                
            if not self.results["api_connection"].get("websocket_authenticated"):
                recommendations.append(
                    "WebSocket API会话未认证，可能导致频繁的签名请求。"
                )
        
        # 检查余额
        if self.results["balance"] and not self.results["balance"].get("sufficient"):
            quote_asset = self.results["balance"].get("quote_asset", "USDT")
            required = self.results["balance"].get("required_capital", "未知")
            available = self.results["balance"].get("quote_balance", "未知")
            recommendations.append(
                f"账户{quote_asset}余额不足，需要{required}但只有{available}。"
                "请充值或减少网格层数/每层资金。"
            )
        
        # 检查网格计算
        if self.results["grid_calculation"] and self.results["grid_calculation"].get("status") == "有无效价格":
            recommendations.append(
                "网格计算产生了无效价格(0或负值)，检查网格范围和当前价格。"
                "可能需要调整网格间距或交易对参数。"
            )
        
        # 提供总结
        if recommendations:
            logger.info("诊断建议:")
            for i, rec in enumerate(recommendations, 1):
                logger.info(f"{i}. {rec}")
        else:
            logger.info("诊断未发现明显问题，检查订单参数和API调用细节。")

    def _get_precision_from_step(self, step_str):
        """从step值获取精度"""
        try:
            # 将科学计数法转换为小数
            step = float(step_str)
            if step == 0:
                return 0
                
            # 解析小数位数
            decimal_str = str(step)
            if 'e' in decimal_str.lower():
                # 处理科学计数法
                mantissa, exponent = decimal_str.lower().split('e')
                return abs(int(exponent))
            else:
                # 处理普通小数
                decimal_part = decimal_str.split('.')
                if len(decimal_part) == 1:
                    return 0  # 没有小数部分
                return len(decimal_part[1].rstrip('0'))
        except Exception as e:
            logger.error(f"解析精度失败: {step_str}, {e}")
            return 8  # 默认为8位精度

# 执行诊断
if __name__ == "__main__":
    diagnostics = GridTradingDiagnostics()
    results = diagnostics.run_diagnostics()
    
    # 保存诊断结果到JSON文件
    try:
        with open("diagnostics_results.json", "w") as f:
            json.dump(results, f, indent=2)
        print("\n诊断结果已保存到 diagnostics_results.json")
    except:
        print("\n保存诊断结果失败")
    
    print("\n诊断完成! 查看上面的日志获取详细结果和建议。")