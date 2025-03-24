import logging
import time
import config
from datetime import datetime
from utils.format_utils import format_price, format_quantity, get_precision_from_filters

class RiskManager:
    def __init__(self, binance_client, telegram_bot=None, asset_manager=None):
        """初始化风险管理模块"""
        self.binance_client = binance_client
        self.telegram_bot = telegram_bot
        self.asset_manager = asset_manager
        
        # 配置参数初始化
        self.symbol = config.SYMBOL
        self.base_asset = self.symbol.replace('USDT', '')
        
        # 风险参数配置
        self.stop_loss_pct = config.STOP_LOSS_PCT / 100
        self.take_profit_pct = config.TAKE_PROFIT_PCT / 100
        self.capital_at_risk_pct = config.CAPITAL_AT_RISK_PCT / 100
        
        # 添加缺失的属性
        self.is_active = False  # 风险管理启用状态
        self.trailing_stop_loss_percent = config.TRAILING_STOP_LOSS_PERCENT / 100  # 追踪止损百分比
        self.trailing_take_profit_percent = config.TRAILING_TAKE_PROFIT_PERCENT / 100  # 追踪止盈百分比
        
        # 设置精度信息
        if self.asset_manager:
            self.price_precision = self.asset_manager.price_precision
            self.quantity_precision = self.asset_manager.quantity_precision
        else:
            self.price_precision = self._get_price_precision()
            self.quantity_precision = self._get_quantity_precision()
        
        # 初始化日志
        self.logger = logging.getLogger(__name__)
        
        # OCO订单跟踪状态
        self.oco_order_id = None
        self.pending_oco_orders = {}
        self.last_price = None
        self.last_update_time = 0
        
        # 设置更新阈值参数
        self.min_update_threshold_percent = config.RISK_UPDATE_THRESHOLD_PERCENT / 100
        self.min_update_interval_seconds = config.RISK_UPDATE_INTERVAL_MINUTES * 60
        
        # 同步的波动率值
        self.volatility_factor = 0.08  # 默认8%
        
        # 获取账户余额
        self.balances = {}
        self.update_account_balances()
        
        # 检查WebSocket API可用性以便日志记录
        client_status = self.binance_client.get_client_status()
        self.using_websocket = client_status["websocket_available"]
        self.logger.info(f"Using WebSocket API for risk management: {self.using_websocket}")

    def _get_symbol_info(self):
        """
        Get symbol information with connection status tracking
        
        Returns:
            dict: Symbol information or None if error
        """
        try:
            # Check which client is being used
            client_status = self.binance_client.get_client_status()
            self.using_websocket = client_status["websocket_available"]
            
            # Get symbol info using the appropriate client
            symbol_info = self.binance_client.get_symbol_info(self.symbol)
            
            # Log which API was used
            api_type = "WebSocket API" if self.using_websocket else "REST API"
            self.logger.debug(f"Retrieved symbol info via {api_type}")
            
            return symbol_info
        except Exception as e:
            self.logger.error(f"Failed to get symbol info: {e}")
            return None

    def _get_price_precision(self):
        """
        Get price precision from symbol info
        
        Returns:
            int: Price precision value
        """
        if not self.symbol_info or 'filters' not in self.symbol_info:
            return 2  # Default price precision
        return get_precision_from_filters(self.symbol_info['filters'], 'PRICE_FILTER', 'tickSize')
    
    def _get_quantity_precision(self):
        """
        Get quantity precision from symbol info
        
        Returns:
            int: Quantity precision value
        """
        if not self.symbol_info or 'filters' not in self.symbol_info:
            return 5  # Default quantity precision
        return get_precision_from_filters(self.symbol_info['filters'], 'LOT_SIZE', 'stepSize')
        
    def _adjust_price_precision(self, price):
        """
        Format price with appropriate precision
        
        Args:
            price (float): Original price value
            
        Returns:
            str: Formatted price string
        """
        return format_price(price, self.price_precision)
    
    def _adjust_quantity_precision(self, quantity):
        """
        Format quantity with appropriate precision
        
        Args:
            quantity (float): Original quantity value
            
        Returns:
            str: Formatted quantity string
        """
        return format_quantity(quantity, self.quantity_precision)
        
    def activate(self, min_price=None, max_price=None):
        """激活风险管理模块
        
        Args:
            min_price (float, optional): 网格最小价格
            max_price (float, optional): 网格最大价格
        
        Returns:
            bool: 激活是否成功
        """
        try:
            # 获取当前价格
            current_price = self.get_current_price(force_refresh=True)
            
            if not current_price:
                self.logger.error("无法获取当前价格，无法激活风险管理")
                return False
            
            # 如果提供了价格范围，可以使用它们来设置更精确的止损止盈水平
            if min_price is not None and max_price is not None:
                self.logger.debug(f"使用提供的价格范围: {min_price:.4f} - {max_price:.4f}")
                # 例如，可以设置止损在网格下方一定比例
                grid_range = max_price - min_price
                stop_price = current_price * (1 - self.trailing_stop_loss_percent)
                # 确保止损不会太接近网格
                stop_price = min(stop_price, min_price - (grid_range * 0.1))
                
                take_profit = current_price * (1 + self.trailing_take_profit_percent)
                # 确保止盈不会太接近网格
                take_profit = max(take_profit, max_price + (grid_range * 0.1))
            else:
                # 使用默认的百分比计算
                stop_price = current_price * (1 - self.trailing_stop_loss_percent)
                take_profit = current_price * (1 + self.trailing_take_profit_percent)
            
            # 格式化为适当精度
            stop_price = float(format_price(stop_price, self.price_precision))
            take_profit = float(format_price(take_profit, self.price_precision))
            
            # 创建OCO订单
            result = self._place_oco_order(stop_price, take_profit)
            
            if result:
                self.is_active = True
                self.logger.info(f"Risk Management Activated via {'WebSocket API' if self.using_websocket else 'REST API'}")
                self.logger.info(f"Current price: {current_price}")
                self.logger.info(f"Stop loss price: {stop_price}")
                self.logger.info(f"Take profit price: {take_profit}")
                return True
            
            return False
        except Exception as e:
            self.logger.error(f"Failed to activate risk management: {e}")
            return False
    
    def _retry_activate(self, grid_lowest, grid_highest):
        """
        Retry activation with updated client status
        
        Args:
            grid_lowest (float): Lowest grid price
            grid_highest (float): Highest grid price
        """
        # Get current price
        current_price = self.binance_client.get_symbol_price(self.symbol)
        
        # Set initial stop loss and take profit prices
        self.stop_loss_price = grid_lowest * (1 - self.trailing_stop_loss_percent)
        self.take_profit_price = grid_highest * (1 + self.trailing_take_profit_percent)
        
        # Set historical high and low prices
        self.highest_price = current_price
        self.lowest_price = current_price
        
        # Initialize last update time
        self.last_update_time = time.time()
        
        message = (f"Risk Management Activated via REST fallback\n"
                   f"Current price: {current_price}\n"
                   f"Stop loss price: {self.stop_loss_price}\n"
                   f"Take profit price: {self.take_profit_price}")
        
        self.logger.info(message)
        if self.telegram_bot:
            self.telegram_bot.send_message(message)
        
        self.is_active = True
        
        # Create initial OCO order
        self._place_oco_orders()
    
    def deactivate(self):
        """停用风险管理模块"""
        if not self.is_active:
            self.logger.debug("Risk Management already inactive")
            return True
            
        try:
            # 取消所有OCO订单
            self._cancel_oco_orders()
            
            # 更新状态
            self.is_active = False
            
            self.logger.info("Risk Management Deactivated")
            return True
        except Exception as e:
            self.logger.error(f"Failed to deactivate risk management: {e}")
            return False

    def check_price(self, current_price):
        """
        Check if price requires updating trailing stop loss or take profit
        
        Args:
            current_price (float): Current market price
            
        Returns:
            None
        """
        if not self.is_active:
            return None
        
        try:
            update_required = False
            update_reason = ""
            
            # Always track price history (lightweight operation)
            if current_price > self.highest_price:
                old_highest = self.highest_price
                self.highest_price = current_price
                
                # Calculate potential new stop loss and take profit prices
                new_stop_loss = current_price * (1 - self.trailing_stop_loss_percent)
                new_take_profit = current_price * (1 + self.trailing_take_profit_percent)
                
                # Check if price change is significant enough (threshold-based)
                stop_loss_percent_change = (new_stop_loss - self.stop_loss_price) / self.stop_loss_price
                take_profit_percent_change = (new_take_profit - self.take_profit_price) / self.take_profit_price
                
                # Only update if price change exceeds threshold AND enough time has passed
                current_time = time.time()
                time_since_update = current_time - self.last_update_time
                
                if (stop_loss_percent_change > self.min_update_threshold_percent or 
                    take_profit_percent_change > self.min_update_threshold_percent):
                    # Price threshold condition met
                    
                    if time_since_update >= self.min_update_interval_seconds:
                        # Update stop loss if new value is higher (proper trailing behavior)
                        if new_stop_loss > self.stop_loss_price:
                            self.stop_loss_price = new_stop_loss
                            update_required = True
                            update_reason += "Stop loss increased significantly. "
                        
                        # Update take profit if new value is higher (trailing behavior)
                        if new_take_profit > self.take_profit_price:
                            self.take_profit_price = new_take_profit
                            update_required = True
                            update_reason += "Take profit increased significantly."
                        
                        if update_required:
                            self.last_update_time = current_time
                            
                            # Log the significant price change with details
                            price_change_percent = (current_price - old_highest) / old_highest * 100
                            self.logger.info(
                                f"Significant price movement: {price_change_percent:.2f}%. "
                                f"New high: {current_price}, Previous: {old_highest}. "
                                f"Updating OCO orders: {update_reason}"
                            )
                    else:
                        # Log that we detected a significant change but cooling period is active
                        self.logger.debug(
                            f"Price change detected but cooling period active. "
                            f"Time since last update: {time_since_update:.1f}s, "
                            f"Required: {self.min_update_interval_seconds}s"
                        )
            
            # Always track lowest price for analysis
            if current_price < self.lowest_price:
                self.lowest_price = current_price
            
            # Update orders if needed and we have an active OCO
            if update_required and self.oco_order_id:
                self._cancel_oco_orders()
                self._place_oco_orders()
                
            return None  # No direct operation is returned, executed via OCO orders
            
        except Exception as e:
            self.logger.error(f"Error in check_price: {e}")
            return None

    def _place_oco_orders(self):
        """Create OCO (One-Cancels-the-Other) order pair for stop loss and take profit"""
        try:
            # Check WebSocket availability before proceeding
            client_status = self.binance_client.get_client_status()
            self.using_websocket = client_status["websocket_available"]
            api_type = "WebSocket API" if self.using_websocket else "REST API"
            
            # Get account balance
            balance = self.binance_client.get_account_info()
            asset = self.symbol.replace('USDT', '')
            
            # Find corresponding asset balance - handle different response formats
            asset_balance = None
            
            if isinstance(balance, dict):
                if 'balances' in balance:
                    # REST API format
                    for item in balance['balances']:
                        if item['asset'] == asset:
                            asset_balance = float(item['free'])
                            break
                elif 'result' in balance and 'balances' in balance['result']:
                    # WebSocket API format
                    for item in balance['result']['balances']:
                        if item['asset'] == asset:
                            asset_balance = float(item['free'])
                            break
            
            if not asset_balance or asset_balance <= 0:
                self.logger.info(f"No available {asset} for risk management")
                return
            
            # Set quantity (all available assets) and format
            quantity = self._adjust_quantity_precision(asset_balance)
            
            # Get current price
            current_price = self.binance_client.get_symbol_price(self.symbol)
            
            # Format stop loss and take profit prices
            stop_price = self._adjust_price_precision(self.stop_loss_price)
            stop_limit_price = self._adjust_price_precision(self.stop_loss_price * 0.99)
            limit_price = self._adjust_price_precision(self.take_profit_price)
            
            self.logger.info(f"Placing OCO order via {api_type}: Stop: {stop_price}, Limit: {limit_price}, Qty: {quantity}")
            
            # Place OCO order with standard parameters (removed aboveType and belowType)
            response = self.binance_client.new_oco_order(
                symbol=self.symbol,
                side="SELL",  # Sell assets
                quantity=quantity,
                price=limit_price,  # Take profit price
                stopPrice=stop_price,  # Stop loss trigger price
                stopLimitPrice=stop_limit_price,  # Stop limit price
                stopLimitTimeInForce="GTC"  # Good Till Cancel
            )
            
            # Handle different response formats from WebSocket vs REST
            if isinstance(response, dict):
                if 'orderListId' in response:
                    # REST API format
                    self.oco_order_id = response['orderListId']
                elif 'result' in response and 'orderListId' in response['result']:
                    # WebSocket API format
                    self.oco_order_id = response['result']['orderListId']
                    
                # Add to pending orders tracking
                self.pending_oco_orders[str(self.oco_order_id)] = {
                    'stop_price': float(stop_price),
                    'limit_price': float(limit_price),
                    'quantity': float(quantity),
                    'timestamp': int(time.time())
                }
            
            message = (f"Risk Management OCO Order Created via {api_type}\n"
                      f"Quantity: {quantity} {asset}\n"
                      f"Stop loss trigger: {stop_price}\n"
                      f"Take profit price: {limit_price}")
            self.logger.info(message)
            if self.telegram_bot:
                self.telegram_bot.send_message(message)
                
        except Exception as e:
            self.logger.error(f"Failed to create OCO order: {e}")
            
            # If this was a WebSocket error, try once more with REST
            if "connection" in str(e).lower() and self.using_websocket:
                self.logger.warning("Connection issue during OCO order placement, falling back to REST API...")
                
                try:
                    # Force client to update connection status
                    client_status = self.binance_client.get_client_status()
                    self.using_websocket = client_status["websocket_available"]
                    
                    # Try again - will use REST if WebSocket is now unavailable
                    self._retry_place_oco_orders()
                except Exception as retry_error:
                    self.logger.error(f"Fallback OCO order placement also failed: {retry_error}")

    def _retry_place_oco_orders(self):
        """Retry placing OCO orders with updated client status"""
        # Get account balance
        balance = self.binance_client.get_account_info()
        asset = self.symbol.replace('USDT', '')
        
        # Find corresponding asset balance - handle different response formats
        asset_balance = None
        
        if isinstance(balance, dict):
            if 'balances' in balance:
                # REST API format
                for item in balance['balances']:
                    if item['asset'] == asset:
                        asset_balance = float(item['free'])
                        break
            elif 'result' in balance and 'balances' in balance['result']:
                # WebSocket API format
                for item in balance['result']['balances']:
                    if item['asset'] == asset:
                        asset_balance = float(item['free'])
                        break
        
        if not asset_balance or asset_balance <= 0:
            self.logger.info(f"No available {asset} for risk management")
            return
        
        # Set quantity (all available assets) and format
        quantity = self._adjust_quantity_precision(asset_balance)
        
        # Format stop loss and take profit prices
        stop_price = self._adjust_price_precision(self.stop_loss_price)
        stop_limit_price = self._adjust_price_precision(self.stop_loss_price * 0.99)
        limit_price = self._adjust_price_precision(self.take_profit_price)
        
        self.logger.info(f"Placing OCO order via REST fallback: Stop: {stop_price}, Limit: {limit_price}, Qty: {quantity}")
        
        # Place OCO order with standard parameters (removed aboveType and belowType)
        response = self.binance_client.new_oco_order(
            symbol=self.symbol,
            side="SELL",
            quantity=quantity,
            price=limit_price,
            stopPrice=stop_price,
            stopLimitPrice=stop_limit_price,
            stopLimitTimeInForce="GTC"
        )
        
        # Handle response
        if isinstance(response, dict):
            if 'orderListId' in response:
                self.oco_order_id = response['orderListId']
            elif 'result' in response and 'orderListId' in response['result']:
                self.oco_order_id = response['result']['orderListId']
        
        message = (f"Risk Management OCO Order Created via REST fallback\n"
                  f"Quantity: {quantity} {asset}\n"
                  f"Stop loss trigger: {stop_price}\n"
                  f"Take profit price: {limit_price}")
        self.logger.info(message)
        if self.telegram_bot:
            self.telegram_bot.send_message(message)

    def _cancel_oco_orders(self):
        """Cancel existing OCO orders"""
        if not self.oco_order_id:
            return
            
        try:
            # Check WebSocket availability before proceeding
            client_status = self.binance_client.get_client_status()
            self.using_websocket = client_status["websocket_available"]
            api_type = "WebSocket API" if self.using_websocket else "REST API"
            
            self.logger.info(f"Cancelling OCO order {self.oco_order_id} via {api_type}")
            
            # 使用统一接口
            response = self.binance_client.cancel_oco_order(
                symbol=self.symbol,
                orderListId=self.oco_order_id
            )
            
            # Clear from pending orders tracking
            str_order_id = str(self.oco_order_id)
            if str_order_id in self.pending_oco_orders:
                self.pending_oco_orders.pop(str_order_id)
                
            self.logger.info(f"Cancelled OCO order ID: {self.oco_order_id}")
            self.oco_order_id = None
            
        except Exception as e:
            # Ignore errors for orders that can't be found - likely already executed or cancelled
            if "not found" in str(e).lower() or "unknown" in str(e).lower() or "does not exist" in str(e).lower():
                self.logger.warning(f"OCO order {self.oco_order_id} not found, likely already executed or cancelled")
                # Reset tracking
                self.oco_order_id = None
                self.pending_oco_orders = {}
            else:
                self.logger.warning(f"Failed to cancel OCO order: {e}")

    def execute_stop_loss(self):
        """Execute stop loss operation"""
        try:
            # Check WebSocket availability before proceeding
            client_status = self.binance_client.get_client_status()
            self.using_websocket = client_status["websocket_available"]
            api_type = "WebSocket API" if self.using_websocket else "REST API"
            
            # Get account balance and close all positions
            balance = self.binance_client.get_account_info()
            asset = self.symbol.replace('USDT', '')
            
            # Find corresponding asset balance - handle different response formats
            asset_balance = None
            
            if isinstance(balance, dict):
                if 'balances' in balance:
                    # REST API format
                    for item in balance['balances']:
                        if item['asset'] == asset:
                            asset_balance = float(item['free'])
                            break
                elif 'result' in balance and 'balances' in balance['result']:
                    # WebSocket API format
                    for item in balance['result']['balances']:
                        if item['asset'] == asset:
                            asset_balance = float(item['free'])
                            break
            
            if asset_balance and asset_balance > 0:
                # Format quantity
                formatted_quantity = self._adjust_quantity_precision(asset_balance)
                
                self.logger.info(f"Executing stop loss via {api_type}: Market selling {formatted_quantity} {asset}")
                
                # Market sell all assets
                self.binance_client.place_market_order(self.symbol, "SELL", formatted_quantity)
                message = f"Stop Loss Executed via {api_type}: Market sold {formatted_quantity} {asset}"
                self.logger.info(message)
                if self.telegram_bot:
                    self.telegram_bot.send_message(message)
            else:
                self.logger.warning(f"No {asset} balance available for stop loss")
                
        except Exception as e:
            self.logger.error(f"Failed to execute stop loss: {e}")
            
            # If this was a WebSocket error, try once more with REST
            if "connection" in str(e).lower() and self.using_websocket:
                self.logger.warning("Connection issue during stop loss execution, falling back to REST API...")
                
                try:
                    # Force client to update connection status
                    client_status = self.binance_client.get_client_status()
                    self.using_websocket = client_status["websocket_available"]
                    
                    # Try again with updated client status
                    self._retry_execute_stop_loss()
                except Exception as retry_error:
                    self.logger.error(f"Fallback stop loss execution also failed: {retry_error}")

    def _retry_execute_stop_loss(self):
        """Retry stop loss execution with updated client status"""
        # Get account balance and close all positions
        balance = self.binance_client.get_account_info()
        asset = self.symbol.replace('USDT', '')
        
        # Find corresponding asset balance - handle different response formats
        asset_balance = None
        
        if isinstance(balance, dict):
            if 'balances' in balance:
                # REST API format
                for item in balance['balances']:
                    if item['asset'] == asset:
                        asset_balance = float(item['free'])
                        break
            elif 'result' in balance and 'balances' in balance['result']:
                # WebSocket API format
                for item in balance['result']['balances']:
                    if item['asset'] == asset:
                        asset_balance = float(item['free'])
                        break
        
        if asset_balance and asset_balance > 0:
            # Format quantity
            formatted_quantity = self._adjust_quantity_precision(asset_balance)
            
            self.logger.info(f"Executing stop loss via REST fallback: Market selling {formatted_quantity} {asset}")
            
            # Market sell all assets
            self.binance_client.place_market_order(self.symbol, "SELL", formatted_quantity)
            message = f"Stop Loss Executed via REST fallback: Market sold {formatted_quantity} {asset}"
            self.logger.info(message)
            if self.telegram_bot:
                self.telegram_bot.send_message(message)

    def execute_take_profit(self):
        """Execute take profit operation"""
        try:
            # Check WebSocket availability before proceeding
            client_status = self.binance_client.get_client_status()
            self.using_websocket = client_status["websocket_available"]
            api_type = "WebSocket API" if self.using_websocket else "REST API"
            
            # Get account balance and close all positions
            balance = self.binance_client.get_account_info()
            asset = self.symbol.replace('USDT', '')
            
            # Find corresponding asset balance - handle different response formats
            asset_balance = None
            
            if isinstance(balance, dict):
                if 'balances' in balance:
                    # REST API format
                    for item in balance['balances']:
                        if item['asset'] == asset:
                            asset_balance = float(item['free'])
                            break
                elif 'result' in balance and 'balances' in balance['result']:
                    # WebSocket API format
                    for item in balance['result']['balances']:
                        if item['asset'] == asset:
                            asset_balance = float(item['free'])
                            break
            
            if asset_balance and asset_balance > 0:
                # Format quantity
                formatted_quantity = self._adjust_quantity_precision(asset_balance)
                
                self.logger.info(f"Executing take profit via {api_type}: Market selling {formatted_quantity} {asset}")
                
                # Market sell all assets
                self.binance_client.place_market_order(self.symbol, "SELL", formatted_quantity)
                message = f"Take Profit Executed via {api_type}: Market sold {formatted_quantity} {asset}"
                self.logger.info(message)
                if self.telegram_bot:
                    self.telegram_bot.send_message(message)
            else:
                self.logger.warning(f"No {asset} balance available for take profit")
                
        except Exception as e:
            self.logger.error(f"Failed to execute take profit: {e}")
            
            # If this was a WebSocket error, try once more with REST
            if "connection" in str(e).lower() and self.using_websocket:
                self.logger.warning("Connection issue during take profit execution, falling back to REST API...")
                
                try:
                    # Force client to update connection status
                    client_status = self.binance_client.get_client_status()
                    self.using_websocket = client_status["websocket_available"]
                    
                    # Try again with updated client status
                    self._retry_execute_take_profit()
                except Exception as retry_error:
                    self.logger.error(f"Fallback take profit execution also failed: {retry_error}")

    def _retry_execute_take_profit(self):
        """Retry take profit execution with updated client status"""
        # Get account balance and close all positions
        balance = self.binance_client.get_account_info()
        asset = self.symbol.replace('USDT', '')
        
        # Find corresponding asset balance - handle different response formats
        asset_balance = None
        
        if isinstance(balance, dict):
            if 'balances' in balance:
                # REST API format
                for item in balance['balances']:
                    if item['asset'] == asset:
                        asset_balance = float(item['free'])
                        break
            elif 'result' in balance and 'balances' in balance['result']:
                # WebSocket API format
                for item in balance['result']['balances']:
                    if item['asset'] == asset:
                        asset_balance = float(item['free'])
                        break
        
        if asset_balance and asset_balance > 0:
            # Format quantity
            formatted_quantity = self._adjust_quantity_precision(asset_balance)
            
            self.logger.info(f"Executing take profit via REST fallback: Market selling {formatted_quantity} {asset}")
            
            # Market sell all assets
            self.binance_client.place_market_order(self.symbol, "SELL", formatted_quantity)
            message = f"Take Profit Executed via REST fallback: Market sold {formatted_quantity} {asset}"
            self.logger.info(message)
            if self.telegram_bot:
                self.telegram_bot.send_message(message)
                
    def get_status(self):
        """
        Get current risk management status
        
        Returns:
            str: Status message with details
        """
        if not self.is_active:
            return "Risk management is inactive"
            
        # Check WebSocket availability
        client_status = self.binance_client.get_client_status()
        api_type = "WebSocket API" if client_status["websocket_available"] else "REST API"
            
        status_text = (
            f"Risk Management Status (using {api_type}):\n"
            f"Stop loss price: {self._adjust_price_precision(self.stop_loss_price)}\n"
            f"Take profit price: {self._adjust_price_precision(self.take_profit_price)}\n"
            f"Historical high: {self._adjust_price_precision(self.highest_price)}\n"
            f"Historical low: {self._adjust_price_precision(self.lowest_price)}\n"
            f"Update thresholds: {self.min_update_threshold_percent*100}% price change, "
            f"{self.min_update_interval_seconds/60} min interval"
        )
        
        if self.oco_order_id:
            status_text += f"\nActive OCO order ID: {self.oco_order_id}"
            
        return status_text

    def update_thresholds(self, price_threshold=None, time_interval=None):
        """
        Update the thresholds used for OCO order updates
        
        Args:
            price_threshold (float): New price threshold as percentage (0.01 = 1%)
            time_interval (int): New time interval in minutes
            
        Returns:
            str: Status message with new threshold values
        """
        if price_threshold is not None:
            self.min_update_threshold_percent = max(0.001, min(0.1, price_threshold))
            
        if time_interval is not None:
            self.min_update_interval_seconds = max(60, min(3600, time_interval * 60))
            
        self.logger.info(
            f"Risk management thresholds updated: "
            f"{self.min_update_threshold_percent*100}% price change, "
            f"{self.min_update_interval_seconds/60} min interval"
        )
        
        return f"Risk management thresholds updated: {self.min_update_threshold_percent*100}% price change, " \
               f"{self.min_update_interval_seconds/60} min interval"

    def update_account_balances(self):
        """更新账户余额信息"""
        try:
            account_info = self.binance_client.get_account_info()
            
            # 处理不同API响应格式的差异
            if isinstance(account_info, dict):
                if 'balances' in account_info:
                    # REST API格式
                    balances_data = account_info['balances']
                elif 'result' in account_info and 'balances' in account_info['result']:
                    # WebSocket API格式
                    balances_data = account_info['result']['balances']
                else:
                    self.logger.warning("无法从账户信息中提取余额数据")
                    return
                
                # 更新余额字典
                for balance in balances_data:
                    asset = balance.get('asset')
                    free = float(balance.get('free', 0))
                    locked = float(balance.get('locked', 0))
                    
                    if asset:
                        self.balances[asset] = {
                            'free': free,
                            'locked': locked,
                            'total': free + locked
                        }
                
                self.logger.debug(f"已更新账户余额信息: {len(self.balances)}个资产")
            else:
                self.logger.warning(f"账户信息格式不正确: {type(account_info)}")
        except Exception as e:
            self.logger.error(f"更新账户余额失败: {e}")

    def update_symbol(self, new_symbol):
        """更新交易对符号和相关参数"""
        old_symbol = self.symbol
        self.symbol = new_symbol
        
        # 更新基础资产名称
        old_base_asset = self.base_asset  # 保存旧资产名称以便日志记录
        self.base_asset = new_symbol.replace('USDT', '')
        
        # 重置价格相关状态
        self.last_price = None
        
        # 重置OCO订单状态
        self.oco_order_id = None
        
        # 获取新交易对的精度信息
        self._set_precision_from_symbol_info()
        
        self.logger.info(f"Risk Manager updated symbol from {old_symbol} to {new_symbol}")
        self.logger.info(f"Base asset changed from {old_base_asset} to {self.base_asset}")
        
        # 如果风险管理已激活，需要取消旧OCO订单并创建新订单
        if self.is_active:
            self._cancel_oco_orders()  # 取消旧交易对的OCO订单
        
        return True

    def get_current_price(self, force_refresh=False):
        """获取当前价格，支持强制刷新"""
        try:
            # 如果强制刷新或者没有缓存价格，则从交易所获取
            if force_refresh or self.last_price is None:
                price_data = self.binance_client.get_symbol_price(self.symbol)
                if isinstance(price_data, dict):
                    self.last_price = float(price_data.get('price', 0))
                else:
                    self.last_price = float(price_data)
                    
                self.last_update_time = time.time()
                
            return self.last_price
        except Exception as e:
            self.logger.error(f"Error getting current price: {e}")
            return None