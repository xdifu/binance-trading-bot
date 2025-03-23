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
        """
        Stop grid trading system
        
        Returns:
            str: Status message
        """
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
        """
        Get price precision from symbol info
        
        Returns:
            int: Price precision value (default 8 if unavailable)
        """
        if not self.symbol_info or 'filters' not in self.symbol_info:
            self.logger.warning(f"Symbol info missing or incomplete for {self.symbol}, using default precision 8")
            return 8  # Increased default from 2 to 8 for better small value handling
        
        precision = get_precision_from_filters(self.symbol_info['filters'], 'PRICE_FILTER', 'tickSize')
        
        # Ensure minimum precision for small-valued assets
        if precision < 4 and self.symbol.startswith(('1000SATS', 'SHIB', 'DOGE')):
            self.logger.warning(f"Symbol {self.symbol} detected as small-value asset but precision is only {precision}, forcing to minimum of 8")
            return 8
            
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
        
        # Fallback for BTC and other high-value assets: ensure minimum precision of 5
        if precision < 5 and 'BTC' in self.symbol:
            self.logger.warning(f"Quantity precision for {self.symbol} detected as {precision}, forcing to minimum of 5")
            return 5
            
        return precision
    
    def _adjust_price_precision(self, price):
        """
        Format price with appropriate precision ensuring non-zero values
        
        Args:
            price (float): Original price value
            
        Returns:
            str: Formatted price string that is never "0"
        """
        if price <= 0:
            self.logger.warning(f"Attempted to format invalid price: {price}, using minimum price")
            return "0.00000001"  # Minimum valid price for Binance
        
        # For small prices or small-value assets (like 1000SATS), ensure enough decimal places
        if price < 0.001 or self.symbol.startswith(('1000SATS', 'SHIB', 'DOGE')):
            # Use utils.format_price with high precision 
            return format_price(price, 8)  # Force 8 decimal places for very small values
        
        # For normal assets, use standard precision based on symbol info
        # But ensure precision is at least 4 to avoid "0" for small values
        actual_precision = max(self.price_precision, 4)
        return format_price(price, actual_precision)
    
    def _adjust_quantity_precision(self, quantity):
        """
        Format quantity with appropriate precision
        
        Args:
            quantity (float): Original quantity value
            
        Returns:
            str: Formatted quantity string
        """
        return format_quantity(quantity, self.quantity_precision)
    
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
        """
        Calculate grid levels based on current market price and config
        
        Returns:
            list: Grid levels with price and side information
        """
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
        
        # Ensure lower bound is never zero or negative
        lower_bound = max(lower_bound, 0.00000001)
        
        # Calculate spacing
        price_range = upper_bound - lower_bound
        level_spacing = price_range / (self.grid_levels - 1) if self.grid_levels > 1 else price_range
        
        # Create grid
        for i in range(self.grid_levels):
            price = lower_bound + i * level_spacing
            
            # Ensure price is valid
            if price <= 0:
                self.logger.error(f"Invalid price calculated for grid level {i}: {price}")
                price = 0.00000001 * (i + 1)  # Use a minimal valid price
                
            # Buy orders below current price, sell orders above current price
            side = "BUY" if price < self.current_market_price else "SELL"
            grid_levels.append({
                'price': price,
                'side': side,
                'order_id': None
            })
            
            # Log the level details including the formatted price
            formatted_price = self._adjust_price_precision(price)
            self.logger.debug(f"Grid level {i}: {side} @ {price} (formatted: {formatted_price})")
        
        return grid_levels
    
    def _get_current_atr(self):
        """
        Get current ATR value for volatility measurement
        
        Returns:
            float: ATR value or None if error
        """
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
        """Cancel all open orders for the trading symbol"""
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
            formatted_price = self._adjust_price_precision(price)
            
            # Double-check price formatting
            if formatted_price == "0" or float(formatted_price) <= 0:
                self.logger.error(f"Invalid formatted price: {formatted_price} for {price}, using minimum valid price")
                formatted_price = "0.00000001"  # Use minimum valid price
            
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
        """Check if grid recalculation is needed based on time or volatility changes"""
        if not self.is_running or not self.last_recalculation:
            return
        
        recalculate = False
        recalculation_reason = ""
        
        # Check for significant volatility changes
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
        """
        Get current grid trading status
        
        Returns:
            str: Status message with details
        """
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
                # Format price for display using our safe formatter
                formatted_price = self._adjust_price_precision(level['price'])
                grid_info.append(f"Grid {i+1}: {level['side']} @ {formatted_price}")
            
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