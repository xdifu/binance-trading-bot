import logging
import math
import time
import threading
from datetime import datetime, timedelta
from binance_api.client import BinanceClient
from binance.error import ClientError  # Use binance-connector's exception class
from utils.indicators import calculate_atr
from utils.format_utils import format_price, format_quantity, get_precision_from_filters
import config
from enum import Enum
import numpy as np

class MarketState(Enum):
    """
    Enum representing different market states that affect grid trading behavior
    
    States:
    - RANGING: Normal sideways market, ideal for grid trading
    - BREAKOUT: Early trend formation with increased directional movement
    - CRASH: Rapid downward price movement
    - PUMP: Rapid upward price movement
    """
    RANGING = 1     # Sideways market, ideal for grid trading
    BREAKOUT = 2    # Early trend formation, requires caution
    CRASH = 3       # Sudden downward price movement
    PUMP = 4        # Sudden upward price movement

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
        protective_default = getattr(config, "ENABLE_PROTECTIVE_MODE", False)
        auto_non_major = getattr(config, "AUTO_PROTECTIVE_FOR_NON_MAJOR", True)
        major_list = set(getattr(config, "MAJOR_SYMBOLS", []))
        self.protection_enabled = protective_default or (auto_non_major and self.symbol not in major_list)
        
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
        self.config_grid_levels = max(min(config.GRID_LEVELS, 12), 5)  # Preserve configured grid level target
        self.current_grid_levels = self.config_grid_levels  # Track live grid level usage without modifying the target
        self.grid_levels = self.config_grid_levels  # Backward compatibility alias for external references
        
        # Calculate dynamic grid spacing based on market volatility using ATR
        initial_atr = self._get_current_atr()
        if initial_atr:
            # Use ATR_RATIO from config for spacing calculation
            atr_ratio = config.ATR_RATIO
            atr_based_spacing = initial_atr / self.current_market_price * atr_ratio
            
            # Set minimum spacing to prevent overly dense grids (0.1% minimum)
            min_spacing = 0.001
            
            # Apply constraints using config values for maximum limits
            # Note: All values are already decimal, so no division needed
            self.grid_spacing = max(min_spacing, min(config.GRID_SPACING, atr_based_spacing, config.MAX_GRID_SPACING))
            
            self.logger.info(f"Dynamic grid spacing set to {self.grid_spacing*100:.2f}% based on ATR={initial_atr:.6f}")
        else:
            # Fallback with configured values when ATR calculation fails
            self.grid_spacing = min(config.GRID_SPACING, config.MAX_GRID_SPACING)
            self.logger.info(f"Using fallback grid spacing: {self.grid_spacing*100:.2f}%")
        
        # CRITICAL FIX: Initialize K-line cache BEFORE any market metrics calculation
        self._kline_cache = {}
        self._kline_cache_ttl = 900  # 15 minutes TTL
        self._kline_cache_lock = threading.Lock()
        
        # Use grid range directly since values are already decimal
        self.grid_range_percent = min(config.GRID_RANGE_PERCENT, config.MAX_GRID_RANGE)
        
        # Preserve original capital per level setting
        self.capital_per_level = config.CAPITAL_PER_LEVEL
        
        # Enhanced non-symmetric grid parameters
        self.core_zone_percentage = max(getattr(config, 'CORE_ZONE_PERCENTAGE', 0.5), 0.7)
        self.core_capital_ratio = max(getattr(config, 'CORE_CAPITAL_RATIO', 0.7), 0.8)
        self.core_grid_ratio = max(getattr(config, 'CORE_GRID_RATIO', 0.6), 0.7)
        
        # Store previous ATR value for volatility change detection
        self.last_atr_value = None
        
        # Log the optimized settings
        self.logger.info(f"Using optimized grid settings: {self.config_grid_levels} levels, {self.grid_spacing*100:.2f}% spacing")
        
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
        
        # Initialize market state related attributes
        self.current_market_state = MarketState.RANGING
        self.last_state_change_time = time.time()
        self.state_cooldown_period = 900  # 15-minute cooldown to prevent frequent state changes
        self.volume_history = []
        self.price_history = []
        self.history_max_size = 30  # Maximum length of historical data to maintain
        # Trend-driven level scaling
        self.level_reduction_factor = 1.0
        
        # Smart grid recalculation tracking (Event-driven with confirmation)
        self.out_of_bounds_start_time = None  # Timestamp when price first went out of bounds
        self.out_of_bounds_confirmation_threshold = 30 * 60  # 30 minutes in seconds (optimized from 2h)
        self.price_out_of_upper_bound = False  # Track if price is above upper limit
        self.price_out_of_lower_bound = False  # Track if price is below lower limit
    
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
    
    def _get_cached_klines(self, symbol, interval, limit):
        """
        Get cached K-line data to reduce API calls and avoid rate limits
        
        Args:
            symbol: Trading pair symbol
            interval: K-line interval (e.g., '4h', '15m')
            limit: Number of K-lines to fetch
            
        Returns:
            list: K-line data or None on failure
        """
        cache_key = f"{symbol}_{interval}_{limit}"
        current_time = time.time()
        
        with self._kline_cache_lock:
            # Check if cached data exists and is still valid
            if cache_key in self._kline_cache:
                cached_data, timestamp = self._kline_cache[cache_key]
                if current_time - timestamp < self._kline_cache_ttl:
                    self.logger.debug(f"Using cached klines for {cache_key} (age: {int(current_time - timestamp)}s)")
                    return cached_data
            
            # Cache miss or expired, fetch new data
            try:
                klines = self.binance_client.get_historical_klines(
                    symbol=symbol,
                    interval=interval,
                    limit=limit
                )
                if klines:
                    self._kline_cache[cache_key] = (klines, current_time)
                    self.logger.debug(f"Fetched and cached {len(klines)} klines for {cache_key}")
                    return klines
                else:
                    self.logger.warning(f"Received empty klines for {cache_key}")
                    # If we have stale cache, return it
                    if cache_key in self._kline_cache:
                        self.logger.warning(f"Using stale cache for {cache_key}")
                        return self._kline_cache[cache_key][0]
                    return None
            except Exception as e:
                self.logger.error(f"Failed to fetch klines for {cache_key}: {e}")
                # Return stale cache if available
                if cache_key in self._kline_cache:
                    self.logger.warning(f"API error, using stale cache for {cache_key}")
                    return self._kline_cache[cache_key][0]
                return None
    
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
            self.is_running = False
            return f"Error: {error_msg}"
        
        temp_grid = self._calculate_grid_levels()
        if not temp_grid:
            error_msg = "Failed to calculate grid levels. The grid is empty or invalid."
            self.logger.error(error_msg)
            if self.telegram_bot:
                self.telegram_bot.send_message(f"⚠️ {error_msg}")
            self.is_running = False
            return f"Error: {error_msg}"
        
        # Calculate actual needed resources
        usdt_needed = 0
        base_needed = 0
        
        for level in temp_grid:
            price = level['price']
            capital = level.get('capital', self.capital_per_level)
            if price <= 0:
                self.logger.error(f"Invalid price value: {price}. Skipping grid level.")
                continue
            
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
        
        available_base = base_balance - self.pending_locks.get(base_asset, 0)
        available_quote = quote_balance - self.pending_locks.get(quote_asset, 0)

        # Initialize warning collection and fund status flag
        warnings = []
        insufficient_funds = False

        # Use relative tolerance (0.1%) instead of absolute to handle varying order sizes
        # This prevents false "insufficient funds" when available ≈ required due to float precision
        quote_tolerance = max(usdt_needed * 0.01, 0.01)  # 1% or min 0.01 USDT
        base_tolerance = max(base_needed * 0.01, 0.001)  # 1% or min 0.001 base asset
        
        # Check if we have enough quote asset (USDT)
        # Use subtraction to avoid accumulation of floating-point errors
        if available_quote < (usdt_needed - quote_tolerance) and not simulation:
            warnings.append(f"Insufficient {quote_asset}: Required {usdt_needed:.2f}, Available {available_quote:.2f}")
            insufficient_funds = True

        # Also check if we have enough base asset (e.g., BTC, ETH)
        self.logger.info(f"Balance check: base_needed={base_needed:.8f}, available_base={available_base:.8f}, base_tolerance={base_tolerance:.8f}, threshold={(base_needed - base_tolerance):.8f}, check={available_base:.8f} < {(base_needed - base_tolerance):.8f} = {available_base < (base_needed - base_tolerance)}")
        if available_base < (base_needed - base_tolerance) and not simulation:
            warnings.append(f"Insufficient {base_asset}: Required {base_needed:.2f}, Available {available_base:.2f}")
            insufficient_funds = True

        # CRITICAL: Reject start if insufficient funds (don't cancel existing orders)
        if insufficient_funds and not simulation:
            error_message = f"❌ Cannot start grid trading: {' '.join(warnings)}\n\nPlease add sufficient funds or adjust grid parameters."
            self.logger.error(error_message)
            if self.telegram_bot:
                self.telegram_bot.send_message(error_message)
            return error_message  # Exit immediately without cancelling orders or setting is_running
        
        # Only cancel orders after fund validation passes
        self._cancel_all_open_orders()
        self.is_running = True  # Mark running only after all validations pass
        
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
        Start grid trading with optimal asset balance for symmetric grid trading.
        Cancels all existing orders first to ensure accurate balance assessment.
        
        Args:
            simulation: Whether to run in simulation mode (no real orders)
            
        Returns:
            str: Status message
        """
        if self.is_running:
            return "System already running"
        
        # CRITICAL FIX: First cancel all existing orders to release locked assets
        if not simulation:
            self.logger.info("Canceling all open orders before balance assessment")
            self._cancel_all_open_orders()
            
            # Wait briefly for balances to update in Binance's system
            time.sleep(2)
        
        # Get current market price
        current_price = self.binance_client.get_symbol_price(self.symbol)
        if not current_price or current_price <= 0:
            error_msg = f"Failed to get valid market price for {self.symbol}"
            self.logger.error(error_msg)
            return f"Error: {error_msg}"
        
        # Check asset balances AFTER canceling orders to get true available funds
        base_asset = self.symbol.replace('USDT', '')
        base_balance = self.binance_client.check_balance(base_asset)
        usdt_balance = self.binance_client.check_balance('USDT')
        
        # Calculate current portfolio value and distribution
        base_value_in_usdt = base_balance * current_price
        total_value_in_usdt = base_value_in_usdt + usdt_balance
        
        # Target ratio: 50% base asset, 50% USDT - safer for dynamic grid requirements
        target_base_ratio = 0.5
        current_base_ratio = base_value_in_usdt / total_value_in_usdt if total_value_in_usdt > 0 else 0
        
        # Log the accurate asset balance after canceling orders
        self.logger.info(f"Portfolio assessment after canceling orders: {base_balance} {base_asset} " 
                       f"({current_base_ratio*100:.1f}% of portfolio, target: {target_base_ratio*100:.1f}%)")
        
        # Determine if and how much to purchase - only if we're below target and not in simulation
        initial_purchase_made = False
        
        if not simulation and current_base_ratio < target_base_ratio:
            # Calculate required USDT to reach target ratio
            target_base_value = total_value_in_usdt * target_base_ratio
            needed_base_value = target_base_value - base_value_in_usdt
            
            # Apply safety constraints to purchase amount
            # CRITICAL FIX: Add 20% buffer to prevent precision rounding errors from dropping below min notional
            min_purchase = float(getattr(config, 'MIN_NOTIONAL_VALUE', 6)) * 1.2
            
            if needed_base_value > (total_value_in_usdt * 0.05) and usdt_balance >= min_purchase:
                # Calculate purchase amount with safety cap
                safety_cap = 0.8
                max_purchase = usdt_balance * safety_cap
                purchase_usdt = min(needed_base_value, max_purchase)
                purchase_usdt = max(purchase_usdt, min_purchase)  # Ensure minimum purchase size
                
                # Calculate quantity to buy
                quantity_to_buy = purchase_usdt / current_price
                
                # Format quantity according to exchange requirements
                formatted_quantity = self._adjust_quantity_precision(quantity_to_buy)
                
                # Notify about the planned purchase
                message = (f"Balancing portfolio for grid trading: "
                          f"Current base ratio: {current_base_ratio*100:.1f}%, "
                          f"Target: {target_base_ratio*100:.1f}%. "
                          f"Converting {purchase_usdt:.4f} USDT to ~{formatted_quantity} {base_asset}")
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
                    
                    # Calculate new ratio after purchase
                    new_base_balance = self.binance_client.check_balance(base_asset)
                    new_usdt_balance = self.binance_client.check_balance('USDT')
                    new_ratio = (new_base_balance * current_price) / ((new_base_balance * current_price) + new_usdt_balance)
                    
                    # Log the successful purchase
                    self.logger.info(f"Initial balance purchase complete. " 
                                   f"New {base_asset} balance: {new_base_balance}, "
                                   f"New base ratio: {new_ratio*100:.1f}%")
                    
                    # Allow time for balances to update
                    time.sleep(2)
                    
                except Exception as e:
                    self.logger.error(f"Failed to execute initial balance purchase: {e}")
                    if self.telegram_bot:
                        self.telegram_bot.send_message(f"⚠️ Failed to execute initial balance purchase: {e}")
            else:
                self.logger.info(f"No initial balance adjustment needed. "
                              f"Current base asset ratio: {current_base_ratio*100:.1f}%, "
                              f"Target: {target_base_ratio*100:.1f}%")
        
        # Start the normal grid trading process
        start_result = self.start(simulation)
        
        if initial_purchase_made:
            return f"Initial balance adjusted for optimal grid trading. {start_result}"
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
            quantity (float|str): Original quantity value
            
        Returns:
            str: Formatted quantity string
        """
        try:
            numeric_quantity = float(quantity)
        except (TypeError, ValueError):
            self.logger.error(f"Invalid quantity value: {quantity}")
            raise ValueError(f"Invalid quantity value: {quantity}")
        
        if numeric_quantity <= 0:
            self.logger.error(f"Quantity must be positive: {quantity}")
            raise ValueError(f"Quantity must be positive: {quantity}")
        
        return format_quantity(numeric_quantity, self.quantity_precision)
    
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
        
        # Update ATR value and trend strength using optimized mixed timeframes
        atr_value, trend_strength = self.calculate_market_metrics()
        
        # Update stored values for future reference
        if atr_value is not None:
            self.last_atr_value = atr_value
        elif self.last_atr_value is None:
            # Set fallback ATR value only if we don't already have one
            self.logger.warning("ATR calculation failed. Using fallback value of 0.01 for grid setup.")
            self.last_atr_value = 0.01  # Fallback value to ensure grid setup proceeds
        
        # Store current trend strength for change monitoring
        self.trend_strength = trend_strength
        self.last_trend_strength = trend_strength
        
        # Check WebSocket API availability
        client_status = self.binance_client.get_client_status()
        self.using_websocket = client_status["websocket_available"]
        
        # Place grid orders using the unified method
        self._initialize_grid_orders()

    def _calculate_max_buy_levels(self, usdt_balance, current_price):
        """
        Calculate maximum BUY levels supported by available USDT balance
        CRITICAL: Accounts for 1.5x capital amplification in core zone
        
        Args:
            usdt_balance: Available USDT balance
            current_price: Current market price
            
        Returns:
            int: Maximum number of BUY levels we can afford
        """
        effective_usdt = max(usdt_balance - self.pending_locks.get('USDT', 0), 0)
        
        # CRITICAL FIX: Account for maximum 1.5x amplification in core zone
        # Use conservative estimate with safety margin
        # Adjusted to 1.2x to match actual config (1.16x) and allow more levels
        max_capital_per_level = self.capital_per_level * 1.2  # 1.16x + safety margin
        
        if max_capital_per_level <= 0:
            return 0
            
        max_buy = int(effective_usdt / max_capital_per_level)
        self.logger.debug(
            f"Available USDT: {effective_usdt:.2f}, can support {max_buy} BUY levels "
            f"(accounting for 1.6x max capital amplification)"
        )
        return max_buy
    
    def _calculate_max_sell_levels(self, base_balance, current_price):
        """
        Calculate maximum SELL levels supported by available base asset balance
        CRITICAL: Accounts for 1.5x capital amplification in core zone
        
        Args:
            base_balance: Available base asset balance
            current_price: Current market price
            
        Returns:
            int: Maximum number of SELL levels we can afford
        """
        base_asset = self.symbol.replace('USDT', '')
        effective_base = max(base_balance - self.pending_locks.get(base_asset, 0), 0)
        # CRITICAL FIX: Account for maximum 1.5x amplification in core zone
        # Adjusted to 1.2x to match actual config (1.16x) and allow more levels
        max_capital_per_level = self.capital_per_level * 1.2  # 1.16x + safety margin
        sell_quantity_per_level = max_capital_per_level / current_price if current_price > 0 else 0
        
        if sell_quantity_per_level <= 0:
            return 0
            
        max_sell = int(effective_base / sell_quantity_per_level)
        self.logger.debug(
            f"Available {base_asset}: {effective_base:.6f}, can support {max_sell} SELL levels "
            f"(accounting for 1.6x max capital amplification)"
        )
        return max_sell

    def _determine_available_grid_levels(self, total_available_usdt):
        """Calculate the number of grid levels supported by available capital."""
        configured_levels = self.config_grid_levels
        min_levels = 3

        if total_available_usdt is None:
            self.logger.warning("Available balance is unknown; defaulting to configured grid levels")
            return configured_levels

        try:
            effective_funds = max(float(total_available_usdt), 0.0)
        except (TypeError, ValueError):
            self.logger.warning("Invalid balance value received; defaulting to configured grid levels")
            return configured_levels

        level_cost = self.capital_per_level * 0.8
        if level_cost <= 0:
            return configured_levels

        affordable_levels = int(effective_funds / level_cost)
        level_cap = configured_levels
        if self.level_reduction_factor < 1.0:
            level_cap = max(min_levels, int(configured_levels * self.level_reduction_factor))
        available_levels = max(min_levels, min(level_cap, affordable_levels))

        if affordable_levels < min_levels and configured_levels >= min_levels:
            self.logger.warning(
                "Available funds support fewer than minimum grid levels; using safety minimum of %s",
                min_levels,
            )

        return available_levels

    def _calculate_grid_levels(self):
        """
        Calculate asymmetric grid price levels with funds concentrated near the optimal center.
        Uses mean reversion principles to find the ideal grid center for oscillating markets.
        
        The grid is structured with:
        - A core zone with higher density near the calculated optimal price center
        - Edge zones with fewer levels at the extremes
        - Dynamic capital allocation based on distance from center price
        - Trend-based positioning to adapt to market direction
        
        Returns:
            list: List of grid levels with prices, sides and capital allocation
        """
        # --- Step 1: Calculate basic grid parameters ---
        try:
            # CRITICAL FIX: Get balances for both assets
            base_asset = self.symbol.replace('USDT', '')
            usdt_balance = self.binance_client.check_balance('USDT')
            base_balance = self.binance_client.check_balance(base_asset)
            
            # Get current price for calculations
            live_current_price = self.binance_client.get_symbol_price(self.symbol)
            
            # Calculate maximum levels supported by each asset
            max_buy_levels = self._calculate_max_buy_levels(usdt_balance, live_current_price)
            max_sell_levels = self._calculate_max_sell_levels(base_balance, live_current_price)
            
            # Use the configured grid levels as target, but respect asset limits
            configured_levels = self.config_grid_levels
            
            # Apply level reduction factor if in trending market
            level_cap = configured_levels
            if self.level_reduction_factor < 1.0:
                level_cap = max(3, int(configured_levels * self.level_reduction_factor))
            
            # Determine actual grid levels we can support
            # CRITICAL FIX: Validate dual-sided grid capability
            self.max_affordable_buy_levels = max_buy_levels
            self.max_affordable_sell_levels = max_sell_levels
            
            # CRITICAL: Reject if cannot support both BUY and SELL
            if max_buy_levels == 0:
                self.logger.error(
                    f"Cannot start grid: insufficient USDT for any BUY orders. "
                    f"Available USDT: {usdt_balance:.2f}, Required per level: {self.capital_per_level * 1.6:.2f}"
                )
                return []  # Must have BUY capability for hedging
            
            if max_sell_levels == 0:
                self.logger.error(
                    f"Cannot start grid: insufficient {base_asset} for any SELL orders. "
                    f"Available {base_asset}: {base_balance:.6f}"
                )
                return []  # Must have SELL capability for hedging
            
            total_supported_levels = max_buy_levels + max_sell_levels
            available_grid_levels = min(level_cap, total_supported_levels)
            
            # Ensure minimum viable grid (at least 1 BUY + 1 SELL)
            min_viable_levels = 2
            if available_grid_levels < min_viable_levels:
                self.logger.warning(
                    f"Insufficient assets for grid trading: max_buy={max_buy_levels}, max_sell={max_sell_levels}. "
                    f"Minimum {min_viable_levels} levels required."
                )
                return []
            
            previous_grid_levels = getattr(self, "current_grid_levels", self.config_grid_levels)
            
            # Calculate total equivalent funds for logging (USDT + base in USDT value)
            total_funds = usdt_balance + (base_balance * live_current_price)

            if available_grid_levels < self.config_grid_levels:
                self.logger.info(
                    "Adjusted grid levels from %s to %s based on available funds: %.2f USDT equivalent",
                    self.config_grid_levels,
                    available_grid_levels,
                    total_funds,
                )
            elif previous_grid_levels < self.config_grid_levels and available_grid_levels == self.config_grid_levels:
                self.logger.info(
                    "Available funds recovered; restoring configured grid level target of %s",
                    self.config_grid_levels,
                )

            grid_level_limit = available_grid_levels

            self.logger.debug(
                "Using %s grid levels (configured target %s) with %.2f USDT equivalent (USDT: %.2f, Base: %.6f)",
                grid_level_limit,
                self.config_grid_levels,
                total_funds,
                usdt_balance,
                base_balance,
            )

           # Use mean reversion optimized center instead of current market price FOR GRID POSITIONING
            grid_center, grid_range_percent = self.calculate_optimal_grid_center()

            # ==================== Dynamic Boundary Snap (Snap to Edge) ====================
            
            # 1. Fetch live price for strict boundary validation
            # We must ensure the grid covers the REAL market price to avoid order rejection.
            try:
                live_current_price = float(self.binance_client.get_symbol_price(self.symbol))
            except Exception as e:
                self.logger.error(f"Failed to get live price for grid validation: {e}")
                # Fallback: use grid_center logic if live price fails
                live_current_price = grid_center

            # 2. Pre-calculate Trend Factor (Unified)
            # This determines the asymmetry of the grid (skewness).
            # Calculated ONCE here to ensure consistency between validation and generation.
            atr_value, trend_strength = self.calculate_market_metrics()
            if atr_value is not None:
                self.last_atr_value = atr_value
            
            # Trend Factor logic: 
            # Trend = -1.0 (Crash) -> factor = 0.2 (Small upper space, defensive)
            # Trend = 1.0 (Pump) -> factor = 0.8 (Large upper space, aggressive)
            trend_factor = 0.5 + (trend_strength * 0.3)
            
            # 3. Define Safety Buffer
            # Exchanges often reject Sell orders if Price <= BestBid * (1+Fee).
            # We add a 1.0% buffer to ensure the generated boundary orders are valid limit orders.
            # Increased to 1.0% to safely cover spread, fees, and volatility slippage.
            safety_buffer = 0.01
            min_valid_upper = live_current_price * (1 + safety_buffer)
            max_valid_lower = live_current_price * (1 - safety_buffer)
            
            # 4. Calculate Theoretical Boundaries based on Historical Center
            raw_range_val = grid_center * grid_range_percent
            theoretical_upper = grid_center + (raw_range_val * trend_factor)
            theoretical_lower = grid_center - (raw_range_val * (1 - trend_factor))
            
            is_snapped = False
            original_center = grid_center

            # 5. Reverse Calculation Logic (Snap to Edge)
            
            # CASE A: Price Pump / Crash Trend (Current ZEC Scenario)
            # The theoretical upper bound is BELOW the current price. We cannot place Sell orders.
            # Fix: Calculate the minimum Grid Center needed so that Upper Bound == min_valid_upper.
            if theoretical_upper < min_valid_upper:
                # Derivation: 
                # Upper = Center * (1 + RangePct * TrendFactor)
                # -> Center = Upper / (1 + RangePct * TrendFactor)
                new_center = min_valid_upper / (1 + grid_range_percent * trend_factor)
                
                self.logger.warning(
                    f"GRID SUBMERGED (Crash Protection): "
                    f"Theoretical Upper {theoretical_upper:.2f} < Min Valid {min_valid_upper:.2f}. "
                    f"Trend: {trend_strength:.2f}. "
                    f"Snapping Center UP: {original_center:.2f} -> {new_center:.2f} (Creating Safety Gap)"
                )
                grid_center = new_center
                is_snapped = True

            # CASE B: Price Crash / Pump Trend
            # The theoretical lower bound is ABOVE the current price. We cannot place Buy orders.
            # Fix: Calculate the maximum Grid Center needed so that Lower Bound == max_valid_lower.
            elif theoretical_lower > max_valid_lower:
                # Derivation:
                # Lower = Center * (1 - RangePct * (1 - TrendFactor))
                # -> Center = Lower / (1 - RangePct * (1 - TrendFactor))
                new_center = max_valid_lower / (1 - grid_range_percent * (1 - trend_factor))
                
                self.logger.warning(
                    f"GRID FLOATING (Pump Protection): "
                    f"Theoretical Lower {theoretical_lower:.2f} > Min Valid {max_valid_lower:.2f}. "
                    f"Trend: {trend_strength:.2f}. "
                    f"Snapping Center DOWN: {original_center:.2f} -> {new_center:.2f} (Creating Safety Gap)"
                )
                grid_center = new_center
                is_snapped = True

            if is_snapped:
                self.logger.info(f"Grid Center Optimization: {original_center:.2f} -> {grid_center:.2f}")

            # ==================== End of Strategy ====================

            # Update instance variable to persist the corrected center
            self.grid_center = grid_center 
            
            # Update local variable 'current_price' which is used for grid generation below.
            # Note: We do NOT update self.current_market_price as that should reflect the actual ticker.
            current_price = grid_center

            # Check validity of calculated center
            if current_price <= 0:
                self.logger.error(f"Invalid grid center price: {current_price}")
                return []
            
            # Calculate grid range based on the (potentially corrected) center
            grid_range = current_price * grid_range_percent
            
            # Calculate final grid boundaries
            # We reuse 'trend_factor' from above to ensure the Snap logic matches the Generation logic exactly.
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
            if total_needed > grid_level_limit:
                # Scale down proportionally while preserving minimums
                ratio = grid_level_limit / total_needed
                core_levels = max(int(core_levels_needed * ratio), 2)
                edge_levels = max(grid_level_limit - core_levels, 1)
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
            
            # CRITICAL FIX: Enforce BUY/SELL limits based on available assets
            buy_count = sum(1 for level in grid_levels if level['side'] == 'BUY')
            sell_count = sum(1 for level in grid_levels if level['side'] == 'SELL')
            
            # Check if we exceed limits
            if buy_count > self.max_affordable_buy_levels or sell_count > self.max_affordable_sell_levels:
                self.logger.warning(
                    f"Grid exceeds asset limits: BUY {buy_count}/{self.max_affordable_buy_levels}, "
                    f"SELL {sell_count}/{self.max_affordable_sell_levels}. Trimming excess orders."
                )
                
                # Trim excess BUY orders (remove furthest from center first)
                if buy_count > self.max_affordable_buy_levels:
                    buy_levels = [l for l in grid_levels if l['side'] == 'BUY']
                    # Sort by distance from grid center (furthest first)
                    buy_levels.sort(key=lambda l: abs(l['price'] - grid_center), reverse=True)
                    # Keep only the affordable amount
                    keep_buy = set(l['price'] for l in buy_levels[:self.max_affordable_buy_levels])
                    grid_levels = [l for l in grid_levels if l['side'] != 'BUY' or l['price'] in keep_buy]
                    self.logger.info(f"Trimmed BUY orders from {buy_count} to {self.max_affordable_buy_levels}")
                
                # Trim excess SELL orders (remove furthest from center first)
                if sell_count > self.max_affordable_sell_levels:
                    sell_levels = [l for l in grid_levels if l['side'] == 'SELL']
                    sell_levels.sort(key=lambda l: abs(l['price'] - grid_center), reverse=True)
                    keep_sell = set(l['price'] for l in sell_levels[:self.max_affordable_sell_levels])
                    grid_levels = [l for l in grid_levels if l['side'] != 'SELL' or l['price'] in keep_sell]
                    self.logger.info(f"Trimmed SELL orders from {sell_count} to {self.max_affordable_sell_levels}")
            
            # CRITICAL: Verify grid balance after trimming
            final_buy_count = sum(1 for level in grid_levels if level['side'] == 'BUY')
            final_sell_count = sum(1 for level in grid_levels if level['side'] == 'SELL')
            
            if final_buy_count == 0 or final_sell_count == 0:
                self.logger.error(
                    f"Grid imbalanced after trimming: BUY={final_buy_count}, SELL={final_sell_count}. "
                    f"Cannot operate with one-sided grid. Rejecting grid generation."
                )
                return []  # Reject unbalanced grid

            actual_grid_levels = len(grid_levels)
            if actual_grid_levels != grid_level_limit:
                self.logger.debug(
                    "Generated %s grid levels after balancing and enforcement (target limit %s, BUY=%s, SELL=%s)",
                    actual_grid_levels,
                    grid_level_limit,
                    final_buy_count,
                    final_sell_count,
                )

            self.current_grid_levels = actual_grid_levels
            # Snapshot the generated grid for troubleshooting (helps explain why orders may not be placed)
            try:
                lowest = min(l["price"] for l in grid_levels)
                highest = max(l["price"] for l in grid_levels)
                self.logger.info(
                    "Grid ready: total=%s (BUY=%s, SELL=%s), center=%.8f, range=%.4f%%, price band=%.8f-%.8f",
                    actual_grid_levels,
                    final_buy_count,
                    final_sell_count,
                    grid_center,
                    grid_range_percent * 100,
                    lowest,
                    highest,
                )
            except Exception:
                # Do not block on logging issues
                pass
            return grid_levels

        except Exception as e:
            self.logger.error(f"Error calculating grid levels: {e}", exc_info=True)
            return []
            
    def _calculate_single_timeframe_trend(self, klines, lookback=20):
        """
        Helper method to calculate trend strength for a specific set of klines.
        Returns a float between -1.0 and 1.0.
        """
        try:
            if not klines or len(klines) < lookback + 1:
                return 0.0

            # Extract close prices
            closes = []
            for k in klines[-lookback-1:]:
                if isinstance(k, list):
                    closes.append(float(k[4]))
                elif isinstance(k, dict) and 'close' in k:
                    closes.append(float(k['close']))
            
            if len(closes) < lookback + 1:
                return 0.0

            # Calculate price change momentum
            changes = []
            for i in range(1, len(closes)):
                change_pct = (closes[i] - closes[i-1]) / closes[i-1]
                changes.append(change_pct)

            # Short trend (last 5 periods)
            short_trend = sum(changes[-5:]) if len(changes) >= 5 else 0

            # Overall trend (time-weighted)
            weights = [0.5 + (i/lookback/2) for i in range(lookback)]
            if len(changes) < len(weights):
                weights = weights[-len(changes):]
            
            weighted_changes = [changes[i] * weights[i] for i in range(len(changes))]
            overall_trend = sum(weighted_changes)

            # Combine short and overall
            combined_trend = (short_trend * 0.7) + (overall_trend * 0.3)

            # Normalize
            return max(min(combined_trend * 50, 1.0), -1.0)

        except Exception as e:
            self.logger.warning(f"Error in single timeframe trend calculation: {e}")
            return 0.0

    def calculate_trend_strength(self, current_price=None, klines=None, lookback=20):
        """
        Legacy wrapper for trend calculation, defaults to 1h timeframe.
        Maintains backward compatibility and sets self.trend_strength.
        """
        try:
            if klines is None:
                klines = self.binance_client.get_historical_klines(
                    symbol=self.symbol,
                    interval="1h",
                    limit=self.atr_period + lookback
                )
            
            trend_strength = self._calculate_single_timeframe_trend(klines, lookback)
            
            # Log results
            self.logger.info(
                f"Detected trend strength (single-frame): {trend_strength:.2f}, "
                f"effective adjustment: {(trend_strength*0.3*100):.1f}% of grid range"
            )
            
            self.trend_strength = trend_strength
            return trend_strength
            
        except Exception as e:
            self.logger.error(f"Error calculating trend strength: {e}")
            return 0

    def calculate_market_metrics(self):
        """
        Calculate market metrics using multi-timeframe weighted analysis:
        - Volatility (ATR): 15m candles
        - Trend: Weighted average of 15m (20%), 1h (50%), 4h (30%)
        
        Returns:
            tuple: (atr_value, trend_strength)
        """
        try:
            # 1. Fetch Data
            # 15m for ATR and short-term trend
            klines_15m = self.binance_client.get_historical_klines(
                symbol=self.symbol, 
                interval="15m", 
                limit=max(self.atr_period + 10, 24) # Need enough for both
            )
            
            # 1h for mid-term trend
            klines_1h = self.binance_client.get_historical_klines(
                symbol=self.symbol, 
                interval="1h", 
                limit=24
            )
            
            # 4h for long-term trend
            klines_4h = self.binance_client.get_historical_klines(
                symbol=self.symbol, 
                interval="4h", 
                limit=24
            )

            # 2. Calculate ATR (using 15m)
            atr = calculate_atr(klines_15m, period=self.atr_period) if klines_15m else None
            
            # 3. Calculate Multi-Timeframe Trend
            t_15m = self._calculate_single_timeframe_trend(klines_15m, lookback=15)
            t_1h = self._calculate_single_timeframe_trend(klines_1h, lookback=15)
            t_4h = self._calculate_single_timeframe_trend(klines_4h, lookback=15)
            
            # Weighted Average: 15m(20%) + 1h(50%) + 4h(30%)
            final_trend = (t_15m * 0.2) + (t_1h * 0.5) + (t_4h * 0.3)
            
            # Store for use in grid creation
            self.trend_strength = final_trend

            atr_display = f"{atr:.8f}" if isinstance(atr, (int, float, np.floating)) else "N/A"
            self.logger.info(
                f"Multi-Timeframe Analysis - ATR: {atr_display} | "
                f"Trend: {final_trend:.2f} (15m:{t_15m:.2f}, 1h:{t_1h:.2f}, 4h:{t_4h:.2f})"
            )
            
            return atr, final_trend
            
        except Exception as e:
            self.logger.error(f"Error in multi-timeframe metrics: {e}")
            # Fallback
            atr = self._get_current_atr()
            trend = self.calculate_trend_strength()
            return atr, trend
    
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
                    # Handle specific Binance error codes from binance-connector
                    # ClientError from binance.error has error_code attribute
                    code_match = False
                    
                    if isinstance(e, ClientError):
                        # Standard binance-connector exception
                        code_match = (e.error_code == -2011 or "unknown order" in str(e).lower())
                        self.logger.debug(f"ClientError caught: code={e.error_code}, message={e}")
                    elif hasattr(e, "error_code"):
                        # Other exceptions with error_code attribute
                        code_match = (getattr(e, "error_code", None) == -2011 or "unknown order" in str(e).lower())
                    else:
                        # Unknown exception type
                        self.logger.warning(f"Unexpected exception type during order cancellation: {type(e).__name__}")
                        code_match = False

                    if code_match:
                        self.logger.warning(
                            f"Order {order.get('orderId')} cancellation rejected (unknown/already filled). Skipping."
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

        # Pause new orders in strong trend protective mode
        if self._should_pause_orders():
            self.logger.info("Protective pause is active; skipping new grid order placement")
            return False
        
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
        
        # CRITICAL: Orderbook-based price protection to prevent instant market execution
        try:
            # Prefer WS bookTicker (per web-socket-api.md), fallback to REST with cache
            best_bid, best_ask = self.binance_client.get_best_bid_ask(self.symbol)
            
            order_price = float(formatted_price)
            fee_rate = config.TRADING_FEE_RATE
            
            if side == 'SELL':
                # Sell orders must be above best bid to avoid instant execution
                # Add fee buffer (2x fees + 0.2% safety margin) to ensure profitability
                min_safe_price = best_bid * (1 + fee_rate * 2 + 0.002)
                if order_price < min_safe_price:
                    self.logger.warning(
                        f"Rejecting SELL @ {order_price:.8f}: below safe price {min_safe_price:.8f} "
                        f"(best_bid={best_bid:.8f}, would execute immediately)"
                    )
                    return False
            
            elif side == 'BUY':
                # Buy orders must be below best ask to avoid instant execution
                max_safe_price = best_ask * (1 - fee_rate * 2 - 0.002)
                if order_price > max_safe_price:
                    self.logger.warning(
                        f"Rejecting BUY @ {order_price:.8f}: above safe price {max_safe_price:.8f} "
                        f"(best_ask={best_ask:.8f}, would execute immediately)"
                    )
                    return False
                    
        except Exception as e:
            # Without orderbook data we cannot guarantee the order will not cross the spread; block placement
            self.logger.error(f"Cannot validate order price (orderbook fetch failed): {e}")
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
        Check if grid needs recalculation based on time, volatility or market state changes.
        Implements smart confirmation: price must stay out of bounds for 2 hours before recalculation.
        
        Returns:
            bool: True if grid was recalculated or adjusted, False otherwise
        """
        # Skip if not running
        if not self.is_running:
            return False
            
        now = datetime.now()
        current_time = time.time()
        
        # SMART RECALCULATION: Check if price is outside grid bounds
        out_of_bounds_recalc = False
        if self.grid and len(self.grid) > 0:
            try:
                current_price = self.binance_client.get_symbol_price(self.symbol)
                grid_prices = [level['price'] for level in self.grid]
                upper_limit = max(grid_prices)
                lower_limit = min(grid_prices)
                
                # Check if price is currently out of bounds
                currently_out_of_upper = current_price > upper_limit
                currently_out_of_lower = current_price < lower_limit
                currently_out_of_bounds = currently_out_of_upper or currently_out_of_lower
                
                if currently_out_of_bounds:
                    # Price is out of bounds - start or continue timer
                    if self.out_of_bounds_start_time is None:
                        # First detection - start timer
                        self.out_of_bounds_start_time = current_time
                        self.price_out_of_upper_bound = currently_out_of_upper
                        self.price_out_of_lower_bound = currently_out_of_lower
                        direction = "above upper limit" if currently_out_of_upper else "below lower limit"
                        self.logger.info(
                            f"Price {direction} detected (${current_price:.6f} vs grid range ${lower_limit:.6f}-${upper_limit:.6f}). "
                            f"Starting 2-hour confirmation timer."
                        )
                    else:
                        # Timer already running - check if confirmation period elapsed
                        time_out_of_bounds = current_time - self.out_of_bounds_start_time
                        if time_out_of_bounds >= self.out_of_bounds_confirmation_threshold:
                            # Confirmed sustained breakout - trigger recalculation
                            direction = "above" if currently_out_of_upper else "below"
                            hours_out = time_out_of_bounds / 3600
                            self.logger.info(
                                f"Price has been {direction} grid range for {hours_out:.1f} hours. "
                                f"Confirmed breakout - triggering grid recalculation."
                            )
                            out_of_bounds_recalc = True
                            # Reset timer after triggering recalc
                            self.out_of_bounds_start_time = None
                            self.price_out_of_upper_bound = False
                            self.price_out_of_lower_bound = False
                else:
                    # Price returned to normal range - reset timer
                    if self.out_of_bounds_start_time is not None:
                        time_was_out = current_time - self.out_of_bounds_start_time
                        self.logger.info(
                            f"Price returned to grid range after {time_was_out/60:.1f} minutes. "
                            f"Canceling breakout confirmation (false breakout detected)."
                        )
                        self.out_of_bounds_start_time = None
                        self.price_out_of_upper_bound = False
                        self.price_out_of_lower_bound = False
                        
            except Exception as e:
                self.logger.error(f"Error checking price bounds: {e}")
        
        # NEW: Detect market state and apply appropriate adjustments
        current_state = self.detect_market_state()
        
        # Trigger recalculation for extreme market states if not already adjusted
        volatility_based_recalc = False
        if current_state in [MarketState.PUMP, MarketState.CRASH] and not hasattr(self, '_state_adjusted'):
            self.logger.info(f"Extreme market state ({current_state.name}) triggered grid recalculation")
            volatility_based_recalc = True
            self._state_adjusted = True  # Mark as adjusted for this state change
        elif current_state == MarketState.RANGING and hasattr(self, '_state_adjusted'):
            # Clear adjustment flag when returning to ranging state
            delattr(self, '_state_adjusted')
        
        # Existing time-based recalculation check
        time_based_recalc = False
        if self.last_recalculation:
            days_passed = (now - self.last_recalculation).days
            if days_passed >= self.recalculation_period:
                self.logger.info(f"Time-based grid recalculation triggered: {days_passed} days since last recalculation")
                time_based_recalc = True
        
        # Check volatility-based recalculation - do not reset volatility_based_recalc!
        partial_adjustment = False
        current_atr, current_trend = self.calculate_market_metrics()

        if current_atr and self.last_atr_value:
            atr_change = abs(current_atr - self.last_atr_value) / self.last_atr_value
            
            # Only check if recalculation not yet triggered
            if not volatility_based_recalc and atr_change > 0.2:
                self.logger.info(f"Major volatility change detected: ATR changed by {atr_change*100:.2f}%, performing full grid recalculation")
                volatility_based_recalc = True
            elif atr_change > 0.1:
                self.logger.info(f"Moderate volatility change detected: ATR changed by {atr_change*100:.2f}%, performing partial grid adjustment")
                partial_adjustment = True
        
        # Additional trend change check
        trend_change = abs(current_trend - self.last_trend_strength) if hasattr(self, 'last_trend_strength') else 0
        if trend_change > 0.5:  # Check if trend changed significantly (50% of the -1 to 1 range)
            self.logger.info(f"Significant trend change detected: {trend_change:.2f}, considering grid adjustment")
        
        # NEW: Apply market state-based adjustments to grid parameters
        self._adjust_grid_based_on_market_state(current_state)
        
        # Handle full grid recalculation (includes out-of-bounds confirmation, time-based, and volatility triggers)
        if time_based_recalc or volatility_based_recalc or out_of_bounds_recalc:
            try:
                # Cancel all orders
                self._cancel_all_open_orders()
                
                # Recalculate grid
                self._setup_grid()
                
                # Update last recalculation timestamp
                self.last_recalculation = now
                
                # Build message with reason and state info
                message = "Grid trading levels recalculated due to "
                if time_based_recalc:
                    message += "scheduled recalculation"
                else:
                    message += f"significant market change (state: {current_state.name})"
                
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
        result = False
        risk_manager = None
        if side == "SELL" and self.telegram_bot and hasattr(self.telegram_bot, 'risk_manager'):
            potential_manager = self.telegram_bot.risk_manager
            if potential_manager and getattr(potential_manager, 'is_active', False):
                risk_manager = potential_manager

        try:
            while True:
                # Create opposite order
                new_side = "SELL" if side == "BUY" else "BUY"

                # Adjust quantity precision
                formatted_quantity = self._adjust_quantity_precision(quantity)

                # Set price with increased spread for new order using config parameter
                buy_sell_spread = config.BUY_SELL_SPREAD  # Already stored as decimal fraction
                if new_side == "SELL":
                    # For SELL orders after BUY executed
                    new_price = price * (1 + buy_sell_spread)
                else:
                    # For BUY orders after SELL executed
                    new_price = price * (1 - buy_sell_spread)

                # Estimate slippage and achievable spread when protection is enabled
                min_profit_buffer = getattr(config, "MIN_EXPECTED_PROFIT_BUFFER", 0)
                round_trip_fee = config.TRADING_FEE_RATE * 2
                achieved_spread = None
                slippage_estimate = 0.0
                if getattr(self, "protection_enabled", False):
                    est = self._estimate_slippage_and_spread(new_side, float(formatted_quantity))
                    if est:
                        slippage_estimate, book_spread, mid_price = est
                        achieved_spread = abs((new_price - price) / price) - (slippage_estimate * 2)
                        required = round_trip_fee + min_profit_buffer + (slippage_estimate * 2)
                        if achieved_spread < required:
                            # Inflate spread to satisfy required profit after fees and slippage
                            adjust_ratio = required
                            if new_side == "SELL":
                                new_price = price * (1 + adjust_ratio)
                            else:
                                new_price = price * (1 - adjust_ratio)
                            self.logger.info(
                                "Adjusted price for %s to cover fees/slippage: %.8f -> %.8f (req %.4f%%, bookSpread %.4f%%, slip %.4f%%)",
                                new_side,
                                price,
                                new_price,
                                required * 100,
                                (book_spread or 0) * 100 if book_spread is not None else 0,
                                slippage_estimate * 100,
                            )

                formatted_price = self._adjust_price_precision(new_price)

                # Calculate expected profit as a decimal (not percentage)
                expected_profit_decimal = abs(float(new_price) - float(price)) / float(price)
                effective_profit = expected_profit_decimal - round_trip_fee - (slippage_estimate * 2)
                min_required_profit = (round_trip_fee * config.PROFIT_MARGIN_MULTIPLIER) + min_profit_buffer

                # CRITICAL FIX: Always place reverse order to maintain hedge, even if profit is low
                # Skipping reverse orders breaks grid integrity and leaves positions unhedged
                if effective_profit <= min_required_profit:
                    # Convert to percentage only for logging purposes
                    expected_profit_pct = expected_profit_decimal * 100
                    min_required_profit_pct = min_required_profit * 100
                    
                    self.logger.warning(
                        f"Low profit margin detected: {expected_profit_pct:.4f}% vs required {min_required_profit_pct:.4f}%. "
                        f"Placing reverse order anyway to maintain grid hedge."
                    )
                    # Continue with order placement - DO NOT SKIP

                # Use config value for minimum order check
                min_order_value = config.MIN_NOTIONAL_VALUE
                order_value = float(formatted_quantity) * float(formatted_price)
                if order_value < min_order_value:
                    self.logger.info(f"Skipping small order - value too low: {order_value:.2f} USDT < {min_order_value} USDT")
                    # Place replacement grid order to maintain grid density
                    self._place_replacement_grid_order(level_index, float(price))
                    result = False
                    break

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
                    result = True
                    break

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
                    result = False
                    break

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
                    
                    # Release local lock once Binance has accepted the order
                    self._release_funds(asset, required)

                    message = f"Grid trade executed: {side} {formatted_quantity} @ {price}\nOpposite order placed: {new_side} {formatted_quantity} @ {formatted_price}"
                    self.logger.info(message)
                    if self.telegram_bot:
                        self.telegram_bot.send_message(message)
                    result = True
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
                            self._release_funds(asset, required)

                            message = f"Grid trade executed: {side} {formatted_quantity} @ {price}\nOpposite order placed via fallback: {new_side} {formatted_quantity} @ {formatted_price}"
                            self.logger.info(message)
                            if self.telegram_bot:
                                self.telegram_bot.send_message(message)
                            result = True
                        except Exception as retry_error:
                            self._release_funds(asset, required)  # Make sure to release funds on retry failure
                            self.logger.error(f"Fallback order placement also failed: {retry_error}")
                            result = False
                    else:
                        result = False
                break
        finally:
            if risk_manager:
                self.logger.info("SELL order filled, triggering OCO order update")
                risk_manager._check_for_missing_oco_orders()

        return result

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
        # Use real-time price for capital allocation decisions, not stale grid_center
        live_price = self.binance_client.get_symbol_price(self.symbol)
        current_price = live_price  # Use live price instead of self.current_market_price
        
        # Get grid center for zone calculations (stored during grid setup)
        grid_center = getattr(self, 'grid_center', current_price)
        grid_range = grid_center * self.grid_range_percent
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
        # CRITICAL FIX: Add 20% buffer to prevent precision rounding errors from dropping below min notional
        min_required_capital = config.MIN_NOTIONAL_VALUE * 1.2
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
            locked_amount = self.pending_locks.get(asset, 0.0)
            effective_available = max(available - locked_amount, 0.0)
            
            # Check if we have enough available balance
            if effective_available < amount:
                self.logger.warning(
                    f"Insufficient {asset} balance for order: "
                    f"Required: {amount}, Available: {available}, "
                    f"Locked: {locked_amount}"
                )
                return False
            
            # Lock the funds temporarily until order is submitted
            self.pending_locks[asset] = locked_amount + amount
            self.logger.debug(
                f"Temporarily locked {amount} {asset}, effective free: {effective_available}, "
                f"pending locks: {self.pending_locks[asset]}"
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

    def _check_for_unfilled_grid_slots(self):
        """Ensure each grid level has an active order and place replacements when needed"""
        if not self.is_running or self.simulation_mode:
            return 0
        if self._should_pause_orders():
            self.logger.info("Protective pause active; skipping refill of missing grid slots")
            return 0

        replacements = 0

        for index, level in enumerate(self.grid):
            order_id = level.get('order_id')

            if order_id:
                continue

            if 'price' not in level or 'side' not in level:
                self.logger.warning(
                    f"Grid level {index} missing required data for replacement order: {level}"
                )
                continue

            try:
                if self._place_grid_order(level):
                    replacements += 1
                else:
                    self.logger.warning(
                        f"Failed to place replacement order for grid level {index} at price {level.get('price')}"
                    )
            except BinanceAPIException as api_error:
                self.logger.error(
                    f"Binance API error while replacing missing order at grid level {index}: {api_error}"
                )
            except Exception as error:
                self.logger.error(
                    f"Unexpected error while replacing missing order at grid level {index}: {error}"
                )

        return replacements

    def _check_for_stale_orders(self):
        """Cancel and reallocate orders that have been open too long without execution"""
        if not self.is_running or self.simulation_mode:
            return 0
            
        current_time = int(time.time())
        max_order_age_hours = max(1, getattr(config, "MAX_ORDER_AGE_HOURS", 4))
        max_order_age = max_order_age_hours * 3600
        base_deviation_threshold = max(0.0005, getattr(config, "PRICE_DEVIATION_THRESHOLD", 0.015))
        round_trip_fee = config.TRADING_FEE_RATE * 2
        min_profit_buffer = getattr(config, "MIN_EXPECTED_PROFIT_BUFFER", 0)
        
        # Track orders to cancel
        orders_to_cancel = []
        
        # Get current market price
        current_price = self.binance_client.get_symbol_price(self.symbol)
        if not current_price:
            return 0
        
        # First identify stale orders far from current price
        for i, level in enumerate(self.grid):
            if level.get('order_id') and level.get('timestamp'):
                # Check age
                order_age = current_time - level['timestamp']
                
                # Dynamic price deviation threshold based on order age
                # The longer an order exists, the smaller the price deviation needed to cancel
                time_factor = min(1, order_age / max_order_age)
                deviation_threshold = base_deviation_threshold * (1 + (1 - time_factor))
                
                # Calculate distance from current price
                price_distance = abs(level['price'] - current_price) / current_price

                # Skip cancellation if the order still has profit potential in protected mode
                if self.protection_enabled and self._has_profit_potential(level, current_price, round_trip_fee, min_profit_buffer):
                    self.logger.debug("Preserving stale order %s due to profit potential", level['order_id'])
                    continue
                
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
        # Re-evaluate protective mode based on symbol category
        major_list = set(getattr(config, "MAJOR_SYMBOLS", []))
        auto_non_major = getattr(config, "AUTO_PROTECTIVE_FOR_NON_MAJOR", True)
        self.protection_enabled = getattr(config, "ENABLE_PROTECTIVE_MODE", False) or (auto_non_major and self.symbol not in major_list)
        
        # Re-calculate trading parameters based on new symbol
        self.base_asset = new_symbol.replace('USDT', '')
        self.quote_asset = 'USDT'  # Assuming all pairs are USDT based
        
        # Update other relevant properties
        self.pending_orders = {}
        self.pending_locks = {}
        self.balance_lock = threading.RLock()
        
        # Cache is already initialized in __init__, no need to re-initialize here
        
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
        
        # STRICT PROFIT CHECK: Prevent forced hedging if it leads to fee erosion
        round_trip_fee = config.TRADING_FEE_RATE * 2
        min_profit_buffer = getattr(config, "MIN_EXPECTED_PROFIT_BUFFER", 0)
        
        if not self._has_profit_potential(new_level, current_price, round_trip_fee, min_profit_buffer):
            self.logger.warning(
                f"Skipping replacement order: Profit potential too low for {side} @ {price:.8f} "
                f"(Current: {current_price:.8f})"
            )
            return

        # Update the grid level and place the order
        self.grid[level_index] = new_level
        self._place_grid_order(new_level)
        
        self.logger.info(f"Placed replacement grid order: {side} at price {price:.8f} to maintain grid density")
    
    def _calculate_volume_change(self, lookback=12):
        """
        Calculate recent volume change ratio compared to baseline period
        
        Args:
            lookback: Number of periods to use as baseline reference
            
        Returns:
            float: Volume change ratio, >1 means volume is increasing
        """
        try:
            # Use 1-hour candles to reduce noise
            klines = self.binance_client.get_historical_klines(
                symbol=self.symbol,
                interval="1h",
                limit=lookback + 12  # Get extended history for comparison
            )
            
            if not klines or len(klines) < lookback + 5:
                self.logger.warning("Insufficient volume data for calculation")
                return 1.0  # Default to no change
            
            # Extract volume data (volume is at index 5)
            volumes = []
            for k in klines:
                if isinstance(k, list) and len(k) > 5:
                    volumes.append(float(k[5]))
                elif isinstance(k, dict) and 'volume' in k:
                    volumes.append(float(k['volume']))
            
            if len(volumes) < lookback + 5:
                return 1.0
            
            # Calculate recent average volume (last 5 hours) vs baseline
            recent_avg_volume = sum(volumes[-5:]) / 5
            baseline_avg_volume = sum(volumes[-(lookback+5):-5]) / lookback
            
            # Avoid division by zero
            if baseline_avg_volume == 0:
                return 1.0
                
            # Calculate change ratio
            volume_change = recent_avg_volume / baseline_avg_volume
            
            # Update internal tracking
            self.volume_history.append(volume_change)
            if len(self.volume_history) > self.history_max_size:
                self.volume_history.pop(0)  # Maintain history length
                
            return volume_change
            
        except Exception as e:
            self.logger.error(f"Volume change calculation error: {e}")
            return 1.0

    def detect_market_state(self):
        """
        Detect current market state by analyzing price movement, volatility and volume
        
        Uses existing metrics like ATR and trend_strength plus additional volume analysis
        to determine if the market is in a normal ranging state or exceptional states
        that require adjusted trading parameters.
        
        Returns:
            MarketState: Current market state enum value
        """
        try:
            # 1. Use existing methods to get ATR and trend strength
            atr_value, trend_strength = self.calculate_market_metrics()
            
            # 2. Calculate volume change (increasing volume can signal breakouts)
            volume_change = self._calculate_volume_change(lookback=12)
            
            # 3. Get current price and calculate price deviation
            current_price = self.binance_client.get_symbol_price(self.symbol)
            
            # Update price history
            self.price_history.append(current_price)
            if len(self.price_history) > self.history_max_size:
                self.price_history.pop(0)
            
            # Calculate price deviation if we have enough history
            price_deviation = 0
            if len(self.price_history) > 10:
                avg_price = sum(self.price_history[:-1]) / (len(self.price_history) - 1)
                price_deviation = abs(current_price - avg_price) / avg_price
            
            # 4. Check if we're in cooldown period
            current_time = time.time()
            if (current_time - self.last_state_change_time) < self.state_cooldown_period:
                # Stay in current state during cooldown to avoid flip-flopping
                return self.current_market_state
            
            # 5. Determine market state based on combined metrics
            
            # PUMP state: Strong uptrend + volume surge
            if trend_strength > 0.7 and volume_change > 1.8:
                new_state = MarketState.PUMP
                state_message = "PUMP detected - Strong uptrend with volume surge"
            
            # CRASH state: Strong downtrend + volume surge
            elif trend_strength < -0.7 and volume_change > 1.8:
                new_state = MarketState.CRASH
                state_message = "CRASH detected - Strong downtrend with volume surge"
            
            # BREAKOUT state: Moderate trend + price deviation or volume increase
            elif (abs(trend_strength) > 0.5 and (price_deviation > 0.02 or volume_change > 1.5)):
                new_state = MarketState.BREAKOUT
                state_message = "BREAKOUT detected - Clear directional movement forming"
            
            # Default RANGING state
            else:
                new_state = MarketState.RANGING
                state_message = "RANGING market - Sideways movement suitable for grid trading"
            
            # 6. Handle state transition if state has changed
            if new_state != self.current_market_state:
                self.last_state_change_time = current_time
                self.logger.info(f"Market state change: {self.current_market_state.name} → {new_state.name}. {state_message}")
                
                # Notify via Telegram if available
                if self.telegram_bot:
                    emoji_map = {
                        MarketState.RANGING: "↔️",
                        MarketState.BREAKOUT: "↗️",
                        MarketState.CRASH: "🔻",
                        MarketState.PUMP: "🚀"
                    }
                    emoji = emoji_map.get(new_state, "")
                    self.telegram_bot.send_message(
                        f"{emoji} Market state changed: {self.current_market_state.name} → {new_state.name}\n"
                        f"Trend strength: {trend_strength:.2f}, Volume change: {volume_change:.2f}x\n"
                        f"{state_message}"
                    )
                
                self.current_market_state = new_state
            
            return self.current_market_state
            
        except Exception as e:
            self.logger.error(f"Market state detection error: {e}")
            return MarketState.RANGING  # Default to ranging when in doubt
            
    def _adjust_grid_based_on_market_state(self, state):
        """
        Dynamically adjust grid parameters based on detected market state
        
        Args:
            state: MarketState enum value
        """
        if not self.is_running:
            return
            
        # Save original settings before modification (if not already saved)
        if not hasattr(self, '_original_grid_settings'):
            self._original_grid_settings = {
                'grid_spacing': self.grid_spacing,
            }
        
        if state == MarketState.PUMP:
            # PUMP state: Increase grid spacing to catch larger moves
            self.grid_spacing = min(self.grid_spacing * 1.25, config.MAX_GRID_SPACING)
            self.logger.info(f"PUMP state adjustment: Grid spacing increased to {self.grid_spacing*100:.2f}%")
            reduction = getattr(config, "PROTECTIVE_TREND_LEVEL_REDUCTION", 0.5)
            self.level_reduction_factor = reduction if self.protection_enabled else 1.0
            self.logger.info("Grid levels scaled by %.2f due to PUMP state", self.level_reduction_factor)
            
        elif state == MarketState.CRASH:
            # CRASH state: Increase grid spacing similar to PUMP
            self.grid_spacing = min(self.grid_spacing * 1.25, config.MAX_GRID_SPACING)
            self.logger.info(f"CRASH state adjustment: Grid spacing increased to {self.grid_spacing*100:.2f}%")
            reduction = getattr(config, "PROTECTIVE_TREND_LEVEL_REDUCTION", 0.5)
            self.level_reduction_factor = reduction if self.protection_enabled else 1.0
            self.logger.info("Grid levels scaled by %.2f due to CRASH state", self.level_reduction_factor)
            
        elif state == MarketState.BREAKOUT:
            # BREAKOUT state: Slightly increase spacing
            self.grid_spacing = min(self.grid_spacing * 1.15, config.MAX_GRID_SPACING)
            self.logger.info(f"BREAKOUT state adjustment: Grid spacing increased to {self.grid_spacing*100:.2f}%")
            self.level_reduction_factor = 0.75 if self.protection_enabled else 1.0
            self.logger.info("Grid levels scaled by %.2f due to BREAKOUT state", self.level_reduction_factor)
            
        elif state == MarketState.RANGING and hasattr(self, '_original_grid_settings'):
            # Restore original settings in RANGING state
            self.grid_spacing = self._original_grid_settings.get('grid_spacing', self.grid_spacing)
            self.logger.info(f"RANGING state: Grid spacing restored to {self.grid_spacing*100:.2f}%")
            self.level_reduction_factor = 1.0
            
            # Clear saved settings
            delattr(self, '_original_grid_settings')

    def get_market_state(self):
        """
        Get current market state information for external use
        
        Returns:
            dict: Dictionary containing market state and related metrics
        """
        # Ensure market state exists
        if not hasattr(self, 'current_market_state'):
            self.current_market_state = MarketState.RANGING
            
        # Get current metrics
        atr_value, trend_strength = self.calculate_market_metrics()
        volume_change = self._calculate_volume_change() if hasattr(self, '_calculate_volume_change') else 1.0
        
        # Return comprehensive state information
        return {
            'state': self.current_market_state,
            'state_name': self.current_market_state.name,
            'trend_strength': trend_strength,
            'volatility': atr_value,
            'volume_change': volume_change,
            'last_changed': self.last_state_change_time if hasattr(self, 'last_state_change_time') else 0
        }

    def _should_pause_orders(self):
        """Return True when protective mode wants to pause new orders (strong trend)."""
        if not getattr(self, "protection_enabled", False):
            return False
        pause_on_trend = getattr(config, "PROTECTIVE_PAUSE_STRONG_TREND", True)
        if pause_on_trend and self.current_market_state in (MarketState.PUMP, MarketState.CRASH):
            return True
        return False

    def _estimate_slippage_and_spread(self, side, quantity):
        """
        Estimate slippage and current book spread using order book depth.
        Returns (slippage_decimal, book_spread_decimal, mid_price) or None on failure.
        """
        try:
            ob = self.binance_client.get_order_book(self.symbol, limit=20)
            bids = [[float(p), float(q)] for p, q in ob.get("bids", []) if q is not None]
            asks = [[float(p), float(q)] for p, q in ob.get("asks", []) if q is not None]
            if not bids or not asks:
                return None
            best_bid = bids[0][0]
            best_ask = asks[0][0]
            mid = (best_bid + best_ask) / 2

            def avg_fill(levels, qty, is_buy):
                remaining = qty
                total_cost = 0.0
                for price, size in levels:
                    take = min(remaining, size)
                    total_cost += take * price
                    remaining -= take
                    if remaining <= 0:
                        break
                if remaining > 0:
                    return None
                avg_price = total_cost / qty if qty > 0 else None
                if avg_price is None:
                    return None
                if is_buy:
                    slip = max(0.0, (avg_price - best_ask) / best_ask)
                else:
                    slip = max(0.0, (best_bid - avg_price) / best_bid)
                return slip, avg_price

            if side == "BUY":
                fill = avg_fill(asks, quantity, True)
            else:
                fill = avg_fill(bids, quantity, False)
            if not fill:
                return None

            slippage, avg_price = fill
            book_spread = (best_ask - best_bid) / mid if mid > 0 else 0
            return slippage, book_spread, mid
        except Exception as e:
            self.logger.warning(f"Slippage estimation failed: {e}")
            return None

    def _has_profit_potential(self, level, current_price, round_trip_fee, min_profit_buffer):
        """
        Check if a stale order still has reasonable upside versus fees.
        """
        try:
            potential = abs(level["price"] - current_price) / current_price
            required = (round_trip_fee * config.PROFIT_MARGIN_MULTIPLIER) + min_profit_buffer
            return potential >= required
        except Exception:
            return False
    
    def calculate_optimal_grid_center(self):
        """
        Calculate optimal grid center using mean reversion properties of historical data.
        
        Balances historical average prices with current price based on trend strength.
        Uses existing trend and volatility metrics when possible.
        
        Returns:
            tuple: (grid_center, grid_range_value) - Optimized grid center and range
        """
        try:
            # 1. Use existing methods to get market metrics
            atr_value, trend_strength = self.calculate_market_metrics()
            
            # 2. Get historical data for mean reversion analysis
            # Use cached klines to avoid rate limits
            historical_klines = self._get_cached_klines(
                symbol=self.symbol, 
                interval="4h",  # 4-hour candles balance detail and noise reduction
                limit=48        # ~8 days - optimized for volatile altcoins, prevents old data from dragging center
            )
            
            if not historical_klines or len(historical_klines) < 20:  # Need at least 20 periods (~3 days)
                self.logger.warning("Insufficient historical data for mean reversion analysis")
                current_price = self.binance_client.get_symbol_price(self.symbol)
                return current_price, self.grid_range_percent
            
            # 3. Segment data into time periods
            recent_data = historical_klines[-42:]     # Last 7 days (most important)
            medium_data = historical_klines[-126:-42] # 7-21 days ago
            long_data = historical_klines[:-126]      # 21-60 days ago
            
            # 4. Calculate time-weighted average prices
            def calculate_weighted_avg(klines):
                total_weight = 0
                weighted_sum = 0
                
                # Weight more recent candles higher within each segment
                for i, k in enumerate(klines):
                    if isinstance(k, list) and len(k) > 4:
                        # Simple linear weight - newer data gets higher weight
                        weight = (i + 1) / len(klines)
                        close_price = float(k[4])  # Close price
                        weighted_sum += close_price * weight
                        total_weight += weight
                
                return weighted_sum / total_weight if total_weight > 0 else 0
                
            recent_avg = calculate_weighted_avg(recent_data)
            medium_avg = calculate_weighted_avg(medium_data)
            long_avg = calculate_weighted_avg(long_data)
            
            # 5. Calculate oscillation degree from trend strength (reuse existing trend data)
            # Transform trend_strength (-1 to 1) into oscillation index (0 to 1)
            # Strong trends (trend_strength near -1 or 1) mean low oscillation
            # Neutral trends (trend_strength near 0) mean high oscillation
            oscillation_index = 1 - abs(trend_strength)
            
            # 6. Dynamic time weights based on oscillation strength
            # More oscillation = more weight on recent data
            recent_weight = 0.45 + (oscillation_index * 0.1)  # 0.45-0.55 range
            medium_weight = 0.30
            long_weight = 0.25 - (oscillation_index * 0.1)    # 0.15-0.25 range
            
            # 7. Calculate historical weighted center
            historical_center = (
                recent_avg * recent_weight + 
                medium_avg * medium_weight + 
                long_avg * long_weight
            )
            
            # 8. Get current price
            current_price = self.binance_client.get_symbol_price(self.symbol)
            
            # 9. Calculate price deviation to dynamically adjust weights
            price_deviation = abs(current_price - historical_center) / historical_center
            
            # 10. Determine final weights based on price deviation and oscillation
            # CRITICAL FIX: Increase base weight to 0.80 (80%) for current price to prevent
            # grid center from deviating too far from market price in trending markets.
            # This fixes the bug where sell orders were generated below current price.
            if price_deviation > 0.15:
                # Large deviation - possible market structure change, maximum weight to current price
                current_weight = 0.85
            elif price_deviation > 0.08:
                # Moderate deviation
                current_weight = 0.82
            else:
                # Small deviation - still prioritize current price heavily
                current_weight = 0.80  # Base 80% weight for current price
                
            # 11. Calculate final grid center
            grid_center = (historical_center * (1 - current_weight) + 
                          current_price * current_weight)

            # 11b. Dynamic deviation limit based on market state
            # Strong trends need wider limits to follow price movement
            # Ranging markets need tighter limits to optimize grid density
            if abs(trend_strength) > 0.8:
                # Strong trend (PUMP/CRASH): allow 8% deviation to follow the trend
                max_dev = 0.08
                market_state = "strong trend"
            elif abs(trend_strength) > 0.5:
                # Moderate trend: allow 5% deviation
                max_dev = 0.05
                market_state = "moderate trend"
            else:
                # Ranging market: keep tight 3% deviation
                max_dev = getattr(config, "MAX_CENTER_DEVIATION", 0.03)
                market_state = "ranging"
            
            deviation = abs(grid_center - current_price) / current_price
            if deviation > max_dev:
                direction = 1 if grid_center > current_price else -1
                adjusted_center = current_price * (1 + direction * max_dev)
                self.logger.info(
                    "Grid center deviation %.2f%% exceeds max %.2f%% (%s), clamping to %.8f",
                    deviation * 100,
                    max_dev * 100,
                    market_state,
                    adjusted_center,
                )
                grid_center = adjusted_center
            
            # 12. Use ATR for grid range calculation if available
            grid_range_value = self.grid_range_percent  # Default to config value
            
            if atr_value:
                # Use ATR to determine appropriate range (as percentage of center)
                normalized_atr = atr_value / grid_center
                
                # Oscillation-adjusted range multiplier
                range_multiplier = 2.2 + (oscillation_index * 0.8)  # 2.2-3.0 range
                
                # Calculate and constrain grid range
                grid_range_value = min(
                    normalized_atr * range_multiplier,
                    self.grid_range_percent,
                    config.MAX_GRID_RANGE
                )
            
            self.logger.info(
                f"Optimal grid center: {grid_center:.8f} "
                f"(historical weight: {(1-current_weight)*100:.1f}%, "
                f"current weight: {current_weight*100:.1f}%, "
                f"oscillation: {oscillation_index:.2f})"
            )
            
            return grid_center, grid_range_value
            
        except Exception as e:
            self.logger.error(f"Error calculating optimal grid center: {e}")
            # Fallback to current price on error
            current_price = self.binance_client.get_symbol_price(self.symbol)
            return current_price, self.grid_range_percent
