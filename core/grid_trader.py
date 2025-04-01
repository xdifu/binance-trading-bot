import logging
import math
import time
import threading
from datetime import datetime, timedelta
from binance_api.client import BinanceClient
from utils.indicators import calculate_atr
from utils.format_utils import format_price, format_quantity, get_precision_from_filters
import config  # Ensure this line is present to import the config module

# Ensure MIN_NOTIONAL_VALUE is defined in the config module
if not hasattr(config, 'MIN_NOTIONAL_VALUE'):
    raise AttributeError("config.MIN_NOTIONAL_VALUE is not defined. Please define it in the config module.")

class GridTrader:
    # Define constants for configuration
    CAPITAL_INCREASE_FACTOR = 1.1  # 10% increase in capital

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
        
        # Initialize logger
        self.logger = logging.getLogger(__name__)
        
        # Add resource locking mechanism early to prevent race conditions
        self.locked_balances = {}  # Tracks locked balances by asset
        self.balance_lock = threading.RLock()  # Thread-safe lock for balance operations
        
        # Apply optimized settings for small capital accounts ($64)
        # Increase grid levels for more frequent trading opportunities
        self.grid_levels = max(min(config.GRID_LEVELS, 5), 4)  # Enforce range of 4-5 grid levels for optimal capital distribution
        
        # Reduce grid spacing for tighter price capture
        self.grid_spacing = min(config.GRID_SPACING / 100, 0.003)  # Max 0.3% spacing
        
        # Add new attributes for dynamic capital allocation
        self.dynamic_grid_levels = max(min(config.GRID_LEVELS, 5), 4)
        self.min_capital_per_grid = config.MIN_NOTIONAL_VALUE * 1.1  # Add 10% buffer to minimum
        self.current_capital_per_grid = self.min_capital_per_grid  # Initialize with a default value
        
        # Initialize last capital recalibration timestamp
        self.last_capital_recalibration = 0
        
        # Initialize total available capital with the current total capital to avoid unintended recalibrations
        self.total_available_capital, _, _ = self._calculate_available_capital()
        
        # Log change to dynamic capital allocation
        self.logger.info("Using dynamic capital allocation instead of fixed CAPITAL_PER_LEVEL")
        
        # Reduce grid range to concentrate capital in smaller price movements
        self.grid_range_percent = min(config.GRID_RANGE_PERCENT / 100, 0.03)  # Max 3% range
        
        # Keep original timing parameters
        self.recalculation_period = config.RECALCULATION_PERIOD
        self.atr_period = config.ATR_PERIOD
        
        # Enhanced non-symmetric grid parameters for better capital efficiency
        # Concentrate more of the grid range in the core zone
        self.core_zone_percentage = max(getattr(config, 'CORE_ZONE_PERCENTAGE', 0.5), 0.7)  # At least 70% of range in core
        
        # Allocate more capital to the core price zone
        self.core_capital_ratio = max(getattr(config, 'CORE_CAPITAL_RATIO', 0.7), 0.8)  # At least 80% of capital in core
        
        # Place more grid points in the core zone for higher frequency trading
        self.core_grid_ratio = max(getattr(config, 'CORE_GRID_RATIO', 0.6), 0.7)  # At least 70% of grid points in core
        
        # Store previous ATR value for volatility change detection
        self.last_atr_value = None
        
        # Log the optimized settings
        self.logger.info(f"Using optimized grid settings for small capital: {self.grid_levels} levels, {self.grid_spacing*100:.2f}% spacing, {self.grid_range_percent*100:.2f}% range")
        self.logger.info(f"Core zone optimized: {self.core_zone_percentage*100:.1f}% of range, {self.core_capital_ratio*100:.1f}% of capital, {self.core_grid_ratio*100:.1f}% of grid points")
        
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
        
        # Get optimal grid parameters to estimate resource needs
        optimized_levels, capital_per_grid = self._optimize_grid_parameters()
        
        # Calculate temporary grid to estimate resource requirements
        temp_grid = self._calculate_grid_levels()
        
        # Calculate actual needed resources
        usdt_needed = 0
        base_needed = 0
        
        for level in temp_grid:
            price = level['price']
            # Use level's capital if available, or fall back to calculated capital_per_grid
            capital = level.get('capital', capital_per_grid)
            quantity = capital / price
            
            if level['side'] == 'BUY':
                # Buy orders need USDT
                usdt_needed += capital
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
        """Get the price precision from the symbol information"""
        try:
            symbol_info = self.binance_client.get_symbol_info(self.symbol)
            if not symbol_info or not symbol_info.get('filters'):
                self.logger.warning(f"Could not get symbol info or filters for {self.symbol}")
                return 4  # Default fallback
            
            # Extract price filter
            price_filter = next((f for f in symbol_info['filters'] if f['filterType'] == 'PRICE_FILTER'), None)
            
            if price_filter and 'tickSize' in price_filter:
                tick_size = float(price_filter['tickSize'])
                
                # Calculate precision based on tick size
                # (e.g. if tickSize is 0.0001, precision is 4)
                import math
                tick_size_str = f"{tick_size:.10f}".rstrip('0').rstrip('.')
                precision = len(tick_size_str.split('.')[-1]) if '.' in tick_size_str else 0
                
                self.logger.info(f"Trading pair {self.symbol} price precision: {precision}")
                
                # Store the tick size for later use
                self.tick_size = tick_size
                
                # 关键修改：直接返回实际精度，不强制最小值为4
                return precision
            else:
                self.logger.warning(f"Price filter not found for {self.symbol}")
                return 4  # Default fallback
        except Exception as e:
            self.logger.error(f"Error getting price precision: {e}")
            return 4  # Default fallback
    
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
        # Directly use format_price from utils - remove redundant validation
        return format_price(price, self.price_precision)
    
    def _adjust_quantity_precision(self, quantity):
        """
        Format quantity with appropriate precision for LOT_SIZE filter compliance
        
        Args:
            quantity (float): Original quantity value
            
        Returns:
            str: Formatted quantity string
        """
        # Directly use format_quantity from utils
        return format_quantity(quantity, self.quantity_precision)
    
    def _setup_grid(self):
        """Set up grid levels and place initial orders"""
        # Optimize grid parameters based on available capital
        optimized_levels, capital_per_grid = self._optimize_grid_parameters()
        
        # Update grid level count if needed
        if optimized_levels != self.dynamic_grid_levels:
            self.dynamic_grid_levels = optimized_levels
        
        # Store the current capital per grid for reference
        self.current_capital_per_grid = capital_per_grid
        self.total_available_capital, _, _ = self._calculate_available_capital()
        
        # Calculate grid levels
        self.grid = self._calculate_grid_levels()
        
        # Store current ATR value for future volatility comparison
        self.last_atr_value = self._get_current_atr()
        
        # Check if WebSocket API is available for potential batch operations
        client_status = self.binance_client.get_client_status()
        self.using_websocket = client_status["websocket_available"]
        
        # Place grid orders (using unified method)
        self._place_grid_orders()
        
        # Connect grid recalculation with risk management
        if self.telegram_bot and hasattr(self.telegram_bot, 'risk_manager'):
            # Calculate grid price range for risk manager
            grid_prices = [level['price'] for level in self.grid]
            grid_lowest = min(grid_prices)
            grid_highest = max(grid_prices)
            
            # Update risk manager with new grid boundaries
            self.telegram_bot.risk_manager.update_stop_loss_take_profit(grid_lowest, grid_highest)
            self.logger.info(f"Updated risk management bounds to match new grid: {grid_lowest:.8f} - {grid_highest:.8f}")
    
    def _calculate_grid_levels(self):
        """
        Calculate asymmetric grid price levels with funds concentrated near current price
        Optimized for dynamic capital allocation
        
        Returns:
            list: List of grid levels with prices, sides and capital allocation
        """
        # Get current market price 
        current_price = self.binance_client.get_symbol_price(self.symbol)
        self.current_market_price = current_price
        
        # Calculate grid range based on current price and adjusted grid_range_percent
        grid_range = current_price * self.grid_range_percent
        
        # Use dynamic grid levels instead of fixed config value
        grid_count = self.dynamic_grid_levels
        
        # Get klines for trend calculation - reuse from ATR calculation if possible
        klines = self.binance_client.get_historical_klines(
            symbol=self.symbol,
            interval="1h",
            limit=self.atr_period + 20  # Add extra for trend calculation
        )
        
        # Calculate trend strength and apply grid offset
        trend_strength = self._calculate_trend_strength(klines)
    
        # 1. First calculate safe grid boundaries
        safe_grid_range = current_price * self.grid_range_percent
        upper_bound = current_price + (safe_grid_range / 2)
        lower_bound = current_price - (safe_grid_range / 2)
    
        # 2. Calculate maximum safe offset range (ensuring core zone doesn't exceed total range)
        max_safe_offset = min(
            (upper_bound - current_price) * 0.8,  # 80% of distance to upper bound
            (current_price - lower_bound) * 0.8   # 80% of distance to lower bound
        ) * 0.5  # Additional 50% safety factor
    
        # 3. Apply trend strength with bounded offset using sigmoid function
        import math
        normalized_strength = max(min(trend_strength, 1.0), -1.0)
        sigmoid_factor = 2 / (1 + math.exp(-4 * abs(normalized_strength))) - 1  # Range 0-1
        trend_direction = 1 if normalized_strength > 0 else -1
        trend_offset = trend_direction * sigmoid_factor * max_safe_offset
    
        self.logger.info(f"Trend strength: {trend_strength:.2f}, Safe max offset: {max_safe_offset:.8f}, Applied offset: {trend_offset:.8f}")
    
        # Define core zone with higher percentage
        core_range = safe_grid_range * self.core_zone_percentage
    
        # Apply calculated trend offset to core zone
        core_center = current_price + trend_offset
    
        # Apply boundary constraints to core center first
        if core_center > (upper_bound + lower_bound) / 2:
            self.logger.warning(f"Adjusting core center: {core_center:.8f} too high")
            # Limit core center to not exceed middle of overall grid
            core_center = min(core_center, (upper_bound + lower_bound) / 2)
        elif core_center < (upper_bound + lower_bound) / 2:
            self.logger.warning(f"Adjusting core center: {core_center:.8f} too low")
            core_center = max(core_center, (upper_bound + lower_bound) / 2)
            # Ensure core center is not too low
    
        # Calculate core boundaries symmetrically around core center
        core_upper = min(core_center + (core_range / 2), upper_bound * 0.95)
        core_lower = max(core_center - (core_range / 2), lower_bound * 1.05)
    
        # Critical validation: ensure core_lower < core_upper
        if core_lower >= core_upper:
            self.logger.warning(f"Core boundary inversion detected: lower={core_lower:.8f}, upper={core_upper:.8f}")
            
            # Calculate midpoint and enforce minimum separation
            mid_point = (core_lower + core_upper) / 2
            min_separation = grid_range * 0.05  # Ensure at least 5% of grid range separation
            
            # Fix the boundaries
            core_upper = mid_point + (min_separation / 2)
            core_lower = mid_point - (min_separation / 2)
            
            self.logger.info(f"Fixed core boundaries: lower={core_lower:.8f}, upper={core_upper:.8f}")
    
        # Define number of grid levels in each zone based on dynamic grid count
        core_levels = int(grid_count * self.core_grid_ratio)
        edge_levels = grid_count - core_levels
        
        # Ensure minimum number of levels in each zone
        if core_levels < 2:
            core_levels = 2
        if edge_levels < 1:
            edge_levels = 1
            
        grid_levels = []
        
        # Calculate price step within core zone
        if core_levels > 1:
            core_step = (core_upper - core_lower) / (core_levels - 1) if core_levels > 1 else 0
            
            # Create grid levels in core zone
            for i in range(core_levels):
                level_price = core_lower + (i * core_step)
                
                # Determine buy/sell based on position relative to current price
                side = "SELL" if level_price >= current_price else "BUY"
                
                # Calculate capital for this level using dynamic method
                capital = self._calculate_dynamic_capital_for_level(level_price)
                
                grid_levels.append({
                    "price": level_price,
                    "side": side,
                    "order_id": None,
                    "capital": capital,
                    "timestamp": None
                })
        
        # Calculate upper edge levels if any
        if edge_levels > 0:
            # Ensure balanced distribution of edge levels
            if edge_levels > 1:
                # Calculate golden ratio based division for more natural distribution
                golden_ratio = 0.618
                upper_edge_levels = max(1, int(edge_levels * golden_ratio))
                lower_edge_levels = edge_levels - upper_edge_levels
                
                self.logger.debug(f"Edge levels distribution: {upper_edge_levels} upper, {lower_edge_levels} lower")
                
                # Adjust step sizes for smoother price progression
                if upper_edge_levels > 0:
                    # Use exponential step size for upper levels to ensure better coverage
                    upper_range = upper_bound - core_upper
                    upper_edge_step = upper_range / sum(1.2**i for i in range(upper_edge_levels))
                    
                    # Create upper edge levels with exponential spacing
                    upper_step_multiplier = 1.2
                    current_step = upper_edge_step
                    current_price_point = core_upper  # Renamed to avoid shadowing current_price
                    
                    for i in range(upper_edge_levels):
                        level_price = current_price_point + current_step
                        current_price_point = level_price
                        current_step *= upper_step_multiplier
                        
                        # Upper levels are always SELL
                        side = "SELL"
                        capital = self._calculate_dynamic_capital_for_level(level_price)
                        
                        grid_levels.append({
                            "price": level_price,
                            "side": side,
                            "order_id": None,
                            "capital": capital,
                            "timestamp": None
                        })
            
            # Calculate lower edge levels
            lower_edge_levels = edge_levels - (upper_edge_levels if 'upper_edge_levels' in locals() else 0)
            
            # Ensure lower edge levels are calculated only once and not overwritten
            if lower_edge_levels > 0:
                lower_edge_step = (core_lower - lower_bound) / lower_edge_levels if lower_edge_levels > 0 else 0
                
                # Create lower edge levels
                for i in range(lower_edge_levels):
                    level_price = core_lower - ((i + 1) * lower_edge_step)  # Start from beyond core zone
                    
                    # Lower levels are always BUY
                    side = "BUY"
                    
                    # Calculate edge zone capital using dynamic method
                    capital = self._calculate_dynamic_capital_for_level(level_price)
                    
                    grid_levels.append({
                        "price": level_price,
                        "side": side,
                        "order_id": None,
                        "capital": capital,
                        "timestamp": None
                    })
        
        # Sort grid levels by price
        grid_levels.sort(key=lambda x: x["price"])
        
        # Validate grid has at least one BUY and one SELL level
        has_buy = any(level["side"] == "BUY" for level in grid_levels)
        has_sell = any(level["side"] == "SELL" for level in grid_levels)
        
        if not has_buy or not has_sell:
            self.logger.warning(f"Grid calculation produced imbalanced grid: buy={has_buy}, sell={has_sell}")
            
            # Force at least one level of each if current grid is invalid
            if not has_buy and len(grid_levels) > 1:
                grid_levels[0]["side"] = "BUY"
            if not has_sell and len(grid_levels) > 1:
                grid_levels[-1]["side"] = "SELL"
        
        self.logger.info(f"Calculated {len(grid_levels)} grid levels: {core_levels} core, {edge_levels} edge")
        
        return grid_levels
    
    def _cancel_all_open_orders(self):
        """
        Cancel all open orders with improved error handling and state verification
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
                
                # Check order status before cancelling to avoid errors
                try:
                    order_status = self.binance_client.get_order_status(self.symbol, order['orderId'])
                    if order_status and order_status in ['FILLED', 'CANCELLED', 'REJECTED', 'EXPIRED']:
                        self.logger.info(f"Order {order['orderId']} already in terminal state: {order_status}, skipping cancel")
                        continue
                except Exception as status_error:
                    self.logger.warning(f"Error checking order status: {status_error}, proceeding with cancellation")
                        
                # Now proceed with cancellation
                result = self.binance_client.cancel_order(
                    symbol=self.symbol,
                    order_id=order['orderId']
                )
                self.logger.info(f"Order cancelled: {order['orderId']}")
            
            return True
        except Exception as e:
            self.logger.error(f"Error cancelling orders: {e}")
            return False
    
    # OPTIMIZED: Unified order placement method replacing both previous methods
    def _place_grid_orders(self):
        """
        Place all grid orders, prioritizing core zone for better capital utilization
        """
        # Get current balances
        base_asset = self.symbol.replace('USDT', '')
        quote_asset = 'USDT'
        base_balance = self.binance_client.check_balance(base_asset)
        quote_balance = self.binance_client.check_balance(quote_asset)
        
        # Account for locked balances
        with self.balance_lock:
            available_base = base_balance - self.locked_balances.get(base_asset, 0)
            available_quote = quote_balance - self.locked_balances.get(quote_asset, 0)
        
        # Sort grid levels by priority before placing orders
        current_price = self.current_market_price
        grid_range = current_price * self.grid_range_percent
        core_range = grid_range * self.core_zone_percentage
        core_upper = current_price + (core_range / 2)
        core_lower = current_price - (core_range / 2)
        
        # Create a list of levels with priorities
        grid_with_priority = []
        for level in self.grid:
            # Calculate priority (0-1) based on proximity to current price
            if core_lower <= level['price'] <= core_upper:
                # Core zone - higher priority the closer to current price
                priority = 1 - (abs(level['price'] - current_price) / (core_range/2))
            else:
                # Edge zone - lower priority
                priority = 0.3
                
            # Boost BUY priority slightly when below current price to create a buy bias
            if level['side'] == 'BUY' and level['price'] < current_price:
                priority *= 1.1  # 10% boost to BUY orders
                
            grid_with_priority.append((level, priority))
        
        # Sort by priority (highest first)
        grid_with_priority.sort(key=lambda x: x[1], reverse=True)
        
        # Place orders in priority order
        for level, _ in grid_with_priority:
            self._place_grid_order(level)
    
        self.logger.info(f"Placed grid orders with priority-based capital allocation")
    
    def _place_grid_order(self, level):
        """
        Place a single grid order with improved capital utilization
        
        Args:
            level: The grid level dictionary with order details
            
        Returns:
            bool: True if successful, False otherwise
        """
        price = level['price']
        side = level['side']
        
        # Get capital for this level (from level or calculate dynamically)
        if 'capital' in level and level['capital'] > 0:
            capital = level['capital']
        else:
            capital = self._calculate_dynamic_capital_for_level(price)
            level['capital'] = capital
        
        # Calculate order quantity based on level's capital
        quantity = capital / price
        
        # Additional log to track capital usage
        self.logger.debug(f"Order preparation: {side} at {price}, capital={capital:.2f}, quantity={quantity:.6f}")
        
        # Rest of method remains the same
        # Adjust quantity and price precision
        formatted_quantity = self._adjust_quantity_precision(quantity)
        formatted_price = self._adjust_price_precision(price)
        
        # Additional validation to ensure price isn't zero or invalid
        if formatted_price == "0" or float(formatted_price) <= 0:
            self.logger.error(f"Price formatting returned invalid value: '{formatted_price}' for {price}, skipping order")
            return False
            
        try:
            if self.simulation_mode:
                # In simulation mode, just log the order without placing it
                level['order_id'] = f"sim_{int(time.time())}_{side}_{formatted_price}"
                self.logger.info(f"Simulation - Would place order: {side} {formatted_quantity} @ {formatted_price}")
                return True
            
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
                return False
            
            # Double-check current balance after locking
            current_balance = self.binance_client.check_balance(asset)
            available = current_balance - self.locked_balances.get(asset, 0)
            
            if available < required:
                # We don't actually have enough funds - release the lock and abort
                self._release_funds(asset, required)
                self.logger.warning(f"Insufficient {asset} for order after locking. " 
                                   f"Required: {required}, Available: {available}")
                return False
                
            self.logger.debug(f"Double-verified {asset} funds for order: {required} available: {available}")
            
            try:
                # Generate a client order ID for tracking
                client_order_id = f"grid_{int(time.time())}_{side}_{formatted_price}"
                
                # Place order with appropriate client (WebSocket or REST)
                order = self.binance_client.place_limit_order(
                    self.symbol, 
                    side, 
                    formatted_quantity, 
                    formatted_price
                )
                
                # Update the grid level with order ID
                level['order_id'] = order['orderId']
                level['timestamp'] = int(time.time())  # Add timestamp for order age tracking
                
                # Add to pending orders for WebSocket response tracking (if using WebSocket)
                if self.using_websocket:
                    self.pending_orders[str(order['orderId'])] = {
                        'grid_index': self.grid.index(level),
                        'side': side,
                        'price': float(formatted_price),
                        'quantity': float(formatted_quantity),
                        'timestamp': int(time.time()),
                        'asset': asset,
                        'required': required
                    }
                
                self.logger.info(f"Order placed via {'WebSocket' if self.using_websocket else 'REST'}: "
                                f"{side} {formatted_quantity} @ {formatted_price}, ID: {order['orderId']}")
                return True
                
            except Exception as e:
                # Release the funds if order placement fails
                self._release_funds(asset, required)
                
                # Improved error logging with more details and stack trace
                self.logger.error(
                    f"Failed to place {side} order: qty={formatted_quantity}, price={formatted_price}", 
                    exc_info=True  # Add stack trace
                )
                self.logger.error(f"Error details: {str(e)}")
                
                # Add API response logging if available
                if hasattr(e, 'response') and e.response:
                    self.logger.error(f"API response: {e.response.text if hasattr(e.response, 'text') else str(e.response)}")
                
                # Check if connection was lost and retry with fallback
                if "connection" in str(e).lower() and self.using_websocket:
                    self.logger.warning("WebSocket connection issue detected, retrying with REST fallback...")
                    
                    # Update status and retry placement
                    client_status = self.binance_client.get_client_status()
                    self.using_websocket = False  # Force REST for fallback
                    
                    try:
                        # Try again with REST
                        order = self.binance_client.place_limit_order(
                            self.symbol, 
                            side, 
                            formatted_quantity, 
                            formatted_price
                        )
                        
                        level['order_id'] = order['orderId']
                        level['timestamp'] = int(time.time())
                        
                        self.logger.info(f"Order placed via REST fallback: {side} {formatted_quantity} @ {formatted_price}, "
                                        f"ID: {order['orderId']}")
                        return True
                    except Exception as retry_error:
                        # Still failed, make sure we release the funds
                        self._release_funds(asset, required)
                        self.logger.error(f"Fallback order placement also failed: {retry_error}")
                return False
        except Exception as e:
            self.logger.error(f"Error in order preparation: {e}")
            return False
    
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
            bool: True if grid was recalculated or adjusted, False otherwise
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
        partial_adjustment = False
        current_atr = self._get_current_atr()
        
        if current_atr and self.last_atr_value:
            atr_change = abs(current_atr - self.last_atr_value) / self.last_atr_value
            
            # Multi-level volatility response with different actions
            if atr_change > 0.2:  # Major volatility change (>20%)
                self.logger.info(f"Major volatility change detected: ATR changed by {atr_change*100:.2f}%, performing full grid recalculation")
                volatility_based_recalc = True
            elif atr_change > 0.1:  # Moderate volatility change (10-20%)
                self.logger.info(f"Moderate volatility change detected: ATR changed by {atr_change*100:.2f}%, performing partial grid adjustment")
                partial_adjustment = True
        
        # Handle full grid recalculation
        if time_based_recalc or volatility_based_recalc:
            try:
                # Cancel all orders
                self._cancel_all_open_orders()
                
                # Recalculate grid
                self._setup_grid()
                
                # Update last recalculation timestamp
                self.last_recalculation = now
                
                message = "Grid trading levels recalculated due to "
                message += "scheduled recalculation" if time_based_recalc else "significant volatility change"
                
                self.logger.info(message)
                if self.telegram_bot:
                    self.telegram_bot.send_message(message)
                
                # Add in check_grid_recalculation method, after performing full recalculation
                if self.telegram_bot and hasattr(self.telegram_bot, 'risk_manager'):
                    # When grid is recalculated, make sure risk manager is notified
                    risk_manager = self.telegram_bot.risk_manager
                    if risk_manager.is_active:
                        # Calculate grid price range
                        grid_prices = [level['price'] for level in self.grid]
                        grid_lowest = min(grid_prices)
                        grid_highest = max(grid_prices)
                        
                        # Update risk manager
                        risk_manager.update_stop_loss_take_profit(grid_lowest, grid_highest)
                        self.logger.info(f"Notified risk manager of grid recalculation: new bounds {grid_lowest:.8f} - {grid_highest:.8f}")
                
                # Add capital allocation recalibration
                recalibration_changed = self._recalibrate_capital_allocation()
                if recalibration_changed:
                    self.logger.info("Capital allocation parameters updated, checking for unfilled grid slots...")
                    self._check_for_unfilled_grid_slots()
                
                return True
            except Exception as e:
                self.logger.error(f"Failed to recalculate grid: {e}")
        
        # Handle partial grid adjustment (without full cancellation)
        elif partial_adjustment:
            try:
                # Get current price
                current_price = self.binance_client.get_symbol_price(self.symbol)
                
                # Adjust grid spacing based on new volatility
                old_spacing = self.grid_spacing
                volatility_ratio = current_atr / self.last_atr_value if self.last_atr_value else 1
                new_spacing = self.grid_spacing * volatility_ratio
                
                # Cap spacing adjustment to reasonable bounds (±30%)
                new_spacing = max(min(new_spacing, self.grid_spacing * 1.3), self.grid_spacing * 0.7)
                
                self.grid_spacing = new_spacing
                self.last_atr_value = current_atr  # Update ATR reference value
                
                # Only adjust stale orders to preserve good positions
                stale_orders_count = self._check_for_stale_orders()
                
                message = f"Partial grid adjustment: spacing changed from {old_spacing*100:.3f}% to {new_spacing*100:.3f}%, {stale_orders_count} stale orders rebalanced"
                self.logger.info(message)
                if self.telegram_bot:
                    self.telegram_bot.send_message(message)
                
                return stale_orders_count > 0
            except Exception as e:
                self.logger.error(f"Failed to perform partial grid adjustment: {e}")
        
        # Additionally check for stale orders
        stale_orders_count = self._check_for_stale_orders()
        if stale_orders_count > 0:
            self.logger.info(f"Rebalanced {stale_orders_count} stale orders")
        
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
        order_status, order_id, symbol, side, price, quantity = self._extract_order_data(order_data)
        
        # Skip if not our symbol or not filled
        if symbol != self.symbol or order_status != 'FILLED':
            return
            
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
        
        # Process the filled order and place the opposite order
        self._process_filled_order(matching_level, level_index, side, quantity, price)
    
    # OPTIMIZED: Extract order data method to reduce code duplication
    def _extract_order_data(self, order_data):
        """
        Extract essential order data from different formats
        
        Args:
            order_data: Order data object or dict
            
        Returns:
            tuple: (status, order_id, symbol, side, price, quantity)
        """
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
                return None, None, None, None, 0, 0
                
        return order_status, order_id, symbol, side, price, quantity
    
    # OPTIMIZED: Process filled order method separated from handler
    def _process_filled_order(self, matching_level, level_index, side, quantity, price):
        """
        Process a filled order and place the opposite order
        Add aggressive grid filling after order processing
        
        Args:
            matching_level: Grid level that was filled
            level_index: Index of the level in the grid
            side: Order side (BUY/SELL)
            quantity: Order quantity
            price: Order price
            
        Returns:
            bool: True if successful, False otherwise
        """
        # Create opposite order
        new_side = "SELL" if side == "BUY" else "BUY"
        
        # Adjust quantity precision
        formatted_quantity = self._adjust_quantity_precision(quantity)
        
        # Set price with increased spread for new order using config parameter
        buy_sell_spread = config.BUY_SELL_SPREAD / 100  # Convert percentage to decimal
        if new_side == "SELL":
            # For SELL orders after BUY executed
            new_price = price * (1 + buy_sell_spread)
            formatted_price = self._adjust_price_precision(new_price)
        else:
            # For BUY orders after SELL executed
            new_price = price * (1 - buy_sell_spread)
            formatted_price = self._adjust_price_precision(new_price)
        
        # Calculate expected profit and trading fees using config parameters
        expected_profit = abs(float(new_price) - float(price)) / float(price) * 100
        trading_fee = config.TRADING_FEE_RATE * 2  # Round-trip fee (buy + sell)
        
        # Only create reverse order if profit exceeds fees by configured margin
        if expected_profit <= trading_fee * config.PROFIT_MARGIN_MULTIPLIER:
            self.logger.info(f"Skipping reverse order - insufficient profit margin: {expected_profit:.4f}% vs required: {trading_fee * config.PROFIT_MARGIN_MULTIPLIER:.4f}%")
            
            # IMPORTANT: Place a replacement grid order to maintain grid density
            self._place_replacement_grid_order(level_index, float(price))
            return False
        
        # Use config value for minimum order check
        min_order_value = config.MIN_NOTIONAL_VALUE  # Use configured minimum notional value
        order_value = float(formatted_quantity) * float(formatted_price)
        if order_value < min_order_value:
            self.logger.info(f"Skipping small order - value too low: {order_value:.2f} USDT < {min_order_value} USDT")
            # Place replacement grid order to maintain grid density
            self._place_replacement_grid_order(level_index, float(price))
            return False
        
        # Double-check price formatting
        if formatted_price == "0" or float(formatted_price) <= 0:
            self.logger.error(f"Invalid formatted price: {formatted_price} for {price}, using minimum valid price")
            formatted_price = self._adjust_price_precision(self.tick_size)  # Use minimum valid price
        
        # Determine capital for dynamic allocation (replace fixed capital)
        capital = self._calculate_dynamic_capital_for_level(price)
        
        # Now place the order using the regular order placement logic
        if self.simulation_mode:
            self.logger.info(f"Simulation - Would place opposite order: {new_side} {formatted_quantity} @ {formatted_price}")
            matching_level['side'] = new_side
            matching_level['order_id'] = f"sim_{int(time.time())}_{new_side}_{formatted_price}"
            message = f"Grid trade executed (simulation): {side} {formatted_quantity} @ {price}\nOpposite order would be: {new_side} {formatted_quantity} @ {formatted_price}"
            if self.telegram_bot:
                self.telegram_bot.send_message(message)
            return True
        
        # Check and lock funds - prevents race conditions
        if new_side == 'BUY':
            asset = 'USDT'
            required = float(formatted_quantity) * float(formatted_price)
        else:  # SELL
            asset = self.symbol.replace('USDT', '')
            required = float(formatted_quantity)
        
        # Try to lock the funds
        if not self._lock_funds(asset, required):
            message = f"⚠️ Cannot place opposite order: Insufficient {asset} balance. Required: {required}, Available: {self.binance_client.check_balance(asset) - self.locked_balances.get(asset, 0)}"
            self.logger.warning(message)
            if self.telegram_bot:
                self.telegram_bot.send_message(message)
            return False
        
        # Place the order with error handling
        try:
            order = self.binance_client.place_limit_order(
                self.symbol, 
                new_side, 
                formatted_quantity, 
                formatted_price
            )
            
            # Update grid level
            matching_level['side'] = new_side
            matching_level['order_id'] = order['orderId']
            
            # Add to pending orders tracking if using WebSocket
            if self.using_websocket:
                self.pending_orders[str(order['orderId'])] = {
                    'grid_index': level_index,
                    'side': new_side,
                    'price': float(formatted_price),
                    'quantity': float(formatted_quantity),
                    'timestamp': int(time.time()),
                    'asset': asset,
                    'required': required
                }
            
            message = f"Grid trade executed: {side} {formatted_quantity} @ {price}\nOpposite order placed: {new_side} {formatted_quantity} @ {formatted_price}"
            self.logger.info(message)
            if self.telegram_bot:
                self.telegram_bot.send_message(message)
            return True
            
        except Exception as e:
            # Release locked funds if order placement fails
            self._release_funds(asset, required)
            self.logger.error(f"Failed to place opposite order: {e}")
            
            # If WebSocket connection error, try with REST API
            if "connection" in str(e).lower() and self.using_websocket:
                try:
                    self.logger.warning("Connection error - falling back to REST API for opposite order")
                    
                    # Force update of client status
                    client_status = self.binance_client.get_client_status()
                    self.using_websocket = False  # Force REST for fallback
                    
                    # Try again with REST
                    order = self.binance_client.place_limit_order(
                        self.symbol, 
                        new_side, 
                        formatted_quantity, 
                        formatted_price
                    )
                    
                    # Update grid
                    matching_level['side'] = new_side
                    matching_level['order_id'] = order['orderId']
                    
                    message = f"Grid trade executed: {side} {formatted_quantity} @ {price}\nOpposite order placed via fallback: {new_side} {formatted_quantity} @ {formatted_price}"
                    self.logger.info(message)
                    if self.telegram_bot:
                        self.telegram_bot.send_message(message)
                    return True
                except Exception as retry_error:
                    self._release_funds(asset, required)  # Make sure to release funds on retry failure
                    self.logger.error(f"Fallback order placement also failed: {retry_error}")
            return False
        
        # Immediately check for unfilled grid slots to maximize capital utilization
        # Do this after order processing to capture the newly freed capital
        threading.Thread(
            target=self._check_for_unfilled_grid_slots,
            daemon=True
        ).start()
        
        # Notify risk manager to update OCO orders after SELL order fills (creates base asset)
        if side == "SELL" and self.telegram_bot and hasattr(self.telegram_bot, 'risk_manager') and self.telegram_bot.risk_manager:
            risk_manager = self.telegram_bot.risk_manager
            if risk_manager.is_active:
                self.logger.info(f"SELL order filled, triggering OCO order update")
                # Use a background thread to avoid blocking
                threading.Thread(
                    target=risk_manager._check_for_missing_oco_orders,
                    daemon=True
                ).start()
                
        return True
    
    # OPTIMIZED: New helper method for capital calculation
    def _calculate_dynamic_capital_for_level(self, price):
        """
        Calculate capital allocation for a grid level based on its position
        Using a more aggressive allocation strategy that prioritizes grid trading
        
        Args:
            price: Price level for the grid
            
        Returns:
            float: Capital amount to allocate for this grid level
        """
        # Get reference values
        current_price = self.current_market_price
        
        # Get the core zone boundaries
        grid_range = current_price * self.grid_range_percent
        core_range = grid_range * self.core_zone_percentage
        core_upper = current_price + (core_range / 2)
        core_lower = current_price - (core_range / 2)
        
        # Calculate position factor (distance from current price)
        if core_lower <= price <= core_upper:
            # Core zone - higher capital allocation for levels closer to current price
            distance_factor = min(1, abs(price - current_price) / (core_range/2)) if core_range > 0 else 0
            position_factor = 1 - (distance_factor * 0.3)  # Scale from 1.0 (at current price) to 0.7 (at core boundary)
        else:
            # Edge zone - reduced but still significant capital allocation (80% of core maximum)
            position_factor = 0.8  # Higher edge allocation to maximize opportunity capture
    
        # If not initialized, calculate now
        if not hasattr(self, 'current_capital_per_grid'):
            _, capital_per_grid = self._optimize_grid_parameters()
            self.current_capital_per_grid = capital_per_grid
    
        # Calculate allocation for this level with priority on grid completeness
        allocation = self.current_capital_per_grid * position_factor
        
        # Ensure minimum notional value is met
        allocation = max(config.MIN_NOTIONAL_VALUE * 1.05, allocation)
        
        return allocation
    
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
        Improved fund release mechanism to prevent over-locking
        """
        with self.balance_lock:
            current_locked = self.locked_balances.get(asset, 0)
            
            # Additional check: log warning if trying to release more than locked
            if amount > current_locked:
                self.logger.warning(
                    f"Attempting to release more funds ({amount} {asset}) than currently locked ({current_locked} {asset}), "
                    f"possible fund tracking inconsistency"
                )
            
            # Prevent negative values
            release_amount = min(current_locked, amount)
            
            if release_amount > 0:
                self.locked_balances[asset] = max(0, current_locked - release_amount)
                self.logger.debug(f"Released {release_amount} {asset}, remaining locked: {self.locked_balances[asset]}")
    
    def _reset_locks(self):
        """Reset all fund locks when stopping grid or recalculating"""
        with self.balance_lock:
            previous_locks = self.locked_balances.copy()
            self.locked_balances = {}
            self.logger.info(f"Reset all fund locks. Previous locks: {previous_locks}")
    
    def _check_for_stale_orders(self):
        """Cancel and reallocate orders that have been open too long without execution"""
        if not self.is_running or self.simulation_mode:
            return 0
            
        current_time = int(time.time())
        max_order_age = 2 * 3600  # 2 hours in seconds
        
        # Track orders to cancel
        orders_to_cancel = []
        
        # Get current market price
        current_price = self.binance_client.get_symbol_price(self.symbol)
        
        # First identify stale orders far from current price
        for i, level in enumerate(self.grid):
            if level.get('order_id') and level.get('timestamp'):
                # Check age
                order_age = current_time - level['timestamp']
                
                # Dynamic price deviation threshold based on order age
                # The longer an order exists, the smaller the price deviation needed to cancel
                time_factor = min(1, order_age / max_order_age)
                deviation_threshold = 0.01 + (0.01 * (1 - time_factor))  
                
                # Calculate distance from current price
                price_distance = abs(level['price'] - current_price) / current_price
                
                # If order is old or far from price, cancel it
                if (order_age > max_order_age) or (price_distance > deviation_threshold):
                    self.logger.info(f"Marking stale order ID {level['order_id']} for cancellation, " 
                                    f"age: {order_age/3600:.1f} hours, "
                                    f"distance from price: {price_distance*100:.1f}%, "
                                    f"threshold: {deviation_threshold*100:.1f}%")
                    orders_to_cancel.append((i, level))
        
        # Cancel identified orders
        for i, level in orders_to_cancel:
            try:
                self.binance_client.cancel_order(symbol=self.symbol, order_id=level['order_id'])
                self.logger.info(f"Cancelled stale order ID {level['order_id']}")
                
                # Release locked funds
                if level['side'] == 'BUY':
                    asset = 'USDT'
                    quantity = level.get('capital', self.capital_per_level) / level['price']
                    formatted_quantity = self._adjust_quantity_precision(quantity)
                    self._release_funds(asset, float(formatted_quantity))
                else:
                    asset = self.symbol.replace('USDT', '')
                    quantity = level.get('capital', self.capital_per_level) / level['price']
                    self._release_funds(asset, quantity)
                    
                # Mark for recreation with updated parameters
                level['order_id'] = None
                
            except Exception as e:
                self.logger.error(f"Failed to cancel stale order: {e}")
        
        # Now place new orders for the cancelled ones, with updated price levels
        if orders_to_cancel:
            # Recalculate grid to get fresh price levels
            new_grid = self._calculate_grid_levels()
            
            # Update cancelled orders with new prices from similar positions in the new grid
            for i, old_level in orders_to_cancel:
                # Find corresponding position in new grid
                grid_position = i / len(self.grid)
                new_index = int(grid_position * len(new_grid))
                new_index = min(max(0, new_index), len(new_grid) - 1)
                
                # Update the level with new price but keep it at same relative position
                self.grid[i]['price'] = new_grid[new_index]['price']
                self.grid[i]['side'] = new_grid[new_index]['side']
                self.grid[i]['capital'] = new_grid[new_index]['capital']
                
                # Place the new order
                self._place_grid_order(self.grid[i])
                
        return len(orders_to_cancel)
    
    def update_symbol(self, new_symbol):
        """Update the trading symbol"""
        if self.is_running:
            self.stop()  # Stop current trading first
        
        # Save old symbol for logging
        old_symbol = self.symbol
        self.symbol = new_symbol
        
        # Get new symbol information and update precisions
        symbol_info = self.binance_client.get_symbol_info(self.symbol)
        
        # Reset and update precision values 
        self.price_precision = self._get_price_precision()
        self.quantity_precision = self._get_quantity_precision()
        
        # Re-calculate trading parameters based on new symbol
        self.base_asset = new_symbol.replace('USDT', '')
        self.quote_asset = 'USDT'  # Assuming all pairs are USDT based
        
        # Update other relevant properties
        self.orders = {}
        self.fund_lock = {self.base_asset: 0.0, self.quote_asset: 0.0}
        
        # Log the updated values
        self.logger.info(f"Symbol updated from {old_symbol} to {new_symbol}")
        self.logger.info(f"Price precision: {self.price_precision}")
        self.logger.info(f"Quantity precision: {self.quantity_precision}")
        
        if self.telegram_bot:
            self.telegram_bot.send_message(f"✅ Symbol updated from {old_symbol} to {new_symbol}")
        
        # Return True to indicate success
        return True
    
    def _place_replacement_grid_order(self, level_index, last_price):
        """
        Place a replacement grid order when the profit margin check fails,
        to ensure the grid density is maintained
        
        Args:
            level_index: Index of the level in the grid
            last_price: Price of the last filled order
        """
        # Get the current price to determine proper grid placement
        current_price = self.binance_client.get_symbol_price(self.symbol)
        
        # Determine if we should add a buy or sell level based on position relative to current price
        if last_price < current_price:
            # If the filled order was below current price, add a buy order
            side = "BUY"
            # Calculate a price below current price based on grid spacing
            price = current_price * (1 - self.grid_spacing)
        else:
            # If the filled order was above current price, add a sell order
            side = "SELL"
            # Calculate a price above current price based on grid spacing
            price = current_price * (1 + self.grid_spacing)
        
        # Create a new grid level
        new_level = {
            "price": price,
            "side": side,
            "order_id": None,
            "capital": self._calculate_dynamic_capital_for_level(price)
        }
        
        # Update the grid level and place the order
        self.grid[level_index] = new_level
        self._place_grid_order(new_level)
        
        self.logger.info(f"Placed replacement grid order: {side} at price {price:.8f} to maintain grid density")
    
    def _check_for_unfilled_grid_slots(self):
        """
        Aggressively check for grid slots without orders and fill them with ALL available funds.
        Ensures maximum capital utilization, leaving NO funds unused.
        
        Returns:
            int: Number of new orders placed
        """
        if not self.is_running or self.simulation_mode:
            return 0
            
        # Track new orders placed
        orders_placed = 0
        
        # Get current balances
        base_asset = self.symbol.replace('USDT', '')
        quote_asset = 'USDT'
        base_balance = self.binance_client.check_balance(base_asset) 
        quote_balance = self.binance_client.check_balance(quote_asset)
        
        # Account for locked balances
        with self.balance_lock:
            available_base = base_balance - self.locked_balances.get(base_asset, 0)
            available_quote = quote_balance - self.locked_balances.get(quote_asset, 0)
    
        # Log available capital for debugging
        self.logger.info(f"Checking unfilled grid slots. Available: {available_quote} USDT, {available_base} {base_asset}")
    
        # Update dynamic capital allocation whenever this method runs
        # This ensures we're always using updated capital calculations
        total_capital, _, _ = self._calculate_available_capital()
        
        # Always recalibrate to capture any change in capital
        self.total_available_capital = total_capital
        self._recalibrate_capital_allocation()
        
        # First, identify all unfilled grid slots and sort them by priority
        unfilled_slots = []
        for i, level in enumerate(self.grid):
            if not level.get('order_id'):
                # Update capital for this level based on current funds
                level['capital'] = self._calculate_dynamic_capital_for_level(level['price'])
        
                # Calculate priority with stronger current price bias
                current_price = self.current_market_price
                distance_from_current = abs(level['price'] - current_price) / current_price

                # More aggressive super-power priority algorithm
                # 1. Highest priority: BUY orders below current price
                # 2. Second priority: SELL orders above current price
                # 3. Lowest priority: Orders against trend (BUY above or SELL below)
                if level['side'] == 'BUY' and level['price'] < current_price:
                    # BUY below current price (optimal trade zone)
                    # Inversely proportional to distance, with exponential boost for closer orders
                    priority = 2.0 * (1.0 - min(distance_from_current * 5, 0.9))
                elif level['side'] == 'SELL' and level['price'] > current_price:
                    # SELL above current price (optimal trade zone)
                    priority = 1.5 * (1.0 - min(distance_from_current * 5, 0.9))
                else:
                    # Against trend - lower priority but still potentially valuable
                    priority = 0.5 * (1.0 - min(distance_from_current * 5, 0.9))
                    
                # Log priority calculation
                self.logger.debug(f"Unfilled slot priority: {level['side']} at {level['price']}, priority={priority:.2f}")
                
                unfilled_slots.append((i, level, priority))
    
        # Sort by priority (highest first)
        unfilled_slots.sort(key=lambda x: x[2], reverse=True)
    
        # Try to place orders for unfilled slots in priority order
        for i, level, priority in unfilled_slots:
            side = level['side']
            price = level['price']
        
            # Check if we have funds for this order
            if side == 'BUY':
                capital_needed = level['capital']
                
                # ULTRA-AGGRESSIVE: Use even very small amounts of capital (down to MIN_NOTIONAL)
                if available_quote >= config.MIN_NOTIONAL_VALUE and available_quote >= capital_needed * 0.6:  # Lowered from 0.9 to 0.6
                    # If we have at least 60% of needed capital, adjust the order to use what we have
                    if available_quote < capital_needed:
                        adjusted_capital = available_quote * 0.99  # Use 99% of available
                        level['capital'] = adjusted_capital
                        self.logger.info(f"Adjusted capital for BUY order: {capital_needed:.2f} → {adjusted_capital:.2f}")
                        capital_needed = adjusted_capital
                
                # We have funds, try to place the order
                if self._place_grid_order(level):
                    orders_placed += 1
                    available_quote -= capital_needed
                    self.logger.info(f"Filled unfunded BUY grid slot at price {price} with {capital_needed} USDT (priority={priority:.2f})")
            else:  # SELL
                quantity_needed = level['capital'] / price
            
                # ULTRA-AGGRESSIVE: Use even very small amounts if they meet minimum requirements
                min_quantity = config.MIN_NOTIONAL_VALUE / price
                if available_base >= min_quantity:
                    # If we have enough for minimum order but not full amount, adjust
                    if available_base < quantity_needed and available_base >= min_quantity:
                        adjusted_quantity = available_base * 0.99  # Use 99% of available
                        adjusted_capital = adjusted_quantity * price
                        level['capital'] = adjusted_capital
                        self.logger.info(f"Adjusted quantity for SELL order: {quantity_needed:.6f} → {adjusted_quantity:.6f}")
                        quantity_needed = adjusted_quantity
                
                    # We have funds, try to place the order
                    if self._place_grid_order(level):
                        orders_placed += 1
                        available_base -= quantity_needed
                        self.logger.info(f"Filled unfunded SELL grid slot at price {price} with {quantity_needed} {base_asset} (priority={priority:.2f})")
    
        if orders_placed > 0:
            self.logger.info(f"Filled {orders_placed} unfilled grid slots with MAXIMUM capital utilization")
            if self.telegram_bot:
                self.telegram_bot.send_message(f"✅ Added {orders_placed} grid orders with ultra-aggressive capital utilization")
                
            # Notify risk manager that grid orders have been placed
            if orders_placed > 0 and self.telegram_bot and hasattr(self.telegram_bot, 'risk_manager'):
                self.telegram_bot.risk_manager._check_for_missing_oco_orders()
        
            # Check if we've used everything - if not, try again with remaining funds
            # This recursive approach ensures we use EVERY bit of available capital
            remaining_base = self.binance_client.check_balance(base_asset) - self.locked_balances.get(base_asset, 0)
            remaining_quote = self.binance_client.check_balance(quote_asset) - self.locked_balances.get(quote_asset, 0)
        
            # If we have enough to meet minimum notional value, try again
            if remaining_quote >= config.MIN_NOTIONAL_VALUE or remaining_base >= (config.MIN_NOTIONAL_VALUE / self.current_market_price):
                self.logger.info(f"Still have unused funds: {remaining_quote:.2f} USDT, {remaining_base:.6f} {base_asset}. Trying secondary allocation.")
                
            # If remaining funds are sufficient for another slot, create a background task
            threading.Thread(
                target=self._adjust_grid_for_remaining_funds,
                args=(remaining_base, remaining_quote),
                daemon=True
            ).start()
            return orders_placed
    
    def _calculate_trend_strength(self, klines, lookback=20):
        """
        Calculate market trend strength on a scale from -1 to 1
        -1: Strong downtrend
         0: No trend
        +1: Strong uptrend
        
        Args:
            klines: List of kline/candlestick data
            lookback: Number of periods to consider
            
        Returns:
            float: Trend strength value between -1 and 1
        """
        try:
            # Ensure we have enough data
            if not klines or len(klines) < lookback + 1:
                return 0
            
            # Extract close prices
            closes = []
            for k in klines[-lookback-1:]:
                # Handle both array format and dict format
                if isinstance(k, list):
                    closes.append(float(k[4]))  # Close price is at index 4
                elif isinstance(k, dict) and 'close' in k:
                    closes.append(float(k['close']))
                    
            if not closes or len(closes) < lookback + 1:
                return 0
                
            # Calculate price change momentum and direction
            changes = []
            for i in range(1, len(closes)):
                change_pct = (closes[i] - closes[i-1]) / closes[i-1]
                changes.append(change_pct)
                
            # Get recent trend direction
            short_trend = sum(changes[-5:]) if len(changes) >= 5 else 0
            
            # Get overall trend momentum considering more weight on recent changes
            weights = [0.5 + (i/lookback/2) for i in range(lookback)]  # Increasing weights
            if len(changes) < len(weights):
                weights = weights[-len(changes):]
                
            weighted_changes = [changes[i] * weights[i] for i in range(len(changes))]
            overall_trend = sum(weighted_changes)
            
            # Combine short and overall trend with more weight on recent
            combined_trend = (short_trend * 0.7) + (overall_trend * 0.3)
            
            # Normalize between -1 and 1 with proper scaling
            normalized_trend = max(min(combined_trend * 50, 1.0), -1.0)
            
            self.logger.debug(f"Calculated trend strength: {normalized_trend:.2f}")
            return normalized_trend
            
        except Exception as e:
            self.logger.error(f"Error calculating trend strength: {e}")
            return 0  # Default to no trend on error

    # Add a new periodic consistency check method
    def _verify_grid_consistency(self):
        """Verify grid state matches actual open orders and correct discrepancies"""
        if not self.is_running or self.simulation_mode:
            return
            
        try:
            # Get all actual open orders from exchange
            open_orders = self.binance_client.get_open_orders(self.symbol)
            exchange_order_ids = set(str(order['orderId']) for order in open_orders)
            
            # Track grid orders that don't exist on exchange
            phantom_orders = []
            
            # Check grid orders against exchange data
            for i, level in enumerate(self.grid):
                if level.get('order_id'):
                    grid_order_id = str(level['order_id'])
                    
                    # If order in grid but not on exchange
                    if grid_order_id not in exchange_order_ids:
                        phantom_orders.append((i, level))
                        self.logger.warning(f"Grid inconsistency: Order {grid_order_id} in grid but not found on exchange")
            
            # Fix phantom orders
            for i, level in phantom_orders:
                self.logger.info(f"Fixing phantom order in grid level {i}: {level}")
                
                # Release any locked funds
                if level['side'] == 'BUY':
                    asset = 'USDT'
                    capital = level.get('capital', self.capital_per_level)
                    self._release_funds(asset, capital)
                else:
                    asset = self.symbol.replace('USDT', '')
                    quantity = level.get('capital', self.capital_per_level) / level['price']
                    self._release_funds(asset, quantity)
                
                # Reset order ID
                level['order_id'] = None
                
                # Try to place a new order if possible
                self._place_grid_order(level)
                
            if phantom_orders:
                self.logger.info(f"Grid consistency check: fixed {len(phantom_orders)} phantom orders")
                
            return len(phantom_orders)
        except Exception as e:
            self.logger.error(f"Error during grid consistency check: {e}")
            return 0

    def _calculate_available_capital(self):
        """
        Calculate total available capital for grid trading considering both quote and base assets
        
        Returns:
            tuple: (total_quote_value_in_usdt, available_base_asset, available_quote_asset)
        """
        base_asset = self.symbol.replace('USDT', '')
        quote_asset = 'USDT'
        
        # Get raw balances
        base_balance = self.binance_client.check_balance(base_asset)
        quote_balance = self.binance_client.check_balance(quote_asset)
        
        # Get current price for converting base asset to quote value
        current_price = self.binance_client.get_symbol_price(self.symbol)
        
        # Account for locked balances
        with self.balance_lock:
            available_base = max(0, base_balance - self.locked_balances.get(base_asset, 0))
            available_quote = max(0, quote_balance - self.locked_balances.get(quote_asset, 0))
        
        # Convert base asset to quote asset value and calculate total
        base_in_quote = available_base * current_price
        total_in_quote = base_in_quote + available_quote
        
        # Log the available capital
        self.logger.info(f"Available capital: {available_quote} {quote_asset} + {available_base} {base_asset} (≈{base_in_quote}{quote_asset}) = {total_in_quote} {quote_asset}")
        
        return total_in_quote, available_base, available_quote

    def _optimize_grid_parameters(self):
        """
        Dynamically optimize grid levels and capital allocation based on available funds
        Prioritizes using ALL available capital for grid trading
        
        Returns:
            tuple: (optimized_grid_levels, capital_per_grid)
        """
        total_capital, available_base, available_quote = self._calculate_available_capital()
        current_price = self.binance_client.get_symbol_price(self.symbol)
        
        # Start with configured grid levels
        target_grid_levels = self.dynamic_grid_levels
        
        # Calculate minimum capital needed for this many grids
        min_capital_needed = target_grid_levels * self.min_capital_per_grid
        
        # If we don't have enough capital, reduce the number of grids
        if total_capital < min_capital_needed:
            # Calculate maximum affordable grid levels
            max_affordable_levels = max(2, int(total_capital / self.min_capital_per_grid))
            
            # Update grid levels count
            target_grid_levels = max_affordable_levels
            self.logger.warning(
                f"Insufficient capital for {self.dynamic_grid_levels} grid levels. "
                f"Reducing to {target_grid_levels} levels based on available capital ({total_capital} USDT)"
            )

        # Calculate capital per grid level - use ALL available capital
        capital_per_grid = total_capital / target_grid_levels
        
        # Ensure each grid meets minimum notional value
        if capital_per_grid < self.min_capital_per_grid:
            capital_per_grid = self.min_capital_per_grid
            # Recalculate grid levels with this capital per grid
            target_grid_levels = max(2, int(total_capital / capital_per_grid))
            self.logger.warning(
                f"Adjusting to {target_grid_levels} grid levels with{capital_per_grid} USDT per grid "
                f"to meet minimum notional value ({config.MIN_NOTIONAL_VALUE} USDT)"
            )
        
        return target_grid_levels, capital_per_grid

    def _recalibrate_capital_allocation(self):
        """
        Aggressively recalibrate capital allocation based on current balances
        Uses even very small amounts of newly available capital
        
        Returns:
            bool: True if recalibration changed parameters, False otherwise
        """
        # Calculate new optimized parameters
        new_levels, new_capital = self._optimize_grid_parameters()
        
        # Save previous values for comparison
        previous_levels = self.dynamic_grid_levels
        previous_capital = getattr(self, 'current_capital_per_grid', 0)
        
        # Check if significant changes occurred
        levels_changed = previous_levels != new_levels
        
        # ANY capital change is now significant - maximizing capital utilization
        # Change threshold reduced from 10% to 5%
        capital_changed = previous_capital != 0 and abs(new_capital - previous_capital) / previous_capital > 0.05
        
        # Even without significant change, check for any additional capital
        current_total, _, _ = self._calculate_available_capital()
        previous_total = getattr(self, 'total_available_capital', 0)
        additional_capital = current_total - previous_total
        
        # If there's ANY additional capital above minimum notional, recalibrate
        capital_increase = additional_capital > config.MIN_NOTIONAL_VALUE
        
        if levels_changed or capital_changed or capital_increase:
            # Log appropriate message based on what changed
            if capital_increase:
                self.logger.info(f"Additional {additional_capital:.2f} USDT detected, recalibrating grid")
            else:
                self.logger.info(
                    f"Capital allocation recalibrated: Grid levels {previous_levels}->{new_levels}, "
                    f"Capital per grid {previous_capital:.2f}->{new_capital:.2f} USDT"
                )
            
            # Update parameters
            self.dynamic_grid_levels = new_levels
            self.current_capital_per_grid = new_capital
            self.total_available_capital = current_total  # Update the total tracking
            
            # Store recalibration time
            self.last_capital_recalibration = time.time()
            return True
            
        # Update current parameters even if no significant change    
        self.dynamic_grid_levels = new_levels
        self.current_capital_per_grid = new_capital
        self.total_available_capital = current_total
        
        # Just update the timestamp
        self.last_capital_recalibration = time.time()
        return False

    def _count_unfilled_grid_slots(self):
        """
        Count number of unfilled grid slots that need capital
        
        Returns:
            int: Number of grid slots without active orders
        """
        if not self.is_running or self.simulation_mode:
            return 0
            
        # Count slots without orders
        unfilled_count = sum(1 for level in self.grid if not level.get('order_id'))
        
        # If we have any unfilled slots, check if we have funds to fill them
        if unfilled_count > 0:
            base_asset = self.symbol.replace('USDT', '')
            quote_asset = 'USDT'
            
            # Get current balances
            base_balance = self.binance_client.check_balance(base_asset)
            quote_balance = self.binance_client.check_balance(quote_asset)
            
            # Account for locked balances
            with self.balance_lock:
                available_base = base_balance - self.locked_balances.get(base_asset, 0)
                available_quote = quote_balance - self.locked_balances.get(quote_asset, 0)
            
            # Check if we have funds to fill at least one slot
            have_funds_for_slots = False
            
            for level in self.grid:
                if not level.get('order_id'):
                    side = level['side']
                    price = level['price']
                    capital = level.get('capital', self._calculate_dynamic_capital_for_level(price))
                    
                    if side == 'BUY' and available_quote >= capital:
                        have_funds_for_slots = True
                        break
                    elif side == 'SELL':
                        quantity_needed = capital / price
                        if available_base >= quantity_needed:
                            have_funds_for_slots = True
                            break
            
            # Only return unfilled count if we actually have funds to fill slots
            if have_funds_for_slots:
                return unfilled_count
            else:
                self.logger.debug(f"Found {unfilled_count} unfilled slots but insufficient funds to fill any")
                return 0
                
        return unfilled_count
