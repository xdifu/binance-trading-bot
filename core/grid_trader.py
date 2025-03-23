import logging
import math
import time
import threading
from datetime import datetime, timedelta
from binance_api.client import BinanceClient
from utils.indicators import calculate_atr
from utils.format_utils import format_price, format_quantity, get_precision_from_filters
import config

class GridTrader:
    def __init__(self, binance_client, telegram_bot=None):
        """
        Initialize grid trading strategy
        
        Args:
            binance_client: BinanceClient instance for API operations
            telegram_bot: Optional TelegramBot instance for notifications
        """
        self.binance_client = binance_client
        self.telegram_bot = telegram_bot
        self.symbol = config.SYMBOL
        self.grid_levels = config.GRID_LEVELS
        self.grid_spacing = config.GRID_SPACING / 100  # Convert to decimal
        self.capital_per_level = config.CAPITAL_PER_LEVEL
        self.grid_range_percent = config.GRID_RANGE_PERCENT / 100  # Convert to decimal
        self.recalculation_period = config.RECALCULATION_PERIOD
        self.atr_period = config.ATR_PERIOD
        
        # Store previous ATR value for volatility change detection
        self.last_atr_value = None
        
        # Initialize logger
        self.logger = logging.getLogger(__name__)
        
        # Get symbol information and set precision
        self.symbol_info = self._get_symbol_info()
        
        # Store tickSize and stepSize directly from filters
        self.tick_size = self._get_tick_size()
        self.step_size = self._get_step_size()
        self.price_precision = self._get_price_precision()
        self.quantity_precision = self._get_quantity_precision()
        
        self.logger.info(f"Trading pair {self.symbol} price precision: {self.price_precision}, quantity precision: {self.quantity_precision}")
        self.logger.info(f"Trading pair {self.symbol} tick size: {self.tick_size}, step size: {self.step_size}")
        
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
        
        # Add resource locking mechanism to prevent race conditions
        self.locked_balances = {}  # Tracks locked balances by asset
        self.balance_lock = threading.RLock()  # Thread-safe lock for balance operations
    
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
    
    def _get_tick_size(self):
        """
        Get tick size directly from symbol filters
        
        Returns:
            float: Tick size value for price (default 0.00000001 if not found)
        """
        if not self.symbol_info or 'filters' not in self.symbol_info:
            self.logger.warning(f"Symbol info missing for {self.symbol}, using minimum tick size 0.00000001")
            return 0.00000001  # Minimum tick size as fallback
        
        for f in self.symbol_info['filters']:
            if f['filterType'] == 'PRICE_FILTER':
                tick_size = float(f['tickSize'])
                self.logger.debug(f"Found tick size for {self.symbol}: {tick_size}")
                return tick_size
                
        self.logger.warning(f"No PRICE_FILTER found for {self.symbol}, using minimum tick size 0.00000001")
        return 0.00000001  # Default if not found
    
    def _get_step_size(self):
        """
        Get step size directly from symbol filters
        
        Returns:
            float: Step size value for quantity (default 1.0 if not found)
        """
        if not self.symbol_info or 'filters' not in self.symbol_info:
            self.logger.warning(f"Symbol info missing for {self.symbol}, using minimum step size 1.0")
            return 1.0  # Default step size
        
        for f in self.symbol_info['filters']:
            if f['filterType'] == 'LOT_SIZE':
                step_size = float(f['stepSize'])
                self.logger.debug(f"Found step size for {self.symbol}: {step_size}")
                return step_size
                
        self.logger.warning(f"No LOT_SIZE filter found for {self.symbol}, using minimum step size 1.0")
        return 1.0  # Default if not found
    
    def get_status(self):
        """
        Get current status of the grid trading system
        
        Returns:
            str: Status message
        """
        if not self.is_running:
            return "Grid trading system is not running"
        
        # Build status message with current market price
        current_price = self.binance_client.get_symbol_price(self.symbol)
        
        # Count active buy and sell orders
        buy_orders = 0
        sell_orders = 0
        for level in self.grid:
            if level.get('order_id'):
                if level['side'] == 'BUY':
                    buy_orders += 1
                elif level['side'] == 'SELL':
                    sell_orders += 1
        
        # Calculate total orders
        total_orders = buy_orders + sell_orders
        
        # Format grid range information
        grid_info = ""
        if self.grid and len(self.grid) > 0:
            prices = [level['price'] for level in self.grid]
            if prices:
                lowest_price = min(prices)
                highest_price = max(prices)
                price_range_pct = ((highest_price - lowest_price) / lowest_price) * 100
                grid_info = f"\nGrid price range: {lowest_price:.8f} - {highest_price:.8f} ({price_range_pct:.2f}%)"
        
        # Format the status message
        status = (
            f"Grid Trading Status: Active\n"
            f"Symbol: {self.symbol}\n"
            f"Current price: {current_price}\n"
            f"Active orders: {total_orders} (Buy: {buy_orders}, Sell: {sell_orders})"
            f"{grid_info}\n"
            f"Using {'WebSocket' if self.using_websocket else 'REST'} API"
        )
        
        return status
    
    def start(self, simulation=False):
        """
        Start grid trading system
        
        Args:
            simulation: Whether to run in simulation mode (no real orders)
            
        Returns:
            str: Status message
        """
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
        
        # Calculate temporary grid to estimate resource requirements
        temp_grid = self._calculate_grid_levels()
        
        # Calculate actual needed resources
        usdt_needed = 0
        base_needed = 0
        
        for level in temp_grid:
            price = level['price']
            quantity = self.capital_per_level / price
            
            if level['side'] == 'BUY':
                # Buy orders need USDT
                usdt_needed += self.capital_per_level
            else:
                # Sell orders need base asset
                base_needed += quantity
        
        # Check balances
        base_balance = self.binance_client.check_balance(base_asset)
        quote_balance = self.binance_client.check_balance(quote_asset)
        
        insufficient_funds = False
        warnings = []
        
        if quote_balance < usdt_needed and not simulation:
            warnings.append(f"Insufficient {quote_asset} balance. Required: {usdt_needed:.2f}, Available: {quote_balance:.2f}")
            insufficient_funds = True
            
        if base_balance < base_needed and not simulation:
            warnings.append(f"Insufficient {base_asset} balance. Required: {base_needed:.2f}, Available: {base_balance:.2f}")
            insufficient_funds = True
        
        if insufficient_funds and not simulation:
            warning_message = "⚠️ Warning: " + " ".join(warnings) + "\nStarting in limited mode."
            self.logger.warning(warning_message)
            if self.telegram_bot:
                self.telegram_bot.send_message(warning_message)
        
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
            return "System already stopped"
        
        try:
            # Cancel all open orders
            if not self.simulation_mode:
                self._cancel_all_open_orders()
            
            # Reset all fund locks
            self._reset_locks()
            
            # Reset internal state
            self.is_running = False
            self.grid = []  # Clear grid
            self.pending_orders = {}  # Clear pending_orders tracking
            self.last_recalculation = None
            
            message = "Grid trading system stopped"
            self.logger.info(message)
            
            return message
        except Exception as e:
            error_message = f"Error stopping grid trading: {e}"
            self.logger.error(error_message)
            return error_message
    
    def _get_price_precision(self):
        """
        Get price precision from symbol info
        
        Returns:
            int: Price precision value (default 8 if unavailable)
        """
        if not self.symbol_info or 'filters' not in self.symbol_info:
            self.logger.warning(f"Symbol info missing or incomplete for {self.symbol}, using default precision 8")
            return 8  # Increased default from 2 to 8 for better small value handling
        
        precision = get_precision_from_filters(self.symbol_info['filters'], 'PRICE_FILTER', 'tickSize')
        
        # Ensure minimum precision for all assets to avoid rounding to zero
        return max(precision, 4)  # Minimum precision of 4 for all assets
    
    def _get_quantity_precision(self):
        """
        Get quantity precision from symbol info
        
        Returns:
            int: Quantity precision value (default 5 if unavailable)
        """
        if not self.symbol_info or 'filters' not in self.symbol_info:
            return 5  # Default quantity precision
        
        # First try to get from LOT_SIZE filter
        precision = get_precision_from_filters(self.symbol_info['filters'], 'LOT_SIZE', 'stepSize')
            
        return precision
    
    def _adjust_price_precision(self, price):
        """
        Format price with appropriate precision
        
        Args:
            price (float): Original price value
            
        Returns:
            str: Formatted price string
        """
        if price <= 0:
            self.logger.warning(f"Attempted to format invalid price: {price}, using minimum price")
            return format_price(self.tick_size, self.price_precision)  # Return minimum tick size
        
        return format_price(price, self.price_precision)
    
    def _adjust_quantity_precision(self, quantity):
        """
        Format quantity with appropriate precision for LOT_SIZE filter compliance
        - Floors to step_size multiples (Binance requirement)
        
        Args:
            quantity (float): Original quantity value
            
        Returns:
            str: Formatted quantity string
        """
        # Ensure quantity is positive
        quantity = abs(float(quantity))
        
        # Floor to nearest valid step size (Binance rule for quantity)
        step_size = self.step_size
        floored_quantity = math.floor(quantity / step_size) * step_size
        
        # Format with correct precision
        return "{:.{}f}".format(floored_quantity, self.quantity_precision)
    
    def _setup_grid(self):
        """Set up grid levels and place initial orders"""
        # Calculate grid levels
        self.grid = self._calculate_grid_levels()
        
        # Store current ATR value for future volatility comparison
        self.last_atr_value = self._get_current_atr()
        
        # Check if WebSocket API is available for potential batch operations
        client_status = self.binance_client.get_client_status()
        self.using_websocket = client_status["websocket_available"]
        
        if self.using_websocket and not self.simulation_mode:
            # WebSocket is available - use optimized order placement
            self.logger.info("Using WebSocket API for grid setup")
            self._place_grid_orders_with_websocket()
        else:
            # Use individual order placement
            self._place_grid_orders_individually()
    
    def _calculate_grid_levels(self):
        """
        Calculate grid price levels based on current market price and configuration
        
        Returns:
            list: List of grid levels with prices and sides
        """
        # Get current market price 
        current_price = self.binance_client.get_symbol_price(self.symbol)
        self.current_market_price = current_price
        
        # Calculate grid range based on current price and grid_range_percent
        grid_range = current_price * self.grid_range_percent
        
        # Calculate upper and lower bounds
        upper_bound = current_price + (grid_range / 2)
        lower_bound = current_price - (grid_range / 2)
        
        # Calculate grid spacing (price difference between adjacent levels)
        grid_step = grid_range / (self.grid_levels - 1) if self.grid_levels > 1 else 0
        
        # 基于ATR调整网格间距
        atr = self._get_current_atr()
        if (atr):
            # 使用ATR值动态调整网格间距
            # ATR值越高，间距越大
            volatility_factor = min(max(atr / current_price * 10, 0.8), 1.5)
            grid_step = grid_step * volatility_factor
            self.logger.info(f"Adjusted grid step based on volatility: {grid_step:.4f}")
        
        # Build the grid levels
        grid = []
        
        for i in range(self.grid_levels):
            # Calculate price for this level
            price = lower_bound + (i * grid_step)
            
            # Determine side (BUY below current price, SELL above)
            side = "BUY" if price < current_price else "SELL"
            
            # Handle exact matches with current price - make it a BUY
            if abs(price - current_price) < 0.0000001:
                side = "BUY"
            
            # Add to grid
            grid.append({
                "price": price,
                "side": side, 
                "order_id": None
            })
        
        return grid
    
    def _cancel_all_open_orders(self):
        """
        Cancel all open orders for the trading pair
        
        Returns:
            bool: True if successful, False otherwise
        """
        try:
            # Use WebSocket if available, otherwise REST
            client_status = self.binance_client.get_client_status()
            self.using_websocket = client_status["websocket_available"]
            
            # Log which API is being used
            self.logger.info(f"Cancelling all open orders via {'WebSocket' if self.using_websocket else 'REST'} API")
            
            # Get all open orders
            open_orders = self.binance_client.get_open_orders(self.symbol)
            
            for order in open_orders:
                if self.simulation_mode:
                    self.logger.info(f"Simulation - Would cancel order {order['orderId']}")
                    continue
                    
                result = self.binance_client.cancel_order(
                    symbol=self.symbol,
                    order_id=order['orderId']
                )
                self.logger.info(f"Order cancelled: {order['orderId']}")
            
            return True
        except Exception as e:
            self.logger.error(f"Error cancelling orders: {e}")
            return False
    
    def _place_grid_orders_with_websocket(self):
        """Place grid orders optimized for WebSocket API with fund locking"""
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
            
            # Additional validation to ensure price isn't zero or invalid
            if formatted_price == "0" or float(formatted_price) <= 0:
                self.logger.error(f"Price formatting returned invalid value: '{formatted_price}' for {price}, skipping order")
                continue
                
            try:
                if self.simulation_mode:
                    # In simulation mode, just log the order without placing it
                    level['order_id'] = f"sim_{int(time.time())}_{side}_{formatted_price}"
                    self.logger.info(f"Simulation - Would place order: {side} {formatted_quantity} @ {formatted_price}")
                    continue
                
                # Check and lock funds - prevents race conditions
                if side == 'BUY':
                    asset = 'USDT'
                    required = float(formatted_quantity) * float(formatted_price)
                else:  # SELL
                    asset = self.symbol.replace('USDT', '')
                    required = float(formatted_quantity)
                
                # Try to lock the funds - this is atomic with the balance check
                if not self._lock_funds(asset, required):
                    self.logger.warning(f"Could not lock funds for {side} order. Required: {required} {asset}")
                    continue
                
                try:
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
                        'timestamp': int(time.time()),
                        'asset': asset,
                        'required': required
                    }
                    
                    self.logger.info(f"Order placed via {'WebSocket' if self.using_websocket else 'REST'}: {side} {formatted_quantity} @ {formatted_price}, ID: {order['orderId']}")
                except Exception as e:
                    # Release the funds if order placement fails
                    self._release_funds(asset, required)
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
                            # Still failed, make sure we release the funds
                            self._release_funds(asset, required)
                            self.logger.error(f"Fallback order placement also failed: {retry_error}")
            except Exception as e:
                self.logger.error(f"Error in order preparation: {e}")

    def _place_grid_orders_individually(self):
        """Place grid orders individually (traditional method) with fund locking"""
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
            
            # Additional validation to ensure price isn't zero or invalid
            if formatted_price == "0" or float(formatted_price) <= 0:
                self.logger.error(f"Price formatting returned invalid value: '{formatted_price}' for {price}, skipping order")
                continue
                
            try:
                if self.simulation_mode:
                    # In simulation mode, just log the order without placing it
                    level['order_id'] = f"sim_{int(time.time())}_{side}_{formatted_price}"
                    self.logger.info(f"Simulation - Would place order: {side} {formatted_quantity} @ {formatted_price}")
                    continue
                
                # Check and lock funds - prevents race conditions
                if side == 'BUY':
                    asset = 'USDT'
                    required = float(formatted_quantity) * float(formatted_price)
                else:  # SELL
                    asset = self.symbol.replace('USDT', '')
                    required = float(formatted_quantity)
                
                # Try to lock the funds - this is atomic with the balance check
                if not self._lock_funds(asset, required):
                    self.logger.warning(f"Could not lock funds for {side} order. Required: {required} {asset}")
                    continue
                
                try:
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
                    # Release the funds if order placement fails
                    self._release_funds(asset, required)
                    self.logger.error(f"Failed to place order: {side} {formatted_quantity} @ {formatted_price}, Error: {e}")
            except Exception as e:
                self.logger.error(f"Error in order preparation: {e}")
    
    def _get_current_atr(self):
        """
        Calculate current ATR for volatility assessment
        
        Returns:
            float: ATR value or None if calculation fails
        """
        try:
            # Get klines for ATR calculation using get_historical_klines instead of get_klines
            klines = self.binance_client.get_historical_klines(
                symbol=self.symbol,
                interval="1h",  # Use 1h for stability
                limit=self.atr_period + 10  # Add buffer
            )
            
            # Calculate ATR
            atr = calculate_atr(klines, period=self.atr_period)
            return atr
        except Exception as e:
            self.logger.error(f"Error calculating ATR: {e}")
            return None
    
    def check_grid_recalculation(self):
        """
        Check if grid needs recalculation based on time or volatility change
        
        Returns:
            bool: True if grid was recalculated, False otherwise
        """
        # Skip if not running
        if not self.is_running:
            return False
            
        now = datetime.now()
        
        # Check if recalculation period has passed
        time_based_recalc = False
        if self.last_recalculation:
            days_passed = (now - self.last_recalculation).days
            if days_passed >= self.recalculation_period:
                self.logger.info(f"Time-based grid recalculation triggered: {days_passed} days since last recalculation")
                time_based_recalc = True
        
        # Check for volatility-based recalculation
        volatility_based_recalc = False
        current_atr = self._get_current_atr()
        
        if current_atr and self.last_atr_value:
            atr_change = abs(current_atr - self.last_atr_value) / self.last_atr_value
            if atr_change > 0.2:  # 20% change in volatility
                self.logger.info(f"Volatility-based grid recalculation triggered: ATR changed by {atr_change*100:.2f}%")
                volatility_based_recalc = True
        
        # If either condition is met, recalculate grid
        if time_based_recalc or volatility_based_recalc:
            try:
                # Cancel all orders
                self._cancel_all_open_orders()
                
                # Recalculate grid
                self._setup_grid()
                
                # Update last recalculation timestamp
                self.last_recalculation = now
                
                message = "Grid trading levels recalculated due to "
                message += "scheduled recalculation" if time_based_recalc else "volatility change"
                
                self.logger.info(message)
                if self.telegram_bot:
                    self.telegram_bot.send_message(message)
                
                return True
            except Exception as e:
                self.logger.error(f"Failed to recalculate grid: {e}")
        
        return False
    
    def _reconcile_grid_with_open_orders(self):
        """
        Reconcile grid with current open orders to handle potential discrepancies
        """
        if self.simulation_mode:
            return
            
        try:
            open_orders = self.binance_client.get_open_orders(self.symbol)
            open_order_ids = set(str(order['orderId']) for order in open_orders)
            
            # Update grid with current open orders
            for level in self.grid:
                if level.get('order_id'):
                    order_id = str(level['order_id'])
                    # If order is in our grid but not actually open, mark it
                    if order_id not in open_order_ids:
                        self.logger.warning(f"Order {order_id} is in grid but not found in open orders")
                        level['order_id'] = None
                        
            self.logger.info("Grid reconciled with open orders")
        except Exception as e:
            self.logger.error(f"Error reconciling grid: {e}")
    
    def handle_order_update(self, order_data):
        """
        Handle order update from WebSocket
        
        Args:
            order_data: Order update data from WebSocket
        """
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
                
                # Release locked funds for the fulfilled order
                self._release_funds(pending_info['asset'], pending_info['required'])
            
            # Find matching grid level
            matching_level = None
            level_index = -1
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
            
            # 修改建议 - 为反向订单添加价差
            if new_side == "SELL":
                # 买入成交后，卖单提价
                new_price = price * 1.01  # 提高1%
                formatted_price = self._adjust_price_precision(new_price)
            else:
                # 卖出成交后，买单降价
                new_price = price * 0.99  # 降低1%
                formatted_price = self._adjust_price_precision(new_price)
            
            # Double-check price formatting
            if formatted_price == "0" or float(formatted_price) <= 0:
                self.logger.error(f"Invalid formatted price: {formatted_price} for {price}, using minimum valid price")
                formatted_price = self._adjust_price_precision(self.tick_size)  # Use minimum valid price
            
            try:
                if self.simulation_mode:
                    self.logger.info(f"Simulation - Would place opposite order: {new_side} {formatted_quantity} @ {formatted_price}")
                    matching_level['side'] = new_side
                    matching_level['order_id'] = f"sim_{int(time.time())}_{new_side}_{formatted_price}"
                    message = f"Grid trade executed (simulation): {side} {formatted_quantity} @ {formatted_price}\nOpposite order would be: {new_side} {formatted_quantity} @ {formatted_price}"
                    if self.telegram_bot:
                        self.telegram_bot.send_message(message)
                    return
                
                # Check and lock funds - prevents race conditions
                if new_side == 'BUY':
                    asset = 'USDT'
                    required = float(formatted_quantity) * float(formatted_price)
                else:  # SELL
                    asset = self.symbol.replace('USDT', '')
                    required = float(formatted_quantity)
                
                # Try to lock the funds - this is atomic with the balance check
                if not self._lock_funds(asset, required):
                    message = f"⚠️ Cannot place opposite order: Insufficient {asset} balance. Required: {required}, Available: {self.binance_client.check_balance(asset) - self.locked_balances.get(asset, 0)}"
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
                        'timestamp': int(time.time()),
                        'asset': asset,
                        'required': required
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

    def _lock_funds(self, asset, amount):
        """
        Lock funds for a particular asset to prevent race conditions
        
        Args:
            asset: Asset symbol (e.g., 'BTC', 'USDT')
            amount: Amount to lock
            
        Returns:
            bool: True if funds were successfully locked, False otherwise
        """
        with self.balance_lock:
            # Get current balance
            current_balance = self.binance_client.check_balance(asset)
            
            # Get current locked amount (default 0)
            current_locked = self.locked_balances.get(asset, 0)
            
            # Calculate available balance
            available = current_balance - current_locked
            
            # Check if we have enough available balance
            if available < amount:
                self.logger.warning(
                    f"Insufficient {asset} balance for locking: "
                    f"Required: {amount}, Available: {available} "
                    f"(Total: {current_balance}, Already locked: {current_locked})"
                )
                return False
            
            # Lock the funds
            self.locked_balances[asset] = current_locked + amount
            self.logger.debug(
                f"Locked {amount} {asset}, total locked now: {self.locked_balances[asset]}, "
                f"remaining available: {current_balance - self.locked_balances[asset]}"
            )
            return True
    
    def _release_funds(self, asset, amount):
        """
        Release previously locked funds
        
        Args:
            asset: Asset symbol (e.g., 'BTC', 'USDT')
            amount: Amount to release
            
        Returns:
            None
        """
        with self.balance_lock:
            current_locked = self.locked_balances.get(asset, 0)
            
            # Ensure we don't release more than locked
            release_amount = min(current_locked, amount)
            
            if release_amount > 0:
                self.locked_balances[asset] = current_locked - release_amount
                self.logger.debug(f"Released {release_amount} {asset}, remaining locked: {self.locked_balances[asset]}")
    
    def _reset_locks(self):
        """Reset all fund locks when stopping grid or recalculating"""
        with self.balance_lock:
            previous_locks = self.locked_balances.copy()
            self.locked_balances = {}
            self.logger.info(f"Reset all fund locks. Previous locks: {previous_locks}")