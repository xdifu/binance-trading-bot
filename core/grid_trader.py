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
        Format price to comply with exchange's PRICE_FILTER
        - Must be tick_size multiples
        - Rounds to nearest valid price point
        
        Args:
            price (float): Original price value
            
        Returns:
            str: Formatted price string compliant with exchange rules
        """
        if price <= 0:
            self.logger.warning(f"Attempted to format invalid price: {price}, using minimum price")
            return format_price(self.tick_size, self.price_precision)  # Return minimum tick size
        
        # Round to the nearest tick size - crucial fix for PRICE_FILTER
        tick_size = self.tick_size
        rounded_price = round(price / tick_size) * tick_size
        
        # Ensure the price is exactly divisible by tickSize (important for Binance)
        # Use modulo operation to check and adjust if needed
        mod_remainder = rounded_price % tick_size
        if mod_remainder != 0:
            # Adjust to nearest valid tick
            rounded_price = rounded_price - mod_remainder
            
        # Format with proper precision for string representation
        formatted_price = "{:.{}f}".format(rounded_price, self.price_precision)
        
        # Remove trailing zeros but never return "0"
        result = formatted_price.rstrip('0').rstrip('.') if '.' in formatted_price else formatted_price
        if result == "0":
            return format_price(tick_size, self.price_precision)
            
        return result
    
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
    
    # ... 其他方法保持不变 ...

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