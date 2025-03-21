import logging
import math
import time
from datetime import datetime, timedelta
from binance_api.client import BinanceClient
from utils.indicators import calculate_atr
from utils.format_utils import format_price, format_quantity, get_precision_from_filters
import config

class GridTrader:
    def __init__(self, binance_client, telegram_bot=None):
        self.binance_client = binance_client
        self.telegram_bot = telegram_bot
        self.symbol = config.SYMBOL
        self.grid_levels = config.GRID_LEVELS
        self.grid_spacing = config.GRID_SPACING / 100  # 转换为小数
        self.capital_per_level = config.CAPITAL_PER_LEVEL
        self.grid_range_percent = config.GRID_RANGE_PERCENT / 100  # 转换为小数
        self.recalculation_period = config.RECALCULATION_PERIOD
        self.atr_period = config.ATR_PERIOD
        
        # 初始化logger (移到顶部)
        self.logger = logging.getLogger(__name__)
        
        # 获取交易对信息并设置精度
        self.symbol_info = self.binance_client.get_symbol_info(self.symbol)
        self.price_precision = self._get_price_precision()
        self.quantity_precision = self._get_quantity_precision()
        
        self.logger.info(f"交易对 {self.symbol} 价格精度: {self.price_precision}, 数量精度: {self.quantity_precision}")
        
        self.grid = []  # [{'price': float, 'order_id': int, 'side': str}]
        self.last_recalculation = None
        self.current_market_price = 0
        self.is_running = False
    
    def start(self):
        """启动网格交易系统"""
        if self.is_running:
            return "系统已在运行"
        
        self.is_running = True
        self.current_market_price = self.binance_client.get_symbol_price(self.symbol)
        self.last_recalculation = datetime.now()
        
        # 取消之前的所有挂单
        self._cancel_all_open_orders()
        
        # 计算并设置网格
        self._setup_grid()
        
        message = f"网格交易系统启动!\n当前价格: {self.current_market_price}\n网格范围: {len(self.grid)}个网格"
        self.logger.info(message)
        if self.telegram_bot:
            self.telegram_bot.send_message(message)
        
        return message
    
    def stop(self):
        """停止网格交易系统"""
        if not self.is_running:
            return "系统未运行"
        
        self._cancel_all_open_orders()
        self.grid = []
        self.is_running = False
        
        message = "网格交易系统已停止"
        self.logger.info(message)
        if self.telegram_bot:
            self.telegram_bot.send_message(message)
        
        return message
    
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
    
    def _setup_grid(self):
        """设置网格"""
        # 计算网格价位
        self.grid = self._calculate_grid_levels()
        
        # 下网格订单
        for level in self.grid:
            price = level['price']
            side = level['side']
            
            # 计算订单数量
            if side == 'BUY':
                quantity = self.capital_per_level / price
            else:  # SELL
                quantity = self.capital_per_level / price
            
            # 调整数量和价格精度
            formatted_quantity = self._adjust_quantity_precision(quantity)
            formatted_price = self._adjust_price_precision(price)
            
            try:
                # 下订单
                order = self.binance_client.place_limit_order(
                    self.symbol, 
                    side, 
                    formatted_quantity, 
                    formatted_price
                )
                
                level['order_id'] = order['orderId']
                self.logger.info(f"下单成功: {side} {formatted_quantity} @ {formatted_price}, ID: {order['orderId']}")
            except Exception as e:
                self.logger.error(f"下单失败: {side} {formatted_quantity} @ {formatted_price}, 错误: {e}")
    
    def _calculate_grid_levels(self):
        """计算网格价位"""
        grid_levels = []
        
        # 调整网格范围基于ATR
        atr = self._get_current_atr()
        if atr:
            # 根据ATR调整网格范围
            volatility_factor = atr / self.current_market_price
            adjusted_range = max(self.grid_range_percent, volatility_factor * 2)
        else:
            adjusted_range = self.grid_range_percent
        
        # 计算上下边界
        lower_bound = self.current_market_price * (1 - adjusted_range)
        upper_bound = self.current_market_price * (1 + adjusted_range)
        
        # 计算间距
        price_range = upper_bound - lower_bound
        level_spacing = price_range / (self.grid_levels - 1) if self.grid_levels > 1 else price_range
        
        # 创建网格
        for i in range(self.grid_levels):
            price = lower_bound + i * level_spacing
            # 买单在当前价格以下，卖单在当前价格以上
            side = "BUY" if price < self.current_market_price else "SELL"
            grid_levels.append({
                'price': price,
                'side': side,
                'order_id': None
            })
        
        return grid_levels
    
    def _get_current_atr(self):
        """获取当前ATR值"""
        try:
            # 获取历史K线数据
            klines = self.binance_client.get_historical_klines(
                self.symbol, 
                '1d', 
                limit=self.atr_period + 10  # 多获取一些数据以确保有足够样本
            )
            
            # 计算ATR
            atr = calculate_atr(klines, self.atr_period)
            return atr
        except Exception as e:
            self.logger.error(f"获取ATR失败: {e}")
            return None
    
    def _cancel_all_open_orders(self):
        """取消所有挂单"""
        try:
            open_orders = self.binance_client.get_open_orders(self.symbol)
            for order in open_orders:
                self.binance_client.cancel_order(self.symbol, order['orderId'])
                self.logger.info(f"已取消订单: {order['orderId']}")
        except Exception as e:
            self.logger.error(f"取消订单失败: {e}")
    
    def handle_order_update(self, order_data):
        """处理订单更新"""
        if not self.is_running:
            return
        
        # 处理订单完成事件
        if order_data['X'] == 'FILLED':
            order_id = order_data['i']
            symbol = order_data['s']
            side = order_data['S']
            price = float(order_data['p'])
            quantity = float(order_data['q'])
            
            if symbol != self.symbol:
                return
            
            # 找到对应的网格
            for i, level in enumerate(self.grid):
                if level['order_id'] == order_id:
                    self.logger.info(f"订单已成交: {side} {quantity} @ {price}")
                    
                    # 根据成交方向创建反向订单
                    new_side = "SELL" if side == "BUY" else "BUY"
                    
                    # 调整数量和价格精度
                    formatted_quantity = self._adjust_quantity_precision(quantity)
                    formatted_price = self._adjust_price_precision(price)
                    
                    try:
                        # 下反向订单
                        new_order = self.binance_client.place_limit_order(
                            self.symbol, 
                            new_side, 
                            formatted_quantity, 
                            formatted_price
                        )
                        
                        # 更新网格
                        self.grid[i]['side'] = new_side
                        self.grid[i]['order_id'] = new_order['orderId']
                        
                        message = f"网格交易执行: {side} {formatted_quantity} @ {formatted_price}\n已下反向单: {new_side} {formatted_quantity} @ {formatted_price}"
                        self.logger.info(message)
                        if self.telegram_bot:
                            self.telegram_bot.send_message(message)
                    except Exception as e:
                        self.logger.error(f"下反向订单失败: {e}")
                    break
    
    def check_grid_recalculation(self):
        """检查是否需要重新计算网格"""
        if not self.is_running or not self.last_recalculation:
            return
        
        days_since_last = (datetime.now() - self.last_recalculation).days
        
        if days_since_last >= self.recalculation_period:
            self.logger.info("开始重新计算网格...")
            
            # 更新当前市场价格
            self.current_market_price = self.binance_client.get_symbol_price(self.symbol)
            
            # 取消所有挂单
            self._cancel_all_open_orders()
            
            # 重新计算并设置网格
            self._setup_grid()
            
            self.last_recalculation = datetime.now()
            
            message = f"网格已重新计算!\n当前价格: {self.current_market_price}\n网格范围: {len(self.grid)}个网格"
            self.logger.info(message)
            if self.telegram_bot:
                self.telegram_bot.send_message(message)
    
    def get_status(self):
        """获取当前状态"""
        if not self.is_running:
            return "系统未运行"
        
        # 获取当前价格
        try:
            current_price = self.binance_client.get_symbol_price(self.symbol)
            
            # 构建状态消息
            grid_info = []
            for i, level in enumerate(self.grid):
                grid_info.append(f"网格 {i+1}: {level['side']} @ {level['price']:.2f}")
            
            status = f"网格交易状态:\n" \
                    f"交易对: {self.symbol}\n" \
                    f"当前价格: {current_price}\n" \
                    f"网格数量: {len(self.grid)}\n" \
                    f"上次调整: {self.last_recalculation}\n" \
                    f"网格详情:\n" + "\n".join(grid_info)
            
            return status
        except Exception as e:
            self.logger.error(f"获取状态失败: {e}")
            return f"获取状态失败: {e}"