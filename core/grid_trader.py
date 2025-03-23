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
        self.grid_spacing = config.GRID_SPACING / 100  # Convert to decimal
        self.capital_per_level = config.CAPITAL_PER_LEVEL
        self.grid_range_percent = config.GRID_RANGE_PERCENT / 100  # Convert to decimal
        self.recalculation_period = config.RECALCULATION_PERIOD
        self.atr_period = config.ATR_PERIOD
        
        # New: Store previous ATR value for volatility change detection
        self.last_atr_value = None
        
        # Initialize logger (moved to top)
        self.logger = logging.getLogger(__name__)
        
        # Get symbol information and set precision
        self.symbol_info = self._get_symbol_info()
        self.price_precision = self._get_price_precision()
        self.quantity_precision = self._get_quantity_precision()
        
        self.logger.info(f"Trading pair {self.symbol} price precision: {self.price_precision}, quantity precision: {self.quantity_precision}")
        
        # Track connection type for logging
        self.using_websocket = self.binance_client.get_client_status()["websocket_available"]
        self.logger.info(f"Using WebSocket API: {self.using_websocket}")
        
        self.grid = []  # [{'price': float, 'order_id': int, 'side': str}]
        self.last_recalculation = None
        self.current_market_price = 0
        self.is_running = False
        self.simulation_mode = False  # Add simulation mode flag
        
        # Track pending operations for better error handling
        self.pending_orders = {}  # Track orders waiting for WebSocket confirmation
    
    def _get_symbol_info(self):
        """Get symbol information with connection status tracking"""
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
    
    def start(self, simulation=False):
        """Start grid trading system"""
        if self.is_running:
            return "System already running"
        
        self.is_running = True
        self.simulation_mode = simulation
        
        # Update client status before starting
        client_status = self.binance_client.get_client_status()
        self.using_websocket = client_status["websocket_available"]
        api_type = "WebSocket API" if self.using_websocket else "REST API"
        self.logger.info(f"Starting grid trading using {api_type}")
        
        # Get current market price
        self.current_market_price = self.binance_client.get_symbol_price(self.symbol)
        self.last_recalculation = datetime.now()
        
        # Check balances before starting
        base_asset = self.symbol.replace('USDT', '')
        quote_asset = 'USDT'
        
        base_balance = self.binance_client.check_balance(base_asset)
        quote_balance = self.binance_client.check_balance(quote_asset)
        
        total_needed = self.capital_per_level * self.grid_levels
        
        if quote_balance < total_needed and not simulation:
            self.logger.warning(f"Insufficient {quote_asset} balance. Required: {total_needed}, Available: {quote_balance}")
            # Add warning in telegram message
            if self.telegram_bot:
                self.telegram_bot.send_message(f"⚠️ Warning: Insufficient {quote_asset} balance. Required: {total_needed}, Available: {quote_balance}\nStarting in limited mode.")
        
        # Cancel all previous open orders
        self._cancel_all_open_orders()
        
        # Calculate and set grid
        self._setup_grid()
        
        message = f"Grid trading system started!\nCurrent price: {self.current_market_price}\nGrid range: {len(self.grid)} levels\nUsing {api_type}"
        self.logger.info(message)
        if self.telegram_bot:
            self.telegram_bot.send_message(message)
        
        return message
    
    def stop(self):
        """Stop grid trading system"""
        if not self.is_running:
            return "System not running"
        
        self._cancel_all_open_orders()
        self.grid = []
        self.pending_orders = {}
        self.is_running = False
        
        message = "Grid trading system stopped"
        self.logger.info(message)
        if self.telegram_bot:
            self.telegram_bot.send_message(message)
        
        return message
    
    def _get_price_precision(self):
        """Get price precision"""
        if not self.symbol_info or 'filters' not in self.symbol_info:
            return 2  # Default price precision
        return get_precision_from_filters(self.symbol_info['filters'], 'PRICE_FILTER', 'tickSize')
    
    def _get_quantity_precision(self):
        """Get quantity precision"""
        if not self.symbol_info or 'filters' not in self.symbol_info:
            return 5  # Default quantity precision
        
        # First try to get from LOT_SIZE filter
        precision = get_precision_from_filters(self.symbol_info['filters'], 'LOT_SIZE', 'stepSize')
        
        # Fallback for BTC and other high-value assets: ensure minimum precision of 5
        if precision < 5 and 'BTC' in self.symbol:
            self.logger.warning(f"Quantity precision for {self.symbol} detected as {precision}, forcing to minimum of 5")
            return 5
            
        return precision
    
    def _adjust_price_precision(self, price):
        """正确格式化价格，确保精度正确"""
        if price <= 0:
            self.logger.warning(f"Attempted to format invalid price: {price}, using minimum price")
            price = 0.00000001  # 安全默认值
        
        # 使用正确的格式化方法，确保不会返回"0"
        formatted_price = "{:.8f}".format(price)  # 使用足够的精度
        
        # 移除尾随零，但确保有效价格
        result = formatted_price.rstrip('0').rstrip('.')
        if not result or result == "0":
            return "0.00000001"  # 保证最小有效价格
        return result
    
    def _adjust_quantity_precision(self, quantity):
        """Adjust quantity precision"""
        return format_quantity(quantity, self.quantity_precision)
    
    def _setup_grid(self):
        """Set up grid"""
        # Calculate grid levels
        self.grid = self._calculate_grid_levels()
        
        # New: Store current ATR value for future volatility comparison
        self.last_atr_value = self._get_current_atr()
        
        # Check if WebSocket API is available for potential batch operations
        client_status = self.binance_client.get_client_status()
        self.using_websocket = client_status["websocket_available"]
        
        if self.using_websocket and not self.simulation_mode:
            # WebSocket is available - could potentially use batch operations
            # But currently the WebSocket API client doesn't support batch order placement
            # So we place orders individually but optimize error handling for WebSocket
            self.logger.info("Using WebSocket API for grid setup")
            self._place_grid_orders_with_websocket()
        else:
            # Use individual order placement
            self._place_grid_orders_individually()
    
    def _place_grid_orders_with_websocket(self):
        """Place grid orders optimized for WebSocket API"""
        for level in self.grid:
            price = level['price']
            side = level['side']
            
            # Validate price before proceeding
            if price <= 0:
                self.logger.error(f"Invalid price value: {price} for {side} order, skipping")
                continue
            
            # Calculate order quantity
            quantity = self.capital_per_level / price
            
            # Adjust quantity and price precision
            formatted_quantity = self._adjust_quantity_precision(quantity)
            formatted_price = self._adjust_price_precision(price)
            
            try:
                if self.simulation_mode:
                    # In simulation mode, just log the order without placing it
                    level['order_id'] = f"sim_{int(time.time())}_{side}_{formatted_price}"
                    self.logger.info(f"Simulation - Would place order: {side} {formatted_quantity} @ {formatted_price}")
                    continue
                
                # Check if we have enough balance for this order
                if side == 'BUY':
                    asset = 'USDT'
                    required = float(formatted_quantity) * float(formatted_price)
                else:  # SELL
                    asset = self.symbol.replace('USDT', '')
                    required = float(formatted_quantity)
                
                balance = self.binance_client.check_balance(asset)
                if balance < required:
                    self.logger.warning(f"Insufficient {asset} balance for {side} order. Required: {required}, Available: {balance}")
                    continue
                
                # Generate a client order ID for tracking
                client_order_id = f"grid_{int(time.time())}_{side}_{formatted_price}"
                
                # Place order with client order ID for tracking WebSocket updates
                order = self.binance_client.place_limit_order(
                    self.symbol, 
                    side, 
                    formatted_quantity, 
                    formatted_price
                )
                
                level['order_id'] = order['orderId']
                
                # Add to pending orders for WebSocket response tracking
                self.pending_orders[str(order['orderId'])] = {
                    'grid_index': self.grid.index(level),
                    'side': side,
                    'price': float(formatted_price),
                    'quantity': float(formatted_quantity),
                    'timestamp': int(time.time())
                }
                
                self.logger.info(f"Order placed via {'WebSocket' if self.using_websocket else 'REST'}: {side} {formatted_quantity} @ {formatted_price}, ID: {order['orderId']}")
            except Exception as e:
                self.logger.error(f"Failed to place order: {side} {formatted_quantity} @ {formatted_price}, Error: {e}")
                
                # Check if connection was lost
                if "connection" in str(e).lower() and self.using_websocket:
                    self.logger.warning("WebSocket connection issue detected, retrying with fallback...")
                    # Update status and retry placement
                    client_status = self.binance_client.get_client_status()
                    self.using_websocket = client_status["websocket_available"]
                    
                    # Try again - will use REST if WebSocket is now unavailable
                    try:
                        order = self.binance_client.place_limit_order(
                            self.symbol, 
                            side, 
                            formatted_quantity, 
                            formatted_price
                        )
                        level['order_id'] = order['orderId']
                        self.logger.info(f"Order placed via fallback: {side} {formatted_quantity} @ {formatted_price}, ID: {order['orderId']}")
                    except Exception as retry_error:
                        self.logger.error(f"Fallback order placement also failed: {retry_error}")
    
    def _place_grid_orders_individually(self):
        """Place grid orders individually (traditional method)"""
        for level in self.grid:
            price = level['price']
            side = level['side']
            
            # Calculate order quantity
            quantity = self.capital_per_level / price
            
            # Adjust quantity and price precision
            formatted_quantity = self._adjust_quantity_precision(quantity)
            formatted_price = self._adjust_price_precision(price)
            
            try:
                if self.simulation_mode:
                    # In simulation mode, just log the order without placing it
                    level['order_id'] = f"sim_{int(time.time())}_{side}_{formatted_price}"
                    self.logger.info(f"Simulation - Would place order: {side} {formatted_quantity} @ {formatted_price}")
                    continue
                
                # Check if we have enough balance for this order
                if side == 'BUY':
                    asset = 'USDT'
                    required = float(formatted_quantity) * float(formatted_price)
                else:  # SELL
                    asset = self.symbol.replace('USDT', '')
                    required = float(formatted_quantity)
                
                balance = self.binance_client.check_balance(asset)
                if balance < required:
                    self.logger.warning(f"Insufficient {asset} balance for {side} order. Required: {required}, Available: {balance}")
                    continue
                
                # Place order
                order = self.binance_client.place_limit_order(
                    self.symbol, 
                    side, 
                    formatted_quantity, 
                    formatted_price
                )
                
                level['order_id'] = order['orderId']
                self.logger.info(f"Order placed successfully: {side} {formatted_quantity} @ {formatted_price}, ID: {order['orderId']}")
            except Exception as e:
                self.logger.error(f"Failed to place order: {side} {formatted_quantity} @ {formatted_price}, Error: {e}")
    
    def _calculate_grid_levels(self):
        """Calculate grid levels"""
        grid_levels = []
        
        # Adjust grid range based on ATR
        atr = self._get_current_atr()
        if atr:
            # Adjust grid range based on ATR
            volatility_factor = atr / self.current_market_price
            adjusted_range = max(self.grid_range_percent, volatility_factor * 2)
        else:
            adjusted_range = self.grid_range_percent
        
        # Calculate upper and lower bounds
        lower_bound = self.current_market_price * (1 - adjusted_range)
        upper_bound = self.current_market_price * (1 + adjusted_range)
        
        # Calculate spacing
        price_range = upper_bound - lower_bound
        level_spacing = price_range / (self.grid_levels - 1) if self.grid_levels > 1 else price_range
        
        # Create grid
        for i in range(self.grid_levels):
            price = lower_bound + i * level_spacing
            # Buy orders below current price, sell orders above current price
            side = "BUY" if price < self.current_market_price else "SELL"
            grid_levels.append({
                'price': price,
                'side': side,
                'order_id': None
            })
        
        return grid_levels
    
    def _get_current_atr(self):
        """Get current ATR value"""
        try:
            # Update connection status before making API calls
            client_status = self.binance_client.get_client_status()
            self.using_websocket = client_status["websocket_available"]
            
            # Get historical klines data
            klines = self.binance_client.get_historical_klines(
                self.symbol, 
                '1d', 
                limit=self.atr_period + 10  # Get extra data to ensure enough samples
            )
            
            # Calculate ATR
            atr = calculate_atr(klines, self.atr_period)
            return atr
        except Exception as e:
            self.logger.error(f"Failed to get ATR: {e}")
            
            # If this was a WebSocket error, force client to update connection status
            if "connection" in str(e).lower() and self.using_websocket:
                self.logger.warning("Connection issue detected while getting ATR, checking client status...")
                client_status = self.binance_client.get_client_status()
                self.using_websocket = client_status["websocket_available"]
            
            return None
    
    def _cancel_all_open_orders(self):
        """Cancel all open orders"""
        try:
            if self.simulation_mode:
                self.logger.info("Simulation mode - Would cancel all open orders")
                return
                
            # Update connection status before making API calls
            client_status = self.binance_client.get_client_status()
            self.using_websocket = client_status["websocket_available"]
            api_type = "WebSocket API" if self.using_websocket else "REST API"
            
            self.logger.info(f"Cancelling all open orders via {api_type}")
            
            open_orders = self.binance_client.get_open_orders(self.symbol)
            for order in open_orders:
                self.binance_client.cancel_order(self.symbol, order['orderId'])
                self.logger.info(f"Order cancelled: {order['orderId']}")
            
            # Clear pending orders tracking
            self.pending_orders = {}
            
        except Exception as e:
            self.logger.error(f"Failed to cancel orders: {e}")
            
            # If this was a WebSocket error, try once more with REST
            if "connection" in str(e).lower() and self.using_websocket:
                self.logger.warning("Connection issue detected while cancelling orders, trying REST API...")
                
                try:
                    # Force client to update connection status
                    client_status = self.binance_client.get_client_status()
                    self.using_websocket = client_status["websocket_available"]
                    
                    # Try again - should use REST if WebSocket is now unavailable
                    open_orders = self.binance_client.get_open_orders(self.symbol)
                    for order in open_orders:
                        self.binance_client.cancel_order(self.symbol, order['orderId'])
                        self.logger.info(f"Order cancelled via fallback: {order['orderId']}")
                except Exception as retry_error:
                    self.logger.error(f"Fallback order cancellation also failed: {retry_error}")
    
    def handle_order_update(self, order_data):
        """Handle order update"""
        if not self.is_running:
            return
        
        # Get essential order information, handling both structured and dict formats
        if isinstance(order_data, dict):
            order_status = order_data.get('X')
            order_id = order_data.get('i')
            symbol = order_data.get('s')
            side = order_data.get('S')
            price = float(order_data.get('p', 0))
            quantity = float(order_data.get('q', 0))
        else:
            # Assume it's a structured object
            try:
                order_status = order_data.X
                order_id = order_data.i
                symbol = order_data.s
                side = order_data.S
                price = float(order_data.p)
                quantity = float(order_data.q)
            except AttributeError as e:
                self.logger.error(f"Failed to parse order update: {e}, data: {order_data}")
                return
        
        # Skip if not our symbol
        if symbol != self.symbol:
            return
            
        # Handle order filled event
        if order_status == 'FILLED':
            self.logger.info(f"Order filled: {side} {quantity} @ {price} (ID: {order_id})")
            
            # Check if this is a pending order we're tracking
            str_order_id = str(order_id)
            if str_order_id in self.pending_orders:
                # Remove from pending orders
                pending_info = self.pending_orders.pop(str_order_id)
                self.logger.debug(f"Pending order {str_order_id} fulfilled from tracking")
            
            # Find matching grid level
            matching_level = None
            for i, level in enumerate(self.grid):
                if level['order_id'] == order_id or str(level['order_id']) == str_order_id:
                    matching_level = level
                    level_index = i
                    break
            
            if not matching_level:
                self.logger.warning(f"Received fill for order {order_id} but couldn't find in grid")
                # Try to reconcile grid with open orders - this is a recovery mechanism
                self._reconcile_grid_with_open_orders()
                return
            
            # Create opposite order
            new_side = "SELL" if side == "BUY" else "BUY"
            
            # Adjust quantity and price precision
            formatted_quantity = self._adjust_quantity_precision(quantity)
            formatted_price = self._adjust_price_precision(price)
            
            try:
                if self.simulation_mode:
                    self.logger.info(f"Simulation - Would place opposite order: {new_side} {formatted_quantity} @ {formatted_price}")
                    matching_level['side'] = new_side
                    matching_level['order_id'] = f"sim_{int(time.time())}_{new_side}_{formatted_price}"
                    message = f"Grid trade executed (simulation): {side} {formatted_quantity} @ {formatted_price}\nOpposite order would be: {new_side} {formatted_quantity} @ {formatted_price}"
                    if self.telegram_bot:
                        self.telegram_bot.send_message(message)
                    return
                
                # Check if we have enough balance for this order
                if new_side == 'BUY':
                    asset = 'USDT'
                    required = float(formatted_quantity) * float(formatted_price)
                else:  # SELL
                    asset = self.symbol.replace('USDT', '')
                    required = float(formatted_quantity)
                
                balance = self.binance_client.check_balance(asset)
                if balance < required:
                    message = f"⚠️ Cannot place opposite order: Insufficient {asset} balance. Required: {required}, Available: {balance}"
                    self.logger.warning(message)
                    if self.telegram_bot:
                        self.telegram_bot.send_message(message)
                    return
                
                # Update connection status before placing order
                client_status = self.binance_client.get_client_status()
                self.using_websocket = client_status["websocket_available"]
                api_type = "WebSocket API" if self.using_websocket else "REST API"
                
                # Place opposite order
                new_order = self.binance_client.place_limit_order(
                    self.symbol, 
                    new_side, 
                    formatted_quantity, 
                    formatted_price
                )
                
                # Update grid
                matching_level['side'] = new_side
                matching_level['order_id'] = new_order['orderId']
                
                # Add to pending orders tracking if using WebSocket
                if self.using_websocket:
                    self.pending_orders[str(new_order['orderId'])] = {
                        'grid_index': level_index,
                        'side': new_side,
                        'price': float(formatted_price),
                        'quantity': float(formatted_quantity),
                        'timestamp': int(time.time())
                    }
                
                message = f"Grid trade executed: {side} {formatted_quantity} @ {formatted_price}\nOpposite order placed via {api_type}: {new_side} {formatted_quantity} @ {formatted_price}"
                self.logger.info(message)
                if self.telegram_bot:
                    self.telegram_bot.send_message(message)
            except Exception as e:
                self.logger.error(f"Failed to place opposite order: {e}")
                
                # If WebSocket connection error, try with REST API
                if "connection" in str(e).lower() and self.using_websocket:
                    try:
                        self.logger.warning("Connection error - falling back to REST API for opposite order")
                        
                        # Force update of client status
                        client_status = self.binance_client.get_client_status()
                        self.using_websocket = client_status["websocket_available"]
                        
                        # Try again - will use REST if WebSocket is now unavailable
                        new_order = self.binance_client.place_limit_order(
                            self.symbol, 
                            new_side, 
                            formatted_quantity, 
                            formatted_price
                        )
                        
                        # Update grid
                        matching_level['side'] = new_side
                        matching_level['order_id'] = new_order['orderId']
                        
                        message = f"Grid trade executed: {side} {formatted_quantity} @ {formatted_price}\nOpposite order placed via fallback: {new_side} {formatted_quantity} @ {formatted_price}"
                        self.logger.info(message)
                        if self.telegram_bot:
                            self.telegram_bot.send_message(message)
                    except Exception as retry_error:
                        self.logger.error(f"Fallback order placement also failed: {retry_error}")
    
    def _reconcile_grid_with_open_orders(self):
        """Reconcile grid with actual open orders (recovery mechanism)"""
        if self.simulation_mode:
            return
            
        try:
            self.logger.info("Reconciling grid with actual open orders...")
            
            # Get current open orders
            open_orders = self.binance_client.get_open_orders(self.symbol)
            
            # Create a map of order IDs to easily check
            open_order_ids = {str(order['orderId']): order for order in open_orders}
            
            # Check each grid level
            for i, level in enumerate(self.grid):
                if level['order_id']:
                    str_order_id = str(level['order_id'])
                    
                    # Check if this order is still open
                    if str_order_id not in open_order_ids:
                        self.logger.warning(f"Grid level {i} order {str_order_id} is not in open orders, may need to be replaced")
                        
                        # Mark as needing replacement (could implement auto-replacement here)
                        level['needs_replacement'] = True
            
            self.logger.info("Grid reconciliation complete")
        except Exception as e:
            self.logger.error(f"Failed to reconcile grid: {e}")
    
    def check_grid_recalculation(self):
        """Check if grid recalculation is needed"""
        if not self.is_running or not self.last_recalculation:
            return
        
        recalculate = False
        recalculation_reason = ""
        
        # New: Check for significant volatility changes
        current_atr = self._get_current_atr()
        
        # Check if we have valid ATR values and enough time has passed for cooldown
        if self.last_atr_value and current_atr:
            # Ensure at least 24 hour cooldown period to avoid frequent recalculations
            hours_since_last = (datetime.now() - self.last_recalculation).total_seconds() / 3600
            if hours_since_last >= 24:
                # Calculate ATR change ratio
                atr_change_ratio = abs(current_atr - self.last_atr_value) / self.last_atr_value
                
                # If volatility changed by more than 30%, trigger recalculation
                if atr_change_ratio > 0.3:  # 30% threshold
                    recalculate = True
                    recalculation_reason = f"Significant volatility change detected: {atr_change_ratio:.2%}"
                    self.logger.info(f"Significant volatility change detected: {atr_change_ratio:.2%}, triggering grid recalculation")
        
        # Original logic: Check time interval
        days_since_last = (datetime.now() - self.last_recalculation).days
        if days_since_last >= self.recalculation_period:
            recalculate = True
            recalculation_reason = f"Regular recalculation period reached ({self.recalculation_period} days)"
            
        # If either condition is met, recalculate grid
        if recalculate:
            self.logger.info(f"Starting grid recalculation. Reason: {recalculation_reason}")
            
            # Update current market price
            self.current_market_price = self.binance_client.get_symbol_price(self.symbol)
            
            # Cancel all open orders
            self._cancel_all_open_orders()
            
            # Recalculate and set grid
            self._setup_grid()
            
            # Reset recalculation timestamp
            self.last_recalculation = datetime.now()
            
            message = f"Grid recalculated! Reason: {recalculation_reason}\nCurrent price: {self.current_market_price}\nGrid range: {len(self.grid)} levels"
            self.logger.info(message)
            if self.telegram_bot:
                self.telegram_bot.send_message(message)
    
    def get_status(self):
        """Get current status"""
        if not self.is_running:
            return "System not running"
        
        # Get current price and update connection status
        try:
            # Check which API we're currently using
            client_status = self.binance_client.get_client_status()
            self.using_websocket = client_status["websocket_available"]
            api_type = "WebSocket API" if self.using_websocket else "REST API"
            
            current_price = self.binance_client.get_symbol_price(self.symbol)
            
            # Build status message
            grid_info = []
            for i, level in enumerate(self.grid):
                grid_info.append(f"Grid {i+1}: {level['side']} @ {level['price']:.2f}")
            
            status = f"Grid Trading Status:\n" \
                    f"Trading pair: {self.symbol}\n" \
                    f"Current price: {current_price}\n" \
                    f"Grid levels: {len(self.grid)}\n" \
                    f"Last adjusted: {self.last_recalculation}\n" \
                    f"API in use: {api_type}\n" \
                    f"Grid details:\n" + "\n".join(grid_info)
            
            return status
        except Exception as e:
            self.logger.error(f"Failed to get status: {e}")
            return f"Failed to get status: {e}"