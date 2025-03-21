import os
import logging
from datetime import datetime, timedelta
from binance.spot import Spot
from binance.error import ClientError
import config

class BinanceClient:
    def __init__(self):
        # 从环境变量获取API密钥
        self.api_key = os.getenv("API_KEY", config.API_KEY)
        # 获取私钥文件路径
        private_key_path = os.getenv("PRIVATE_KEY", config.PRIVATE_KEY)
        self.private_key_pass = None if os.getenv("PRIVATE_KEY_PASS") == "None" else os.getenv("PRIVATE_KEY_PASS", 
                                getattr(config, 'PRIVATE_KEY_PASS', None))
        self.base_url = config.BASE_URL
        self.use_testnet = config.USE_TESTNET
        self.logger = logging.getLogger(__name__)
        
        # 初始化Binance API客户端
        try:
            # 读取私钥文件内容而非路径
            if os.path.isfile(private_key_path):
                with open(private_key_path, 'rb') as f:
                    private_key_data = f.read()
            else:
                self.logger.error(f"私钥文件不存在: {private_key_path}")
                raise FileNotFoundError(f"私钥文件不存在: {private_key_path}")
            
            # 设置正确的base_url
            base_url = "https://testnet.binance.vision" if self.use_testnet else self.base_url
            
            # 使用正确的初始化方式
            self.client = Spot(
                api_key=self.api_key,
                base_url=base_url,
                private_key=private_key_data,
                private_key_pass=self.private_key_pass
            )
            
            # 验证连接是否有效
            self.client.account()
            self.logger.info("Binance API连接成功 (使用Ed25519验证)")
        except ClientError as e:
            self.logger.error(f"Binance API连接失败: {e}")
            
            # 如果是API格式错误，尝试使用API_SECRET回退
            if hasattr(e, 'error_code') and e.error_code == -2014:
                self.logger.warning("API密钥格式错误，请确保使用了正确的ED25519 API密钥")
                self.logger.warning("请访问 https://www.binance.com/zh-CN/support/faq/%E5%A6%82%E4%BD%95%E7%94%9F%E6%88%90ed25519%E5%AF%86%E9%92%A5%E5%AF%B9%E5%9C%A8%E5%B8%81%E5%AE%89%E5%8F%91%E9%80%81api%E8%AF%B7%E6%B1%82-6b9a63f1e3384cf48a2eedb82767a69a")
            raise
            
    def get_exchange_info(self, symbol=None):
        """获取交易所信息"""
        try:
            if symbol:
                return self.client.exchange_info(symbol=symbol)
            return self.client.exchange_info()
        except ClientError as e:
            self.logger.error(f"获取交易所信息失败: {e}")
            raise

    def get_symbol_info(self, symbol):
        """获取交易对详细信息"""
        try:
            info = self.client.exchange_info(symbol=symbol)
            if info and "symbols" in info and len(info["symbols"]) > 0:
                return info["symbols"][0]
            return None
        except ClientError as e:
            self.logger.error(f"获取交易对信息失败: {e}")
            raise

    def get_account_info(self):
        """获取账户信息"""
        try:
            return self.client.account()
        except ClientError as e:
            self.logger.error(f"获取账户信息失败: {e}")
            raise

    def get_symbol_price(self, symbol):
        """获取指定交易对的最新价格"""
        try:
            ticker = self.client.ticker_price(symbol=symbol)
            return float(ticker['price'])
        except ClientError as e:
            self.logger.error(f"获取{symbol}价格失败: {e}")
            raise
            
    def place_limit_order(self, symbol, side, quantity, price):
        """下限价单"""
        try:
            # 确保使用字符串格式
            params = {
                'symbol': symbol,
                'side': side,
                'type': 'LIMIT',
                'timeInForce': 'GTC',
                'quantity': str(quantity),
                'price': str(price)
            }
            return self.client.new_order(**params)
        except ClientError as e:
            self.logger.error(f"下限价单失败: {e}")
            raise
            
    def place_market_order(self, symbol, side, quantity):
        """下市价单"""
        try:
            params = {
                'symbol': symbol,
                'side': side,
                'type': 'MARKET',
                'quantity': str(quantity)  # 确保使用字符串格式
            }
            return self.client.new_order(**params)
        except ClientError as e:
            self.logger.error(f"下市价单失败: {e}")
            raise
            
    def cancel_order(self, symbol, order_id):
        """取消订单"""
        try:
            return self.client.cancel_order(symbol=symbol, orderId=order_id)
        except ClientError as e:
            self.logger.error(f"取消订单失败: {e}")
            raise
            
    def get_open_orders(self, symbol=None):
        """获取当前挂单"""
        try:
            if symbol:
                return self.client.get_open_orders(symbol=symbol)
            return self.client.get_open_orders()
        except ClientError as e:
            self.logger.error(f"获取挂单失败: {e}")
            raise
            
    def get_historical_klines(self, symbol, interval, start_str=None, limit=500):
        """获取K线历史数据"""
        try:
            return self.client.klines(
                symbol=symbol,
                interval=interval,
                startTime=start_str,
                limit=limit
            )
        except ClientError as e:
            self.logger.error(f"获取K线数据失败: {e}")
            raise

    def place_stop_loss_order(self, symbol, quantity, stop_price):
        """下止损单"""
        try:
            params = {
                'symbol': symbol,
                'side': 'SELL',
                'type': 'STOP_LOSS',
                'quantity': quantity,
                'stopPrice': stop_price,
                'timeInForce': 'GTC'
            }
            return self.client.new_order(**params)
        except ClientError as e:
            self.logger.error(f"下止损单失败: {e}")
            raise
    
    def place_take_profit_order(self, symbol, quantity, stop_price):
        """下止盈单"""
        try:
            params = {
                'symbol': symbol,
                'side': 'SELL',
                'type': 'TAKE_PROFIT',
                'quantity': quantity,
                'stopPrice': stop_price,
                'timeInForce': 'GTC'
            }
            return self.client.new_order(**params)
        except ClientError as e:
            self.logger.error(f"下止盈单失败: {e}")
            raise

    def new_oco_order(self, symbol, side, quantity, price, stopPrice, stopLimitPrice=None, stopLimitTimeInForce="GTC", **kwargs):
        """创建OCO订单（One-Cancels-the-Other）"""
        try:
            params = {
                'symbol': symbol,
                'side': side,
                'quantity': str(quantity),
                'price': str(price),
                'stopPrice': str(stopPrice),
                'stopLimitTimeInForce': stopLimitTimeInForce
            }
            
            if stopLimitPrice:
                params['stopLimitPrice'] = str(stopLimitPrice)
                
            # 添加其他可选参数
            params.update(kwargs)
            
            return self.client.new_oco_order(**params)
        except ClientError as e:
            self.logger.error(f"创建OCO订单失败: {e}")
            raise