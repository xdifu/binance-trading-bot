import logging
import math
import time
import threading
from datetime import datetime, timedelta
from binance_api.client import BinanceClient
from binance.exceptions import BinanceAPIException  # 需添加此导入
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
        
        # Initialize logger first to enable logging throughout initialization
        self.logger = logging.getLogger(__name__)
        
        # Initialize required parameters before they're used
        self.recalculation_period = config.RECALCULATION_PERIOD
        self.atr_period = config.ATR_PERIOD
        
        # Get symbol information and set precision
        self.symbol_info = self._get_symbol_info()
        
        # Store tickSize and stepSize directly from filters
        self.tick_size = self._get_tick_size()
        self.step_size = self._get_step_size()
        self.price_precision = self._get_price_precision()
        self.quantity_precision = self._get_quantity_precision()
        
        # Get current market price
        self.current_market_price = self.binance_client.get_symbol_price(self.symbol)
        
        # Apply optimized settings for small capital accounts ($64)
        # Increase grid levels for more frequent trading opportunities
        self.grid_levels = max(min(config.GRID_LEVELS, 12), 5)  # Enforce range of 5-12 grid levels
        
        # Calculate dynamic grid spacing based on market volatility using ATR
        initial_atr = self._get_current_atr()
        if initial_atr:
            # Use ATR_RATIO from config for spacing calculation
            atr_ratio = config.ATR_RATIO
            atr_based_spacing = initial_atr / self.current_market_price * atr_ratio
            
            # Set minimum spacing to prevent overly dense grids
            min_spacing = 0.001  # 0.1% minimum spacing
            
            # Apply constraints using config values for maximum limits
            self.grid_spacing = max(min_spacing, min(config.GRID_SPACING / 100, atr_based_spacing, config.MAX_GRID_SPACING))
            
            self.logger.info(f"Dynamic grid spacing set to {self.grid_spacing*100:.2f}% based on ATR={initial_atr:.6f}")
        else:
            # Fallback with configured maximum when ATR calculation fails
            self.grid_spacing = min(config.GRID_SPACING / 100, config.MAX_GRID_SPACING)
            self.logger.info(f"Using fallback grid spacing: {self.grid_spacing*100:.2f}%")
        
        # Rest of the initialization remains unchanged
        # Use configured maximum for grid range limit
        self.grid_range_percent = min(config.GRID_RANGE_PERCENT / 100, config.MAX_GRID_RANGE)
        
        # Preserve original capital per level setting
        self.capital_per_level = config.CAPITAL_PER_LEVEL
        
        # Enhanced non-symmetric grid parameters
        self.core_zone_percentage = max(getattr(config, 'CORE_ZONE_PERCENTAGE', 0.5), 0.7)
        self.core_capital_ratio = max(getattr(config, 'CORE_CAPITAL_RATIO', 0.7), 0.8)
        self.core_grid_ratio = max(getattr(config, 'CORE_GRID_RATIO', 0.6), 0.7)
        
        # Store previous ATR value for volatility change detection
        self.last_atr_value = None
        
        # Log the optimized settings
        self.logger.info(f"Using optimized grid settings: {self.grid_levels} levels, {self.grid_spacing*100:.2f}% spacing")
        
        # Further initialization code...
        self.grid = []  # [{'price': float, 'order_id': int, 'side': str}]
        self.last_recalculation = None
        self.is_running = False
        self.simulation_mode = False  # Add simulation mode flag
        
        # Track pending operations for better error handling
        self.pending_orders = {}  # Track orders waiting for WebSocket confirmation
        
        # Replace locked_balances with pending_locks
        self.pending_locks = {}  # Track temporarily locked funds before order submission
        self.balance_lock = threading.RLock()  # Thread-safe lock for balance operations
        
        # Initialize trend tracking attribute
        self.trend_strength = 0
    
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
    
    def _get_fallback_tick_size(self):
        """
        Get the fallback tick size dynamically or from configuration.
        
        Returns:
            float: Fallback tick size value.
        """
        # Example: Fetch from configuration or set a default value
        return getattr(config, 'FALLBACK_TICK_SIZE', 0.00000001)

    def _is_symbol_info_valid(self):
        """
        Check if symbol information is valid and contains filters.

        Returns:
            bool: True if valid, False otherwise.
        """
        return self.symbol_info and 'filters' in self.symbol_info

    def _get_tick_size(self):
        """
        Get tick size directly from symbol filters
        
        Returns:
            float: Tick size value for price (default 0.00000001 if not found)
        """
        if not self._is_symbol_info_valid():
            error_msg = f"Symbol information is invalid for {self.symbol}. Cannot calculate grid levels."
            self.logger.error(error_msg)
            raise ValueError(error_msg)  # Raise an exception to stop further execution
        
        for f in self.symbol_info['filters']:
            if f['filterType'] == 'PRICE_FILTER':
                tick_size = float(f['tickSize'])
                self.logger.debug(f"Found tick size for {self.symbol}: {tick_size}")
                return tick_size
        fallback_tick_size = self._get_fallback_tick_size()
        self.logger.warning(f"No PRICE_FILTER found for {self.symbol}, using fallback tick size {fallback_tick_size}")
        return fallback_tick_size  # Default if not found
    
    def _get_step_size(self):
        """
        Get step size directly from symbol filters
        
        Returns:
            float: Step size value for quantity (default 1.0 if not found)
        """
        if not self._is_symbol_info_valid():
            self.logger.warning(f"Symbol info missing for {self.symbol}. This might occur if the trading pair is invalid, delisted, or there is a connection issue. Using minimum step size 1.0")
            return 1.0  # Default step size
        
        for f in self.symbol_info['filters']:
            if f['filterType'] == 'LOT_SIZE':
                if 'stepSize' in f:
                    step_size = float(f['stepSize'])
                    self.logger.debug(f"Found step size for trading pair {self.symbol}: {step_size}")
                    return step_size
                else:
                    self.logger.warning(f"'stepSize' not found in LOT_SIZE filter for {self.symbol}. Filter details: {f}")
                    continue
                
        fallback_step_size = getattr(config, 'FALLBACK_STEP_SIZE', 1.0)  # Configurable fallback
        self.logger.warning(f"No LOT_SIZE filter found for {self.symbol}, using fallback step size {fallback_step_size}")
        return fallback_step_size  # Use configurable fallback
    
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
        if not self._is_symbol_info_valid():
            error_msg = f"Symbol info is invalid for {self.symbol}. Cannot calculate grid levels."
            self.logger.error(error_msg)
            if self.telegram_bot:
                self.telegram_bot.send_message(f"⚠️ {error_msg}")
            return f"Error: {error_msg}"
        
        temp_grid = self._calculate_grid_levels()
        if not temp_grid:
            error_msg = "Failed to calculate grid levels. The grid is empty or invalid."
            self.logger.error(error_msg)
            if self.telegram_bot:
                self.telegram_bot.send_message(f"⚠️ {error_msg}")
            return f"Error: {error_msg}"
        
        # Calculate actual needed resources
        usdt_needed = 0
        base_needed = 0
        
        for level in temp_grid:
            price = level['price']
            if price > 0:
                quantity = self.capital_per_level / price
            else:
                self.logger.error(f"Invalid price value: {price}. Skipping grid level.")
                continue
            
            if level['side'] == 'BUY':
                # Buy orders need USDT
                usdt_needed += self.capital_per_level
            else:
                # Sell orders need base asset
                base_needed += quantity
        
        # Check balances
        base_balance = self.binance_client.check_balance(base_asset)
        quote_balance = self.binance_client.check_balance(quote_asset)
        
        available_base = base_balance - self.pending_locks.get(base_asset, 0)
        available_quote = quote_balance - self.pending_locks.get(quote_asset, 0)

        # Initialize warning collection and fund status flag
        warnings = []
        insufficient_funds = False

        # Check if we have enough quote asset (USDT)
        tolerance = 1e-6  # Small tolerance to account for floating-point rounding issues
        if available_quote + tolerance < usdt_needed and not simulation:
            warnings.append(f"Insufficient {quote_asset}: Required {usdt_needed:.2f}, Available {available_quote:.2f}")
            insufficient_funds = True

        # Also check if we have enough base asset (e.g., BTC, ETH)
        if available_base + tolerance < base_needed and not simulation:
            warnings.append(f"Insufficient {base_asset}: Required {base_needed:.2f}, Available {available_base:.2f}")
            insufficient_funds = True

        # Single warning message block for all fund insufficiency issues
        if insufficient_funds and not simulation:
            warning_message = f"⚠️ Warning: {' '.join(warnings)}\nStarting in limited mode."
            self.logger.warning(warning_message)
            if self.telegram_bot:
                self.telegram_bot.send_message(warning_message)
        
        # Cancel all previous open orders
        self._cancel_all_open_orders()
        
        simulation_status = "Simulation mode: ON" if self.simulation_mode else "Simulation mode: OFF"
        message = f"Grid trading system started!\nCurrent price: {self.current_market_price}\nGrid range: {len(self.grid)} levels\nUsing {api_type}\n{simulation_status}"
        self._setup_grid()
        
        message = f"Grid trading system started!\nCurrent price: {self.current_market_price}\nGrid range: {len(self.grid)} levels\nUsing {api_type}"
        self.logger.info(message)
        if self.telegram_bot:
            self.telegram_bot.send_message(message)
        
        return message
    
    def start_balanced_grid(self, simulation=False):
        """
        Start grid trading with initial asset balance optimization.
        Ensures sufficient base asset by performing an initial purchase if necessary.
        
        Args:
            simulation: Whether to run in simulation mode (no real orders)
            
        Returns:
            str: Status message
        """
        if self.is_running:
            return "System already running"
        
        # Get current market price
        current_price = self.binance_client.get_symbol_price(self.symbol)
        if not current_price or current_price <= 0:
            error_msg = f"Failed to get valid market price for {self.symbol}"
            self.logger.error(error_msg)
            return f"Error: {error_msg}"
        
        # Check asset balances
        base_asset = self.symbol.replace('USDT', '')
        base_balance = self.binance_client.check_balance(base_asset)
        usdt_balance = self.binance_client.check_balance('USDT')
        
        # Check if base asset is severely lacking (< 10% of portfolio)
        base_value_in_usdt = base_balance * current_price
        total_value_in_usdt = base_value_in_usdt + usdt_balance
        
        # Only proceed with balance adjustment if needed and not in simulation mode
        initial_purchase_made = False
        if base_value_in_usdt < (total_value_in_usdt * 0.1) and not simulation:
            # Use 40% of available USDT to purchase base asset
            purchase_usdt = usdt_balance * 0.4
            quantity_to_buy = purchase_usdt / current_price
            
            # Format quantity according to exchange requirements
            formatted_quantity = self._adjust_quantity_precision(quantity_to_buy)
            
            # Notify about the planned purchase
            message = f"Balancing portfolio: Converting {purchase_usdt:.4f} USDT to ~{formatted_quantity} {base_asset}"
            self.logger.info(message)
            
            if self.telegram_bot:
                self.telegram_bot.send_message(f"⚙️ {message}")
            
            try:
                # Execute the market order
                order = self.binance_client.place_market_order(
                    self.symbol, 
                    "BUY", 
                    formatted_quantity
                )
                initial_purchase_made = True
                
                # Log the successful purchase
                self.logger.info(f"Initial balance purchase complete. New {base_asset} balance: {self.binance_client.check_balance(base_asset)}")
                
                # Allow time for balances to update
                time.sleep(2)
                
            except Exception as e:
                self.logger.error(f"Failed to execute initial balance purchase: {e}")
                if self.telegram_bot:
                    self.telegram_bot.send_message(f"⚠️ Failed to execute initial balance purchase: {e}")
        
        # Start the normal grid trading process
        start_result = self.start(simulation)
        
        if initial_purchase_made:
            return f"Initial balance adjusted. {start_result}"
        return start_result
    
    def stop(self):
        """Stop grid trading system"""
        if not self.is_running:
            return "System already stopped"
        
        try:
            # Cancel all open orders
            if not self.simulation_mode:
                if not self._cancel_all_open_orders():
                    self.logger.error("Failed to cancel all open orders. Stopping further execution.")
            # Reset all fund locks
            try:
                self._reset_locks()
            except Exception as e:
                self.logger.error(f"Error resetting locks: {e}")
                
            self.grid = []  # Clear grid
            self.pending_orders = {}  # Clear pending_orders tracking
            self.last_recalculation = None
            self.is_running = False
            # Reset internal state
            stop_message = "Grid trading system stopped"
            self.logger.info(stop_message)
            return stop_message
            
        except Exception as e:
            error_message = f"Error stopping grid trading: {e}"
            self.logger.error(error_message)
            return error_message
    
    def _get_price_precision(self):
        """Get the price precision from the symbol information"""
        try:
            if not self.symbol_info or not self.symbol_info.get('filters'):
                self.logger.warning(f"Could not get symbol info or filters for {self.symbol}")
                return 4  # Default fallback
            
            # Extract price filter
            price_filter = next((f for f in self.symbol_info['filters'] if f['filterType'] == 'PRICE_FILTER'), None)
            if not price_filter:
                self.logger.warning(f"No PRICE_FILTER found for {self.symbol}. Using default precision of 4.")
                return 4  # Default fallback precision
            
            if price_filter and 'tickSize' in price_filter:
                tick_size = float(price_filter['tickSize'])
                
                # Calculate precision based on tick size
                # (e.g. if tickSize is 0.0001, precision is 4)
                import math
                precision = -int(math.log10(tick_size))
                
                self.logger.info(f"Trading pair {self.symbol} price precision: {precision}")
                
                # Store the tick size for later use
                self.tick_size = tick_size
                
                # Key modification: Directly return actual precision without forcing a minimum value of 4
                # Ensure precision is a valid integer, fallback to default if invalid
                try:
                    return int(precision)
                except (TypeError, ValueError):
                    self.logger.warning(f"Invalid precision value '{precision}' for {self.symbol}. Falling back to default precision of 5.")
                    return 5  # Default fallback precision
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
        if not self._is_symbol_info_valid():
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
        # Validate that quantity is a valid number
        if not isinstance(quantity, (int, float)) or quantity <= 0:
            self.logger.error(f"Invalid quantity value: {quantity}")
            raise ValueError(f"Invalid quantity value: {quantity}")
        
        # Directly use format_quantity from utils
        return format_quantity(quantity, self.quantity_precision)
        Returns:
            str: Formatted quantity string
        """
        # Directly use format_quantity from utils
        return format_quantity(quantity, self.quantity_precision)
    
    def _setup_grid(self):
        """
        Set up grid levels and place initial orders.
        
        This method calculates grid levels based on current market price and ATR.
        It includes fallback mechanisms for ATR calculation failures.
        """
        # Calculate grid levels
        self.grid = self._calculate_grid_levels()
        
        # Check if grid calculation succeeded
        if not self.grid:
            self.logger.error("Grid levels calculation returned an empty list. Aborting grid setup.")
            return
        
        # Update ATR value for volatility tracking
        atr_value = self._get_current_atr()
        if atr_value is not None:
            self.last_atr_value = atr_value
        elif self.last_atr_value is None:
            # Set fallback ATR value only if we don't already have one
            self.logger.warning("ATR calculation failed. Using fallback value of 0.01 for grid setup.")
            self.last_atr_value = 0.01  # Fallback value to ensure grid setup proceeds
        
        # Store current trend strength for change monitoring
        self.last_trend_strength = self.trend_strength
        
        # Check WebSocket API availability
        client_status = self.binance_client.get_client_status()
        self.using_websocket = client_status["websocket_available"]
        
        # Place grid orders using the unified method
        self._initialize_grid_orders()
    
    def _calculate_grid_levels(self):
        """
        Calculate asymmetric grid price levels with funds concentrated near current price.
        Optimized for small capital accounts with trend adaptation.
        
        The grid is structured with:
        - A core zone with higher density near the current price
        - Edge zones with fewer levels at the extremes
        - Dynamic capital allocation based on distance from current price
        - Trend-based positioning to adapt to market direction
        
        Returns:
            list: List of grid levels with prices, sides and capital allocation
        """
        # --- Step 1: Calculate basic grid parameters ---
        try:
            # Calculate maximum possible grid count based on available funds
            total_available_usdt = self.binance_client.check_balance('USDT')
            adjusted_grid_levels = min(self.grid_levels, int(total_available_usdt / (self.capital_per_level * 0.8)))
            if adjusted_grid_levels != self.grid_levels:
                self.logger.info(f"Adjusted grid levels from {self.grid_levels} to {adjusted_grid_levels} based on available funds: {total_available_usdt} USDT")
                self.grid_levels = max(adjusted_grid_levels, 3)  # Ensure at least 3 grid levels

            # Get current market price 
            current_price = self.binance_client.get_symbol_price(self.symbol)
            if current_price <= 0:
                self.logger.error(f"Invalid current price: {current_price}")
                return []
                
            self.current_market_price = current_price
            grid_range = current_price * self.grid_range_percent
            
            # --- Step 2: Calculate trend-based offset and strength ---
            trend_strength = self.calculate_trend_strength(current_price=current_price)
            
            # --- Step 3: Define grid boundaries with asymmetric trend adaptation ---
            # Overall grid boundaries adjusted by trend direction
            trend_factor = 0.5 + (trend_strength * 0.3)  # Range 0.2-0.8 based on trend
            upper_bound = current_price + (grid_range * trend_factor)
            lower_bound = current_price - (grid_range * (1 - trend_factor))

            # Core zone maintains proportional distance from current price
            core_range = grid_range * self.core_zone_percentage
            core_upper = current_price + (core_range * trend_factor)
            core_lower = current_price - (core_range * (1 - trend_factor))
            
            # Ensure core zone has valid width
            if core_upper <= core_lower + (self.tick_size * 5):
                # Maintain minimum core zone width
                core_center = (core_upper + core_lower) / 2
                min_width = core_range * 0.3  # At least 30% of original width
                core_upper = core_center + (min_width / 2)
                core_lower = core_center - (min_width / 2)
            
            # --- Step 4: Distribute grid levels ---
            # Calculate actual width of core and edge zones
            core_width = core_upper - core_lower
            edge_width = (upper_bound - core_upper) + (core_lower - lower_bound)

            # Determine volatility-adjusted grid spacing
            # Use last known ATR or fallback to 1% of current price
            atr_value = self.last_atr_value or (current_price * 0.01)
            # Set ideal spacing to 20% of ATR, bounded between 0.5%-2% of price
            ideal_spacing = max(current_price * 0.005, min(atr_value * 0.2, current_price * 0.02))

            # Calculate required grid levels based on actual zone widths
            # Ensure minimum of 2 levels in core zone for proper trading
            core_levels_needed = max(int(core_width / ideal_spacing), 2)
            # Use 20% wider spacing in edge zones for better capital efficiency
            edge_levels_needed = max(int(edge_width / (ideal_spacing * 1.2)), 1)

            # Adjust if total exceeds grid level limit
            total_needed = core_levels_needed + edge_levels_needed
            if total_needed > self.grid_levels:
                # Scale down proportionally while preserving minimums
                ratio = self.grid_levels / total_needed
                core_levels = max(int(core_levels_needed * ratio), 2)
                edge_levels = max(self.grid_levels - core_levels, 1)
            else:
                # Use calculated values directly
                core_levels = core_levels_needed
                edge_levels = edge_levels_needed

            # Log the dynamic allocation details
            self.logger.debug(f"Dynamic grid allocation: ideal spacing={ideal_spacing/current_price*100:.2f}%, "
                             f"core zone={core_levels} points, edge zone={edge_levels} points")

            grid_levels = []
            
            # Create core zone grid levels
            core_grid = self._create_core_zone_grid(current_price, core_upper, core_lower, core_levels)
            if not core_grid:
                self.logger.error("Failed to create core zone grid")
                return []
            grid_levels.extend(core_grid)
            
            # Create edge zone grid levels if needed
            if edge_levels > 0:
                edge_grid = self._create_edge_zone_grid(
                    current_price, core_upper, core_lower, upper_bound, lower_bound, edge_levels
                )
                grid_levels.extend(edge_grid)
            
            # --- Step 5: Sort and validate the grid ---
            grid_levels.sort(key=lambda x: x["price"])
            grid_levels = self._ensure_balanced_grid(grid_levels, upper_bound, lower_bound)
            
            return grid_levels
            
        except Exception as e:
            self.logger.error(f"Error calculating grid levels: {e}", exc_info=True)
            return []
            
    def calculate_trend_strength(self, current_price=None, klines=None, lookback=20):
        """
        Calculate market trend strength on a scale from -1 to 1
        
        Args:
            current_price: Optional current market price (will fetch data if klines not provided)
            klines: Optional list of kline/candlestick data (will be fetched if not provided)
            lookback: Number of periods to consider for trend calculation
            
        Returns:
            float: trend_strength - Value between -1 and 1 indicating trend direction and strength
        """
        try:
            # Step 1: Get klines if not provided
            if klines is None:
                klines = self.binance_client.get_historical_klines(
                    symbol=self.symbol,
                    interval="1h",
                    limit=self.atr_period + lookback
                )
            
            # Step 2: Ensure we have enough data
            if not klines or len(klines) < lookback + 1:
                self.logger.warning("Insufficient kline data for trend calculation")
                return 0
            
            # Step 3: Extract close prices
            closes = []
            for k in klines[-lookback-1:]:
                # Handle both array format and dict format
                if isinstance(k, list):
                    closes.append(float(k[4]))  # Close price is at index 4
                elif isinstance(k, dict) and 'close' in k:
                    closes.append(float(k['close']))
                    
            if not closes or len(closes) < lookback + 1:
                self.logger.warning("Could not extract enough close prices from kline data")
                return 0
                
            # Step 4: Calculate price change momentum and direction
            changes = []
            for i in range(1, len(closes)):
                change_pct = (closes[i] - closes[i-1]) / closes[i-1]
                changes.append(change_pct)
                
            # Step 5: Get recent trend direction (last 5 periods)
            short_trend = sum(changes[-5:]) if len(changes) >= 5 else 0
            
            # Step 6: Get overall trend momentum with time-weighted changes
            weights = [0.5 + (i/lookback/2) for i in range(lookback)]  # Increasing weights
            if len(changes) < len(weights):
                weights = weights[-len(changes):]
                
            weighted_changes = [changes[i] * weights[i] for i in range(len(changes))]
            overall_trend = sum(weighted_changes)
            
            # Step 7: Combine short and overall trend with more weight on recent
            combined_trend = (short_trend * 0.7) + (overall_trend * 0.3)
            
            # Step 8: Normalize between -1 and 1 with proper scaling
            trend_strength = max(min(combined_trend * 50, 1.0), -1.0)
            
            # Step 9: Log results
            trend_multiplier = getattr(config, 'TREND_MULTIPLIER', 0.05)
            self.logger.info(
                f"Detected trend strength: {trend_strength:.2f}, "
                f"effective adjustment: {(trend_strength*0.3*100):.1f}% of grid range"
            )
            self.logger.debug(f"Calculated trend strength: {trend_strength:.2f}")
            
            # Store calculated trend strength as instance attribute for use in grid creation
            self.trend_strength = trend_strength
            
            return trend_strength
            
        except Exception as e:
            self.logger.error(f"Error calculating trend strength: {e}")
            return 0  # Default to no trend on error
    
    def _create_core_zone_grid(self, current_price, core_upper, core_lower, core_levels):
        """
        Create grid levels for the core price zone with trend-adaptive buy/sell distribution
        
        Args:
            current_price: Current market price
            core_upper: Upper bound of core zone
            core_lower: Lower bound of core zone
            core_levels: Number of grid levels in core zone
            
        Returns:
            list: Grid levels in the core zone
        """
        core_grid = []
        
        # Ensure core zone has valid width
        if core_upper <= core_lower:
            core_upper = core_lower + self.tick_size * 5  # Ensure minimum separation
            self.logger.warning(
                f"Core zone has zero or negative width. "
                f"Adjusted core_upper to {core_upper:.8f}, core_lower: {core_lower:.8f}"
            )
        
        # Validate core_levels to prevent division by zero
        if core_levels < 2:
            self.logger.error(f"Invalid core_levels value: {core_levels}. Must be at least 2.")
            return []
        
        # Get trend strength to optimize buy/sell distribution
        trend_strength = self.trend_strength  # Direct access instead of getattr
        
        # Adjust buy/sell ratio based on trend (more buys in downtrend, more sells in uptrend)
        # Range is 0.3 (strong uptrend) to 0.7 (strong downtrend)
        buy_ratio = 0.5 + (trend_strength * -0.2)
        buy_ratio = max(0.3, min(0.7, buy_ratio))
        
        # Calculate number of buy and sell levels
        buy_levels = max(int(core_levels * buy_ratio), 1)  # At least 1 buy level
        sell_levels = core_levels - buy_levels
        
        # Ensure at least one level of each type
        if sell_levels < 1:
            sell_levels = 1
            buy_levels = core_levels - 1
        
        self.logger.debug(f"Core zone distribution: {buy_levels} buy levels, {sell_levels} sell levels (trend strength: {trend_strength:.2f})")
        
        # Calculate step sizes for buy and sell zones
        buy_step = (current_price - core_lower) / buy_levels if buy_levels > 0 else 0
        sell_step = (core_upper - current_price) / sell_levels if sell_levels > 0 else 0
        
        # Create buy levels below current price
        for i in range(buy_levels):
            level_price = current_price - ((i + 1) * buy_step)
            capital = self._calculate_dynamic_capital_for_level(level_price)
            
            core_grid.append({
                "price": level_price,
                "side": "BUY",
                "order_id": None,
                "capital": capital,
                "timestamp": None
            })
        
        # Create sell levels above current price
        for i in range(sell_levels):
            level_price = current_price + ((i + 1) * sell_step)
            capital = self._calculate_dynamic_capital_for_level(level_price)
            
            core_grid.append({
                "price": level_price,
                "side": "SELL",
                "order_id": None,
                "capital": capital,
                "timestamp": None
            })
        
        return core_grid
    
    def _create_edge_zone_grid(self, current_price, core_upper, core_lower, upper_bound, lower_bound, edge_levels):
        """
        Create grid levels for the upper and lower edge zones
        
        Args:
            current_price: Current market price
            core_upper: Upper bound of core zone
            core_lower: Lower bound of core zone
            upper_bound: Upper bound of entire grid
            lower_bound: Lower bound of entire grid
            edge_levels: Total number of grid levels in edge zones
            
        Returns:
            list: Grid levels in the edge zones
        """
        edge_grid = []
        
        # Split remaining levels between upper and lower edges
        upper_edge_levels = edge_levels // 2
        lower_edge_levels = edge_levels - upper_edge_levels
        
        # Create upper edge levels
        if upper_edge_levels > 0 and core_upper < upper_bound:
            upper_edge_step = (upper_bound - core_upper) / upper_edge_levels
            
            for i in range(upper_edge_levels):
                level_price = core_upper + ((i + 1) * upper_edge_step)
                capital = self._calculate_dynamic_capital_for_level(level_price)
                
                edge_grid.append({
                    "price": level_price,
                    "side": "SELL",  # Upper levels are always SELL
                    "order_id": None,
                    "capital": capital,
                    "timestamp": None
                })
        
        # Create lower edge levels
        if lower_edge_levels > 0 and core_lower > lower_bound:
            lower_edge_step = (core_lower - lower_bound) / lower_edge_levels
            
            for i in range(lower_edge_levels):
                level_price = core_lower - ((i + 1) * lower_edge_step)
                capital = self._calculate_dynamic_capital_for_level(level_price)
                
                edge_grid.append({
                    "price": level_price,
                    "side": "BUY",  # Lower levels are always BUY
                    "order_id": None,
                    "capital": capital,
                    "timestamp": None
                })
                
        return edge_grid
    
    def _ensure_balanced_grid(self, grid_levels, upper_bound, lower_bound):
        """
        Ensure grid has at least one BUY and one SELL level
        
        Args:
            grid_levels: Current grid levels
            upper_bound: Upper bound of grid
            lower_bound: Lower bound of grid
            
        Returns:
            list: Balanced grid with at least one BUY and one SELL level
        """
        # Check if grid is empty
        if not grid_levels:
            self.logger.warning("Empty grid detected. Creating minimal balanced grid.")
            return self._create_minimal_grid(upper_bound, lower_bound)
        
        # Check if we have at least one buy and one sell level
        has_buy = any(level["side"] == "BUY" for level in grid_levels)
        has_sell = any(level["side"] == "SELL" for level in grid_levels)
        
        if not has_buy or not has_sell:
            self.logger.warning(f"Grid calculation produced imbalanced grid: buy={has_buy}, sell={has_sell}")
            
            # Add a BUY level if needed
            if not has_buy:
                try:
                    adjusted_lower_bound = float(self._adjust_price_precision(lower_bound))
                    buy_level = {
                        "price": adjusted_lower_bound,
                        "side": "BUY",
                        "order_id": None,
                        "capital": self._calculate_dynamic_capital_for_level(adjusted_lower_bound),
                        "timestamp": None
                    }
                    grid_levels.insert(0, buy_level)
                    self.logger.info(f"Added a BUY level at price {adjusted_lower_bound:.8f} to balance the grid")
                except Exception as e:
                    self.logger.error(f"Failed to add BUY level: {e}")
            
            # Add a SELL level if needed
            if not has_sell:
                try:
                    adjusted_upper_bound = float(self._adjust_price_precision(upper_bound))
                    sell_level = {
                        "price": adjusted_upper_bound,
                        "side": "SELL",
                        "order_id": None,
                        "capital": self._calculate_dynamic_capital_for_level(adjusted_upper_bound),
                        "timestamp": None
                    }
                    grid_levels.append(sell_level)
                    self.logger.info(f"Added a SELL level at price {adjusted_upper_bound:.8f} to balance the grid")
                except Exception as e:
                    self.logger.error(f"Failed to add SELL level: {e}")
        
        return grid_levels
    
    def _create_minimal_grid(self, upper_bound, lower_bound):
        """
        Create a minimal grid with just a BUY and a SELL level
        
        Args:
            upper_bound: Upper bound of grid
            lower_bound: Lower bound of grid
            
        Returns:
            list: Minimal grid with one BUY and one SELL level
        """
        minimal_grid = []
        
        try:
            # Add minimal BUY level at lower bound
            adjusted_lower_bound = float(self._adjust_price_precision(lower_bound))
            buy_level = {
                "price": adjusted_lower_bound,
                "side": "BUY",
                "order_id": None,
                "capital": self._calculate_dynamic_capital_for_level(adjusted_lower_bound),
                "timestamp": None
            }
            minimal_grid.append(buy_level)
            
            # Add minimal SELL level at upper bound
            adjusted_upper_bound = float(self._adjust_price_precision(upper_bound))
            sell_level = {
                "price": adjusted_upper_bound,
                "side": "SELL",
                "order_id": None,
                "capital": self._calculate_dynamic_capital_for_level(adjusted_upper_bound),
                "timestamp": None
            }
            minimal_grid.append(sell_level)
            
            self.logger.info("Created minimal grid with one BUY and one SELL level")
        except Exception as e:
            self.logger.error(f"Failed to create minimal grid: {e}")
            
        return minimal_grid
    
    def _cancel_all_open_orders(self):
        """Cancel all open orders with comprehensive error handling"""
        try:
            # Get all open orders for the symbol
            open_orders = self.binance_client.get_open_orders(self.symbol)
            
            for order in open_orders:
                try:
                    # Attempt to cancel each order
                    self.binance_client.cancel_order(
                        symbol=self.symbol, 
                        order_id=order['orderId']
                    )
                    self.logger.info(f"Cancelled order {order['orderId']}")
                    
                except Exception as e:
                    # Handle specific Binance error codes
                    if isinstance(e, BinanceAPIException):
                        # -2011: CANCEL_REJECTED (order doesn't exist/already filled)
                        # -2011: "Unknown order sent" (legacy code)
                        if e.code == -2011 or "Unknown order sent" in str(e):  # 修正重复的错误码
                            self.logger.warning(
                                f"Order {order['orderId']} cancellation rejected. "
                                "Likely already filled or expired. Skipping."
                            )
                            continue  # Skip to next order instead of failing
                            
                    # Re-raise unexpected errors
                    raise e
                    
            return True
            
        except Exception as e:
            self.logger.error(f"Error cancelling orders: {e}")
            return False
    
    def _initialize_grid_orders(self):
        """
        Place all grid orders with unified logic for both WebSocket and REST APIs.
        Processes all grid levels sequentially.
        """
        orders_placed = 0
        for level in self.grid:
            # Validate grid level data before placing the order
            if 'price' not in level or 'side' not in level or level['price'] <= 0 or level['side'] not in ['BUY', 'SELL']:
                self.logger.error(f"Invalid grid level data: {level}. Skipping order placement.")
                continue
                
            # Call the single order placement method for each level
            if self._place_grid_order(level):
                orders_placed += 1
        
        # Check for additional funds that can be used for more buy orders
        available_usdt = self.binance_client.check_balance('USDT') - self.pending_locks.get('USDT', 0)
        additional_levels = int(available_usdt / (self.capital_per_level * 1.1))  # Use slightly higher capital requirement for safety
        
        if additional_levels > 0:
            self.logger.info(f"Found unused USDT: {available_usdt}, creating {additional_levels} additional buy levels")
            added_orders = self._add_additional_buy_levels(additional_levels)
            orders_placed += added_orders
        
        self.logger.info(f"Grid setup complete: {orders_placed} orders placed out of {len(self.grid)} grid levels")
        return orders_placed > 0
    
    def _place_grid_order(self, level):
        """
        Place a single grid order with unified WebSocket/REST handling
        
        Args:
            level: The grid level dictionary with order details
            
        Returns:
            bool: True if successful, False otherwise
        """
        price = level['price']
        side = level['side']
        
        # Get capital for this level (use dynamic capital if available, otherwise fall back to default)
        capital = level.get('capital', self.capital_per_level)
        
        # Validate price before proceeding
        if price <= 0:
            self.logger.error(f"Invalid price value: {price} for {side} order, skipping")
            return False
            
        # Calculate order quantity based on level's capital
        quantity = capital / price
        
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
                
                # IMPORTANT: Release the temporary lock since order is now placed
                # and Binance has already accounted for these funds
                self._release_funds(asset, required)
                
                self.logger.info(f"Order placed via {'WebSocket' if self.using_websocket else 'REST'}: "
                                f"{side} {formatted_quantity} @ {formatted_price}, ID: {order['orderId']}")
                return True
                
            except Exception as e:
                # Release the funds if order placement fails
                self._release_funds(asset, required)
                self.logger.error(f"Failed to place order: {side} {formatted_quantity} @ {formatted_price}, Error: {e}")
                
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
        
        # Determine capital for dynamic allocation
        capital = self._calculate_dynamic_capital_for_level(float(formatted_price))
        
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
            message = f"⚠️ Cannot place opposite order: Insufficient {asset} balance. Required: {required}, Available: {self.binance_client.check_balance(asset) - self.pending_locks.get(asset, 0)}"
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
        
        # Notify risk manager to update OCO orders after SELL order fills (creates base asset)
        if side == "SELL" and self.telegram_bot and hasattr(self.telegram_bot, 'risk_manager') and self.telegram_bot.risk_manager:
            risk_manager = self.telegram_bot.risk_manager
            if risk_manager.is_active:
                self.logger.info(f"SELL order filled, triggering OCO order update")
                risk_manager._check_for_missing_oco_orders()

    # OPTIMIZED: New helper method for capital calculation
    def _calculate_dynamic_capital_for_level(self, price):
        """
        Calculate appropriate capital allocation based on price zone
        
        Args:
            price: The price level for the order
            
        Returns:
            float: The capital allocation in USDT for this price level
        """
        # Get current price and calculate zones properly each time
        current_price = self.current_market_price
        grid_range = current_price * self.grid_range_percent
        core_range = grid_range * self.core_zone_percentage
        core_upper = current_price + (core_range / 2)
        core_lower = current_price - (core_range / 2)

        # Base capital from configuration - use full amount
        base_capital = self.capital_per_level
        
        # Calculate dynamic allocation based on price zone
        if core_lower <= price <= core_upper:
            # Core zone - enhanced capital allocation
            distance_factor = 1 - min(1, abs(price - current_price) / (core_range/2)) if core_range > 0 else 0
            
            # Multiplier INCREASES capital by up to 50%
            capital_multiplier = 1 + (distance_factor * 0.5)
            
            # Use full base capital with enhancement
            allocation = base_capital * capital_multiplier
        else:
            # Edge zone - slight reduction but still substantial
            edge_discount = 0.9  # Use 90% of base capital (was 70%)
            
            # Still maintains significant capital
            allocation = base_capital * edge_discount
        
        # Ensure we meet minimum notional value required by exchange
        min_required_capital = config.MIN_NOTIONAL_VALUE
        return max(min_required_capital, allocation)
    
    def _lock_funds(self, asset, amount):
        """
        Temporarily lock funds for an order about to be submitted to Binance
        
        Args:
            asset: Asset symbol (e.g., 'BTC', 'USDT')
            amount: Amount to lock
            
        Returns:
            bool: True if funds were successfully locked, False otherwise
        """
        with self.balance_lock:
            # Get directly available balance from Binance (already accounts for open orders)
            available = self.binance_client.check_balance(asset)
            
            # Check if we have enough available balance
            if available < amount:
                self.logger.warning(
                    f"Insufficient {asset} balance for order: "
                    f"Required: {amount}, Available: {available}"
                )
                return False
            
            # Lock the funds temporarily until order is submitted
            self.pending_locks[asset] = self.pending_locks.get(asset, 0) + amount
            self.logger.debug(
                f"Temporarily locked {amount} {asset}, pending locks: {self.pending_locks[asset]}"
            )
            return True
    
    def _release_funds(self, asset, amount):
        """
        Release temporarily locked funds after order submission or failure
        
        Args:
            asset: Asset symbol (e.g., 'BTC', 'USDT')
            amount: Amount to release
        """
        with self.balance_lock:
            current_pending = self.pending_locks.get(asset, 0)
            
            # Ensure we don't release more than what's pending
            release_amount = min(current_pending, amount)
            
            if release_amount > 0:
                self.pending_locks[asset] = current_pending - release_amount
                self.logger.debug(f"Released {release_amount} {asset} from pending locks, remaining: {self.pending_locks.get(asset, 0)}")
    
    def _reset_locks(self):
        """Reset all temporary fund locks when stopping grid or recalculating"""
        with self.balance_lock:
            previous_locks = self.pending_locks.copy()
            self.pending_locks = {}
            self.logger.info(f"Reset all pending locks. Previous locks: {previous_locks}")
    
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
                
                # Just mark for recreation with updated parameters
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
