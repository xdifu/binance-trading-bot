import logging
import time
import config
from datetime import datetime
from utils.format_utils import format_price, format_quantity, get_precision_from_filters

class RiskManager:
    def __init__(self, binance_client, telegram_bot=None):
        """
        Initialize risk management system
        
        Args:
            binance_client: BinanceClient instance for API operations
            telegram_bot: Optional TelegramBot instance for notifications
        """
        self.binance_client = binance_client
        self.telegram_bot = telegram_bot
        self.symbol = config.SYMBOL
        self.trailing_stop_loss_percent = config.TRAILING_STOP_LOSS_PERCENT / 100  # Convert to decimal
        self.trailing_take_profit_percent = config.TRAILING_TAKE_PROFIT_PERCENT / 100  # Convert to decimal
        
        # Initialize logger
        self.logger = logging.getLogger(__name__)
        
        # Track connection type for logging
        self.using_websocket = self.binance_client.get_client_status()["websocket_available"]
        self.logger.info(f"Using WebSocket API for risk management: {self.using_websocket}")
        
        # Get symbol information and set precision
        self.symbol_info = self._get_symbol_info()
        self.price_precision = self._get_price_precision()
        self.quantity_precision = self._get_quantity_precision()
        
        # Price tracking variables
        self.stop_loss_price = None
        self.highest_price = None
        self.lowest_price = None
        self.take_profit_price = None
        self.oco_order_id = None
        self.is_active = False
        
        # Update thresholds to reduce API calls - configurable if needed
        self.min_update_threshold_percent = 0.01  # 1% minimum price movement
        self.min_update_interval_seconds = 600    # 10 minutes minimum between updates
        
        # Track last update time
        self.last_update_time = 0
        
        # Track pending operations for better error handling
        self.pending_oco_orders = {}

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
        
    def activate(self, grid_lowest, grid_highest):
        """
        Activate risk management system
        
        Args:
            grid_lowest (float): Lowest grid price
            grid_highest (float): Highest grid price
        """
        try:
            # Check WebSocket availability before proceeding
            client_status = self.binance_client.get_client_status()
            self.using_websocket = client_status["websocket_available"]
            api_type = "WebSocket API" if self.using_websocket else "REST API"
            
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
            
            message = (f"Risk Management Activated via {api_type}\n"
                       f"Current price: {current_price}\n"
                       f"Stop loss price: {self.stop_loss_price}\n"
                       f"Take profit price: {self.take_profit_price}")
            
            self.logger.info(message)
            if self.telegram_bot:
                self.telegram_bot.send_message(message)
            
            self.is_active = True
            
            # Create initial OCO order
            self._place_oco_orders()
            
        except Exception as e:
            self.logger.error(f"Failed to activate risk management: {e}")
            
            # If this was a WebSocket error, try once more with REST
            if "connection" in str(e).lower() and self.using_websocket:
                self.logger.warning("Connection issue detected, falling back to REST API...")
                
                try:
                    # Force client to update connection status
                    client_status = self.binance_client.get_client_status()
                    self.using_websocket = client_status["websocket_available"]
                    
                    # Try again - will use REST if WebSocket is now unavailable
                    self._retry_activate(grid_lowest, grid_highest)
                except Exception as retry_error:
                    self.logger.error(f"Fallback activation also failed: {retry_error}")
    
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
        """Deactivate risk management system"""
        if not self.is_active:
            return
        
        try:
            # Cancel existing OCO orders
            self._cancel_oco_orders()
            
            self.is_active = False
            self.stop_loss_price = None
            self.take_profit_price = None
            self.highest_price = None
            self.lowest_price = None
            self.oco_order_id = None
            self.pending_oco_orders = {}
            self.last_update_time = 0
            
            message = "Risk Management Deactivated"
            self.logger.info(message)
            if self.telegram_bot:
                self.telegram_bot.send_message(message)
        except Exception as e:
            self.logger.error(f"Failed to deactivate risk management: {e}")
            
            # If this was a WebSocket error, try once more with REST
            if "connection" in str(e).lower() and self.using_websocket:
                self.logger.warning("Connection issue during deactivation, falling back to REST API...")
                
                try:
                    # Force client to update connection status
                    client_status = self.binance_client.get_client_status()
                    self.using_websocket = client_status["websocket_available"]
                    
                    # Reset OCO tracking
                    self.oco_order_id = None
                    self.pending_oco_orders = {}
                    
                    # Complete deactivation
                    self.is_active = False
                    self.stop_loss_price = None
                    self.take_profit_price = None
                    self.highest_price = None
                    self.lowest_price = None
                    
                    message = "Risk Management Deactivated (REST fallback)"
                    self.logger.info(message)
                    if self.telegram_bot:
                        self.telegram_bot.send_message(message)
                except Exception as retry_error:
                    self.logger.error(f"Fallback deactivation also failed: {retry_error}")

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
            
            # Critical fix: Always refresh current price first to ensure we use the correct symbol
            current_price = float(self.binance_client.get_symbol_price(self.symbol))
            self.logger.info(f"Current price for {self.symbol}: {current_price}")
            
            # Update highest/lowest price tracking with current price to prevent stale data
            if not self.highest_price or current_price > self.highest_price:
                self.highest_price = current_price
            if not self.lowest_price or current_price < self.lowest_price:
                self.lowest_price = current_price
            
            # Recalculate stop loss and take profit based on current price
            safety_margin = 0.01  # 1% safety margin
            self.stop_loss_price = current_price * (1 - self.trailing_stop_loss_percent)
            self.take_profit_price = current_price * (1 + self.trailing_take_profit_percent)
            
            # Get account balance - Always use current symbol to determine the asset
            balance = self.binance_client.get_account_info()
            asset = self.symbol.replace('USDT', '')  # Ensure we get the correct asset for current symbol
            
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
            
            # Get symbol info for price filter constraints
            symbol_info = self.binance_client.get_symbol_info(self.symbol)
            
            # Initialize price range variables
            min_price = current_price * 0.85  # Default: 15% below current price 
            max_price = current_price * 1.15  # Default: 15% above current price
            
            # Get correct price filters based on symbol_info
            if symbol_info and 'filters' in symbol_info:
                for filter_item in symbol_info['filters']:
                    if filter_item['filterType'] == 'PERCENT_PRICE_BY_SIDE':
                        # This filter is most relevant for OCO orders
                        bid_multiplier_up = float(filter_item.get('bidMultiplierUp', 1.2))
                        ask_multiplier_down = float(filter_item.get('askMultiplierDown', 0.8))
                        
                        # Apply specific multipliers for OCO with SELL side
                        max_price = current_price * bid_multiplier_up
                        min_price = current_price * ask_multiplier_down
                        
                        self.logger.info(f"Applied PERCENT_PRICE_BY_SIDE filter: {min_price:.4f} to {max_price:.4f}")
                        break
                    elif filter_item['filterType'] == 'PERCENT_PRICE':
                        # Generic percent price filter
                        multiplier_up = float(filter_item.get('multiplierUp', 1.2))
                        multiplier_down = float(filter_item.get('multiplierDown', 0.8))
                        max_price = current_price * multiplier_up
                        min_price = current_price * multiplier_down
            
            # Apply additional safety margin to avoid edge cases
            safe_max = max_price * 0.95  # Stay 5% below maximum
            safe_min = min_price * 1.05  # Stay 5% above minimum
            
            # Critical fix: Ensure stop price < current price < limit price for SELL orders
            # This is required by Binance OCO order rules
            if self.stop_loss_price >= current_price or self.stop_loss_price < safe_min:
                old_stop = self.stop_loss_price
                self.stop_loss_price = min(current_price * 0.95, safe_min)
                self.logger.warning(f"Adjusted stop loss from {old_stop:.4f} to {self.stop_loss_price:.4f} to comply with price rules")
            
            if self.take_profit_price <= current_price or self.take_profit_price > safe_max:
                old_take = self.take_profit_price
                self.take_profit_price = max(current_price * 1.05, safe_max)
                self.logger.warning(f"Adjusted take profit from {old_take:.4f} to {self.take_profit_price:.4f} to comply with price rules")
            
            # Format prices correctly
            stop_price = self._adjust_price_precision(self.stop_loss_price)
            stop_limit_price = self._adjust_price_precision(self.stop_loss_price * 0.99)
            limit_price = self._adjust_price_precision(self.take_profit_price)
            
            # Final safety check - ensure stop < current < limit for SELL OCO orders
            current_price_str = self._adjust_price_precision(current_price)
            self.logger.info(f"OCO price check: stop ({stop_price}) < current ({current_price_str}) < limit ({limit_price})")
            
            if float(stop_price) >= float(current_price_str) or float(limit_price) <= float(current_price_str):
                self.logger.error(f"Invalid OCO price relationship: stop ({stop_price}) < current ({current_price_str}) < limit ({limit_price})")
                if self.telegram_bot:
                    self.telegram_bot.send_message(f"⚠️ Cannot create OCO order: invalid price relationship")
                return False
            
            self.logger.info(f"Placing OCO order via {api_type}: Stop: {stop_price}, Limit: {limit_price}, Qty: {quantity}")
            
            # Place OCO order with standard parameters
            response = self.binance_client.new_oco_order(
                symbol=self.symbol,
                side="SELL",  # Sell assets
                quantity=quantity,
                price=limit_price,  # Take profit price
                stopPrice=stop_price,  # Stop loss trigger price
                stopLimitPrice=stop_limit_price,  # Stop limit price
                stopLimitTimeInForce="GTC"  # Good Till Cancel
            )
            
            # Check if the response indicates an error
            if isinstance(response, dict):
                if not response.get('success', True) or 'error' in response:
                    error_info = response.get('error', {})
                    error_code = error_info.get('code', 'Unknown')
                    error_msg = error_info.get('msg', 'Unknown error')
                    self.logger.error(f"Error {error_code}: {error_msg}")
                    
                    # Send notification about the failure
                    if self.telegram_bot:
                        self.telegram_bot.send_message(
                            f"⚠️ Failed to create risk management orders: {error_msg}"
                        )
                    return False
                
                # Handle different response formats from WebSocket vs REST
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
                return True
            else:
                self.logger.error(f"Invalid response format from OCO order creation")
                return False
                
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
                    return self._retry_place_oco_orders()
                except Exception as retry_error:
                    self.logger.error(f"Fallback OCO order placement also failed: {retry_error}")
                    return False
            return False

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
            
            if hasattr(self.binance_client, 'ws_client') and self.using_websocket:
                # Using WebSocket API client
                if hasattr(self.binance_client.ws_client.client, 'cancel_oco_order'):
                    response = self.binance_client.ws_client.client.cancel_oco_order(
                        symbol=self.symbol,
                        orderListId=self.oco_order_id
                    )
                else:
                    # Fallback to REST client
                    response = self.binance_client.rest_client.cancel_oco_order(  # 修改此处：cancel_order_list -> cancel_oco_order
                        symbol=self.symbol,
                        orderListId=self.oco_order_id
                    )
            else:
                # Using REST client directly
                response = self.binance_client.rest_client.cancel_oco_order(  # 修改此处：cancel_order_list -> cancel_oco_order
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
                
                # If this was a WebSocket error, try once more with REST
                if "connection" in str(e).lower() and self.using_websocket:
                    try:
                        # Force client to update connection status
                        client_status = self.binance_client.get_client_status()
                        self.using_websocket = client_status["websocket_available"]
                        
                        # Try again with REST client directly
                        self.binance_client.rest_client.cancel_oco_order(  # 修改此处：cancel_order_list -> cancel_oco_order
                            symbol=self.symbol,
                            orderListId=self.oco_order_id
                        )
                        
                        self.logger.info(f"Cancelled OCO order ID: {self.oco_order_id} via REST fallback")
                        self.oco_order_id = None
                        self.pending_oco_orders = {}
                    except Exception as retry_error:
                        if "not found" in str(retry_error).lower() or "unknown" in str(retry_error).lower():
                            self.logger.warning(f"OCO order {self.oco_order_id} not found during fallback attempt")
                            self.oco_order_id = None 
                            self.pending_oco_orders = {}
                        else:
                            self.logger.error(f"Fallback OCO order cancellation also failed: {retry_error}")

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