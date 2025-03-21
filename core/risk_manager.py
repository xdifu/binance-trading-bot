import logging
import time
from datetime import datetime
from utils.format_utils import format_price, format_quantity, get_precision_from_filters
import config

class RiskManager:
    def __init__(self, binance_client, telegram_bot=None):
        self.binance_client = binance_client
        self.telegram_bot = telegram_bot
        self.symbol = config.SYMBOL
        self.trailing_stop_loss_percent = config.TRAILING_STOP_LOSS_PERCENT / 100  # 转换为小数
        self.trailing_take_profit_percent = config.TRAILING_TAKE_PROFIT_PERCENT / 100  # 转换为小数
        
        # 获取交易对信息并设置精度
        self.symbol_info = self.binance_client.get_symbol_info(self.symbol)
        self.price_precision = self._get_price_precision()
        self.quantity_precision = self._get_quantity_precision()
        
        self.stop_loss_price = None
        self.highest_price = None
        self.lowest_price = None
        self.take_profit_price = None
        self.oco_order_id = None
        
        self.is_active = False
        self.logger = logging.getLogger(__name__)

    def _get_price_precision(self):
        """获取价格精度"""
        if not self.symbol_info or 'filters' not in self.symbol_info:
            return 2  # 默认价格精度
        return get_precision_from_filters(self.symbol_info['filters'], 'PRICE_FILTER', 'tickSize')
    
    def _get_quantity_precision(self):
        """获取数量精度"""
        if not self.symbol_info or 'filters' not in self.symbol_info:
            return 5  # 默认数量精度
        return get_precision_from_filters(self.symbol_info['filters'], 'LOT_SIZE', 'stepSize')
        
    def _adjust_price_precision(self, price):
        """调整价格精度"""
        return format_price(price, self.price_precision)
    
    def _adjust_quantity_precision(self, quantity):
        """调整数量精度"""
        return format_quantity(quantity, self.quantity_precision)
        
    def activate(self, grid_lowest, grid_highest):
        """激活风险管理"""
        try:
            # 获取当前价格
            current_price = self.binance_client.get_symbol_price(self.symbol)
            
            # 设置初始止损价和止盈价
            self.stop_loss_price = grid_lowest * (1 - self.trailing_stop_loss_percent)
            self.take_profit_price = grid_highest * (1 + self.trailing_take_profit_percent)
            
            # 设置历史最高最低价格
            self.highest_price = current_price
            self.lowest_price = current_price
            
            message = (f"风险管理已激活\n"
                       f"当前价格: {current_price}\n"
                       f"止损价: {self.stop_loss_price}\n"
                       f"止盈价: {self.take_profit_price}")
            
            self.logger.info(message)
            if self.telegram_bot:
                self.telegram_bot.send_message(message)
            
            self.is_active = True
            
            # 创建初始OCO单
            self._place_oco_orders()
            
        except Exception as e:
            self.logger.error(f"激活风险管理失败: {e}")
            
    def deactivate(self):
        """停用风险管理"""
        if not self.is_active:
            return
        
        try:
            # 取消现有OCO订单
            self._cancel_oco_orders()
            
            self.is_active = False
            self.stop_loss_price = None
            self.take_profit_price = None
            self.highest_price = None
            self.lowest_price = None
            self.oco_order_id = None
            
            message = "风险管理已停用"
            self.logger.info(message)
            if self.telegram_bot:
                self.telegram_bot.send_message(message)
        except Exception as e:
            self.logger.error(f"停用风险管理失败: {e}")

    def check_price(self, current_price):
        """检查价格是否需要更新追踪止损或止盈"""
        if not self.is_active:
            return None
        
        update_required = False
        
        # 更新历史最高价和最低价
        if current_price > self.highest_price:
            self.highest_price = current_price
            # 更新追踪止盈价格
            new_take_profit = current_price * (1 - self.trailing_take_profit_percent)
            if new_take_profit > self.take_profit_price:
                self.take_profit_price = new_take_profit
                update_required = True
                self.logger.info(f"更新追踪止盈价格: {self.take_profit_price}")
        
        if current_price < self.lowest_price:
            self.lowest_price = current_price
            # 更新追踪止损价格
            new_stop_loss = current_price * (1 - self.trailing_stop_loss_percent)
            if new_stop_loss > self.stop_loss_price:
                self.stop_loss_price = new_stop_loss
                update_required = True
                self.logger.info(f"更新追踪止损价格: {self.stop_loss_price}")
        
        # 如果需要更新订单
        if update_required and self.oco_order_id:
            self._cancel_oco_orders()
            self._place_oco_orders()
            
        return None  # 不再直接返回操作，而是通过OCO订单执行

    def _place_oco_orders(self):
        """创建OCO订单对（止损和止盈）"""
        try:
            # 获取账户余额
            balance = self.binance_client.get_account_info()
            asset = self.symbol.replace('USDT', '')
            
            # 查找对应资产余额
            asset_balance = None
            for item in balance['balances']:
                if item['asset'] == asset:
                    asset_balance = float(item['free'])
                    break
            
            if not asset_balance or asset_balance <= 0:
                self.logger.info(f"没有可用的{asset}资产进行风险管理")
                return
            
            # 设置数量（全部可用资产）并格式化
            quantity = self._adjust_quantity_precision(asset_balance)
            
            # 获取当前价格
            current_price = self.binance_client.get_symbol_price(self.symbol)
            
            # 格式化止损和止盈价格
            stop_price = self._adjust_price_precision(self.stop_loss_price)
            stop_limit_price = self._adjust_price_precision(self.stop_loss_price * 0.99)
            limit_price = self._adjust_price_precision(self.take_profit_price)
            
            # 下OCO订单
            response = self.binance_client.new_oco_order(
                symbol=self.symbol,
                side="SELL",  # 卖出资产
                quantity=quantity,
                price=limit_price,  # 止盈价格
                stopPrice=stop_price,  # 止损触发价
                stopLimitPrice=stop_limit_price,  # 止损限价
                stopLimitTimeInForce="GTC"  # Good Till Cancel
            )
            
            self.oco_order_id = response.get('orderListId')
            
            message = (f"已设置风险管理OCO订单\n"
                      f"数量: {quantity} {asset}\n"
                      f"止损触发价: {stop_price}\n"
                      f"止盈价: {limit_price}")
            self.logger.info(message)
            if self.telegram_bot:
                self.telegram_bot.send_message(message)
                
        except Exception as e:
            self.logger.error(f"创建OCO订单失败: {e}")

    def _cancel_oco_orders(self):
        """取消现有OCO订单"""
        if not self.oco_order_id:
            return
            
        try:
            self.binance_client.client.cancel_oco_order(
                symbol=self.symbol,
                orderListId=self.oco_order_id
            )
            self.logger.info(f"已取消OCO订单 ID: {self.oco_order_id}")
            self.oco_order_id = None
        except Exception as e:
            # 忽略找不到订单的错误，可能已经执行或取消
            self.logger.warning(f"取消OCO订单失败: {e}")
            self.oco_order_id = None

    def execute_stop_loss(self):
        """执行止损操作"""
        try:
            # 获取账户余额，平掉所有仓位
            balance = self.binance_client.get_account_info()
            asset = self.symbol.replace('USDT', '')
            
            # 查找对应资产余额
            asset_balance = None
            for item in balance['balances']:
                if item['asset'] == asset:
                    asset_balance = float(item['free'])
                    break
            
            if asset_balance and asset_balance > 0:
                # 格式化数量
                formatted_quantity = self._adjust_quantity_precision(asset_balance)
                
                # 市价卖出全部资产
                self.binance_client.place_market_order(self.symbol, "SELL", formatted_quantity)
                message = f"执行止损: 已市价卖出 {formatted_quantity} {asset}"
                self.logger.info(message)
                if self.telegram_bot:
                    self.telegram_bot.send_message(message)
        except Exception as e:
            self.logger.error(f"执行止损失败: {e}")

    def execute_take_profit(self):
        """执行止盈操作"""
        try:
            # 获取账户余额，平掉所有仓位
            balance = self.binance_client.get_account_info()
            asset = self.symbol.replace('USDT', '')
            
            # 查找对应资产余额
            asset_balance = None
            for item in balance['balances']:
                if item['asset'] == asset:
                    asset_balance = float(item['free'])
                    break
            
            if asset_balance and asset_balance > 0:
                # 格式化数量
                formatted_quantity = self._adjust_quantity_precision(asset_balance)
                
                # 市价卖出全部资产
                self.binance_client.place_market_order(self.symbol, "SELL", formatted_quantity)
                message = f"执行止盈: 已市价卖出 {formatted_quantity} {asset}"
                self.logger.info(message)
                if self.telegram_bot:
                    self.telegram_bot.send_message(message)
        except Exception as e:
            self.logger.error(f"执行止盈失败: {e}")