import logging
import time
import config
from datetime import datetime
from utils.format_utils import format_price, format_quantity, get_precision_from_filters

class RiskManager:
    def __init__(self, binance_client, telegram_bot=None, grid_trader=None):
        """
        Initialize risk management system
        
        Args:
            binance_client: BinanceClient instance for API operations
            telegram_bot: Optional TelegramBot instance for notifications
            grid_trader: Optional GridTrader instance for fund coordination
        """
        self.binance_client = binance_client
        self.telegram_bot = telegram_bot
        self.grid_trader = grid_trader  # Store reference to grid trader for fund coordination
        self.symbol = config.SYMBOL
        
        # Add validation for MIN_NOTIONAL_VALUE
        if not hasattr(config, 'MIN_NOTIONAL_VALUE'):
            raise AttributeError("config.MIN_NOTIONAL_VALUE is not defined. Please define it in the config module.")
    
        # Initialize logger first to enable logging
        self.logger = logging.getLogger(__name__)
        
        # Enhanced strategy for small capital accounts - more conservative stop loss and take profit
        base_stop_loss = config.TRAILING_STOP_LOSS_PERCENT / 100
        base_take_profit = config.TRAILING_TAKE_PROFIT_PERCENT / 100

        # For small capital accounts ($64), use tighter stops to protect capital
        capital_adjustment_factor = 0.8  # 20% tighter stops for small accounts

        # Adjust base values but ensure minimum safety distance
        self.trailing_stop_loss_percent = max(0.015, base_stop_loss * capital_adjustment_factor)  # At least 1.5% stop loss
        self.trailing_take_profit_percent = max(0.008, base_take_profit * capital_adjustment_factor)  # At least 0.8% take profit

        self.logger.info(f"Using optimized risk parameters for small capital: Stop loss at {self.trailing_stop_loss_percent*100:.2f}%, Take profit at {self.trailing_take_profit_percent*100:.2f}%")

        # Dynamic volatility tracking for adaptive risk management
        self.last_volatility_check = time.time()
        self.volatility_check_interval = 3600  # Check market volatility every hour
        self.volatility_adjustment_active = False  # Track if volatility-based adjustment is active
        
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
        
        # Optimized thresholds for small capital accounts - more frequent updates and smaller movements
        self.min_update_threshold_percent = 0.003  # 0.3% minimum price movement (reduced from 1%)
        self.min_update_interval_seconds = 300    # 5 minutes minimum between updates (reduced from 10 minutes)
        self.logger.info(f"Using optimized risk thresholds for small capital: {self.min_update_threshold_percent*100}% movement, {self.min_update_interval_seconds/60} minutes interval")
        
        # Track last update time
        self.last_update_time = 0
        
        # Track pending operations for better error handling
        self.pending_oco_orders = {}

        # Remove the dependency on CAPITAL_PER_LEVEL
        # Instead of directly using grid_trader.capital_per_level, we'll use dynamic allocation
        
        # Add new capital tracking attributes
        self.min_order_value = config.MIN_NOTIONAL_VALUE
        self.dynamic_allocation_enabled = True  # Flag to indicate we're using dynamic allocation
        
        self.logger.info("Using dynamic capital allocation for risk management orders")

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
            
            # Check for dynamic allocation capability in grid trader
            if self.grid_trader and hasattr(self.grid_trader, 'total_available_capital'):
                # Store reference to available capital for future checks
                self.total_grid_capital = self.grid_trader.total_available_capital
                self.logger.info(f"Risk management activated with dynamic allocation reference: {self.total_grid_capital:.2f} USDT total grid capital")
                
                # Set dynamic risk parameters based on account size
                if self.total_grid_capital < 100:  # Small account (<$100)
                    # Tighter risk management for small accounts
                    capital_adjustment_factor = 0.7  # 30% tighter stops
                elif self.total_grid_capital < 500:  # Medium account ($100-$500)
                    capital_adjustment_factor = 0.8  # 20% tighter stops
                else:  # Large account (>$500)
                    capital_adjustment_factor = 0.9  # 10% tighter stops
                    
                # Apply adjustment to risk parameters
                base_stop_loss = self.trailing_stop_loss_percent
                base_take_profit = self.trailing_take_profit_percent
                
                self.trailing_stop_loss_percent = max(0.005, base_stop_loss * capital_adjustment_factor)
                self.trailing_take_profit_percent = max(0.005, base_take_profit * capital_adjustment_factor)
                
                self.logger.info(f"Risk parameters adjusted based on capital size: SL {self.trailing_stop_loss_percent*100:.2f}%, TP {self.trailing_take_profit_percent*100:.2f}%")
            
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
                
            # Periodically check market volatility to adjust risk parameters
            current_time = time.time()
            if current_time - self.last_volatility_check > self.volatility_check_interval:
                try:
                    # Get symbol info with ATR if available
                    from utils.indicators import calculate_atr
                    
                    # Get klines for volatility assessment
                    klines = self.binance_client.get_historical_klines(
                        symbol=self.symbol,
                        interval="1h",
                        limit=24  # Use 24 hours of data
                    )
                    
                    if klines:
                        # Calculate ATR as volatility indicator
                        atr = calculate_atr(klines, period=14)
                        if atr:
                            # Calculate volatility as percentage of price
                            volatility_percent = atr / current_price
                            
                            # High volatility: above 1.5% hourly movement
                            high_volatility = volatility_percent > 0.015
                            
                            # Adjust risk parameters based on volatility
                            if high_volatility and not self.volatility_adjustment_active:
                                # In high volatility, increase protection
                                original_stop_loss = self.trailing_stop_loss_percent
                                original_take_profit = self.trailing_take_profit_percent
                                
                                # Tighten stops in high volatility
                                self.trailing_stop_loss_percent *= 0.8  # 20% tighter
                                self.trailing_take_profit_percent *= 0.8  # 20% tighter
                                
                                self.volatility_adjustment_active = True
                                self.logger.info(
                                    f"High volatility detected ({volatility_percent*100:.2f}%). "
                                    f"Adjusted stop loss: {self.trailing_stop_loss_percent*100:.2f}% "
                                    f"(from {original_stop_loss*100:.2f}%). "
                                    f"Adjusted take profit: {self.trailing_take_profit_percent*100:.2f}% "
                                    f"(from {original_take_profit*100:.2f}%)"
                                )
                            elif not high_volatility and self.volatility_adjustment_active:
                                # Reset to standard values in normal volatility
                                base_stop_loss = config.TRAILING_STOP_LOSS_PERCENT / 100
                                base_take_profit = config.TRAILING_TAKE_PROFIT_PERCENT / 100
                                capital_adjustment_factor = 0.8
                                
                                self.trailing_stop_loss_percent = max(0.015, base_stop_loss * capital_adjustment_factor)
                                self.trailing_take_profit_percent = max(0.008, base_take_profit * capital_adjustment_factor)
                                
                                self.volatility_adjustment_active = False
                                self.logger.info(
                                    f"Normal volatility detected ({volatility_percent*100:.2f}%). "
                                    f"Reset to standard risk parameters: "
                                    f"Stop loss: {self.trailing_stop_loss_percent*100:.2f}%, "
                                    f"Take profit: {self.trailing_take_profit_percent*100:.2f}%"
                                )
                
                    # Update last check time
                    self.last_volatility_check = current_time
                except Exception as e:
                    self.logger.error(f"Error in volatility adjustment: {e}")
            
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
            try:
                # Calculate ATR for dynamic safety margin
                klines = self.binance_client.get_historical_klines(
                    symbol=self.symbol,
                    interval="1h",  # Use 1-hour timeframe for stability
                    limit=14  # Standard ATR period
                )
                
                if klines:
                    from utils.indicators import calculate_atr
                    atr = calculate_atr(klines, period=14)
                    
                    # Use ATR as a percentage of price for relative volatility
                    atr_percent = atr / current_price if current_price > 0 else 0.01
                    
                    # Scale ATR based on account size and risk appetite
                    # Small accounts need more protection - use at least 1.5x ATR
                    atr_multiplier = 1.5
                    
                    # Ensure minimum safety margin regardless of ATR
                    min_safety_percent = 0.005  # 0.5% minimum
                    
                    # Calculate dynamic safety margins
                    sl_margin = max(min_safety_percent, atr_percent * atr_multiplier)
                    tp_margin = max(min_safety_percent, atr_percent * atr_multiplier)
                    
                    self.logger.info(f"Using ATR-based safety margins: SL {sl_margin*100:.2f}%, TP {tp_margin*100:.2f}% (ATR: {atr_percent*100:.2f}%)")
                    
                    # Apply the calculated margins
                    self.stop_loss_price = current_price * (1 - sl_margin)
                    self.take_profit_price = current_price * (1 + tp_margin)
                else:
                    # Fallback to standard calculation if ATR calculation fails
                    self.stop_loss_price = current_price * (1 - self.trailing_stop_loss_percent)
                    self.take_profit_price = current_price * (1 + self.trailing_take_profit_percent)
                    self.logger.warning("Could not calculate ATR, using fixed safety margins")
            except Exception as e:
                # Fallback to standard calculation on error
                self.stop_loss_price = current_price * (1 - self.trailing_stop_loss_percent)
                self.take_profit_price = current_price * (1 + self.trailing_take_profit_percent)
                self.logger.error(f"Error calculating ATR-based margins: {e}, using fixed safety margins")
            
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
            
            # Reserve portion of assets for grid trading
            if self.grid_trader and hasattr(self.grid_trader, 'locked_balances'):
                # Check if asset is locked by grid trader
                locked_by_grid = self.grid_trader.locked_balances.get(asset, 0)
                
                # Also check for dynamic allocation capability
                if hasattr(self.grid_trader, 'total_available_capital'):
                    # More intelligent reservation based on grid trader's allocation strategy
                    grid_allocation_percent = 0.7  # Reserve 70% for grid, leave 30% for risk management
                    total_available = self.grid_trader.total_available_capital
                    reserved_amount = max(
                        locked_by_grid,
                        asset_balance * grid_allocation_percent  # Base dynamic reservation on actual capital
                    )
                else:
                    # Fallback to simple percentage if grid trader doesn't use dynamic allocation
                    reserve_for_grid = 0.3  # Reserve 30% of available assets for grid trading
                    reserved_amount = max(locked_by_grid, asset_balance * reserve_for_grid)
                
                if asset_balance > reserved_amount:
                    original_balance = asset_balance
                    asset_balance -= reserved_amount
                    self.logger.info(f"Reserved {reserved_amount} {asset} for grid trading. Using {asset_balance}/{original_balance} for OCO orders")
                else:
                    self.logger.info(f"Insufficient {asset} balance for OCO orders after grid reservation")
                    return False

            # Calculate appropriate OCO quantity based on available balance and grid allocation
            if self.grid_trader and hasattr(self.grid_trader, '_calculate_dynamic_capital_for_level'):
                # Get a dynamic allocation based on current price
                suggested_allocation = self.grid_trader._calculate_dynamic_capital_for_level(current_price)
                
                # Calculate available percentage after grid reservation
                available_pct = 0.3  # Allow using 30% of assets for risk management
                
                # Calculate suggested quantity based on allocation
                suggested_qty = (asset_balance * available_pct) if asset_balance > 0 else 0
                
                # Fix: Use self.min_order_value instead of undefined min_notional_value
                min_notional_value = self.min_order_value
                
                # Ensure quantity meets minimum requirements
                min_qty_for_min_notional = min_notional_value / current_price
                quantity = max(suggested_qty, min_qty_for_min_notional)
                
                # Format with precision
                quantity = self._adjust_quantity_precision(quantity)
                
                self.logger.info(f"Using dynamic allocation for OCO orders: {quantity} {asset} (based on {available_pct*100}% of balance)")
            else:
                # Original code for quantity calculation
                # Fix: Use self.min_order_value instead of undefined min_notional_value
                min_notional_value = self.min_order_value
                min_qty_for_min_notional = min_notional_value / current_price
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
            
            # Get minimum notional value dynamically based on current conditions
            if hasattr(self.grid_trader, 'current_capital_per_grid'):
                # Use a fraction of grid trader's dynamic capital as minimum for risk orders
                min_notional_value = max(
                    config.MIN_NOTIONAL_VALUE,  # Base minimum
                    self.grid_trader.current_capital_per_grid * 0.5  # Half of current grid capital
                )
                self.logger.debug(f"Using dynamic minimum order value: {min_notional_value} based on grid allocation")
            else:
                # Fallback to config value
                min_notional_value = config.MIN_NOTIONAL_VALUE

            # Calculate order value
            order_value = float(quantity) * current_price

            # Check if order meets minimum value requirement
            if order_value < min_notional_value:
                self.logger.warning(
                    f"OCO order value ({order_value:.2f} USDT) below minimum required ({min_notional_value} USDT). "
                    f"Adjusting quantity to meet minimum requirement."
                )
                
                # Calculate new quantity to meet minimum
                adjusted_quantity = min_notional_value / current_price
                
                # Format quantity with precision
                quantity = self._adjust_quantity_precision(adjusted_quantity)
                
                # Verify the adjustment worked
                new_order_value = float(quantity) * current_price
                
                if new_order_value < min_notional_value:
                    self.logger.error(
                        f"Cannot place OCO orders: even with adjustment, order value ({new_order_value:.2f} USDT) "
                        f"still below minimum ({min_notional_value} USDT)"
                    )
                    return False
                
                self.logger.info(f"Adjusted quantity to {quantity} to meet minimum order value")

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
            self.logger.error(f"Failed to create OCO order: {e}", exc_info=True)
            
            # Log API response details if available
            if hasattr(e, 'response') and hasattr(e.response, 'text'):
                self.logger.error(f"API response: {e.response.text}")
                
            # Provide more actionable information
            error_message = str(e).lower()
            if "insufficient balance" in error_message:
                self.logger.warning(f"Insufficient balance detected. Required: {quantity} {asset}, checking actual balance...")
                # Log actual balance for comparison
                try:
                    actual_balance = self.binance_client.check_balance(asset)
                    self.logger.info(f"Actual {asset} balance: {actual_balance}")
                except Exception as balance_error:
                    self.logger.error(f"Failed to get balance info: {balance_error}")
            elif "price" in error_message and ("invalid" in error_message or "trigger" in error_message):
                self.logger.warning(f"Price validation issue. SL: {stop_price}, TP: {limit_price}, Current: {current_price_str}")
            elif "quantity" in error_message:
                self.logger.warning(f"Quantity validation issue. Check LOT_SIZE filter. Quantity: {quantity}")
            
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
            # Check order status before attempting to cancel
            try:
                order_status = None
                order_list_status = None
                
                # Try to get the OCO order status
                order_details = self.binance_client.get_oco_order(self.symbol, orderListId=self.oco_order_id)
                
                if order_details:
                    # Extract status from response
                    if 'listStatusType' in order_details:
                        order_list_status = order_details['listStatusType']
                    elif 'result' in order_details and 'listStatusType' in order_details['result']:
                        order_list_status = order_details['result']['listStatusType']
                        
                    self.logger.info(f"OCO order {self.oco_order_id} current status: {order_list_status}")
                    
                    # If order is already in terminal state, just update our tracking
                    if order_list_status in ['ALL_DONE', 'EXPIRED', 'CANCELLED']:
                        self.logger.info(f"OCO order {self.oco_order_id} already in terminal state: {order_list_status}, no cancellation needed")
                        # Capture the original order ID before resetting
                        original_order_id = str(self.oco_order_id)
                        self.oco_order_id = None
                        
                        # Clear from pending orders tracking
                        if original_order_id in self.pending_oco_orders:
                            self.pending_oco_orders.pop(original_order_id)
                            
                        return
            except Exception as status_error:
                # Log error but proceed with cancellation attempt
                self.logger.warning(f"Error checking OCO order status: {status_error}, proceeding with cancellation")

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
                    response = self.binance_client.rest_client.cancel_oco_order(  # 修改: cancel_order_list -> cancel_oco_order
                        symbol=self.symbol,
                        orderListId=self.oco_order_id
                    )
            else:
                # Using REST client directly
                response = self.binance_client.rest_client.cancel_oco_order(  # 修改: cancel_order_list -> cancel_oco_order
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
                        self.binance_client.rest_client.cancel_oco_order(  # 修改: cancel_order_list -> cancel_oco_order
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
    
    def update_symbol(self, new_symbol):
        """
        Update the trading symbol and reset related state
        
        Args:
            new_symbol: New trading symbol to use
        """
        old_symbol = self.symbol
        self.symbol = new_symbol
        
        # Reset price tracking for the new symbol
        self.stop_loss_price = None
        self.highest_price = None
        self.lowest_price = None
        self.take_profit_price = None
        
        # Cancel any existing OCO orders
        if self.oco_order_id:
            self._cancel_oco_orders()
            self.oco_order_id = None
        
        # Reset pending operations tracking
        self.pending_oco_orders = {}
        
        # Update symbol info and precision settings for the new symbol
        self.symbol_info = self._get_symbol_info()
        self.price_precision = self._get_price_precision()
        self.quantity_precision = self._get_quantity_precision()
        
        self.logger.info(f"Risk manager symbol updated from {old_symbol} to {new_symbol}")

    def _check_for_missing_oco_orders(self):
        """
        Check if OCO orders should be placed but are missing,
        and attempt to place them if funds are now available.
        With support for dynamic capital allocation.
        
        Returns:
            bool: True if OCO orders were placed, False otherwise
        """
        if not self.is_active:
            return False
            
        # Skip if we already have active OCO orders
        if self.oco_order_id:
            return False
            
        # Check if we have sufficient funds now
        asset = self.symbol.replace('USDT', '')
        balance = self.binance_client.check_balance(asset)
        
        # If grid trader is using dynamic allocation, check for available capital
        if self.grid_trader and hasattr(self.grid_trader, 'total_available_capital'):
            # Check if there's significant capital change that might enable OCO orders
            if hasattr(self, 'last_checked_capital'):
                capital_increase = self.grid_trader.total_available_capital - self.last_checked_capital
                significant_increase = capital_increase > config.MIN_NOTIONAL_VALUE * 2  # Twice minimum order value
                
                if significant_increase:
                    self.logger.info(f"Detected capital increase of {capital_increase:.2f} USDT, reassessing OCO order placement")
                    
            # Update last checked capital
            self.last_checked_capital = self.grid_trader.total_available_capital
        
        # Check base asset balance (existing code)
        if balance > 0:
            min_quantity_needed = config.MIN_NOTIONAL_VALUE / self.binance_client.get_symbol_price(self.symbol)
            if balance > min_quantity_needed:
                self.logger.info(f"Detected {balance} {asset} available for risk management, placing OCO orders")
                return self._place_oco_orders()
                
        return False

    def update_stop_loss_take_profit(self, grid_lowest, grid_highest):
        """
        Update stop loss and take profit based on grid boundaries
        
        Args:
            grid_lowest (float): Lowest price in the grid
            grid_highest (float): Highest price in the grid
        """
        if not self.is_active:
            self.logger.debug("Risk manager not active, ignoring stop loss/take profit update")
            return False
            
        # Verify inputs are valid
        if grid_lowest is None or grid_highest is None or grid_lowest <= 0 or grid_highest <= 0:
            self.logger.warning(f"Invalid grid boundaries for SL/TP update: low={grid_lowest}, high={grid_highest}")
            return False
            
        # Get current price for reference
        try:
            current_price = self.binance_client.get_symbol_price(self.symbol)
            
            # Calculate new stop loss and take profit prices based on grid boundaries
            # Use a percentage of the grid range for improved protection
            
            # Store old values for logging
            old_sl = self.stop_loss_price
            old_tp = self.take_profit_price
            
            # Calculate new values based on grid boundaries with padding
            # For small capital accounts, be more conservative with stop loss
            grid_range = grid_highest - grid_lowest
            grid_center = (grid_highest + grid_lowest) / 2
            
            # Set stop loss below grid bottom with trailing_stop_loss_percent padding
            self.stop_loss_price = grid_lowest * (1 - self.trailing_stop_loss_percent)
            
            # Set take profit above grid top with trailing_take_profit_percent padding
            self.take_profit_price = grid_highest * (1 + self.trailing_take_profit_percent)
            
            # Log the changes
            self.logger.info(
                f"Updated risk parameters based on grid boundaries [{grid_lowest:.8f} - {grid_highest:.8f}]\n"
                f"Stop loss: {old_sl:.8f} -> {self.stop_loss_price:.8f}\n"
                f"Take profit: {old_tp:.8f} -> {self.take_profit_price:.8f}"
            )
            
            # If we have an active OCO order, cancel and replace it
            if self.oco_order_id:
                self.logger.info("Cancelling existing OCO orders to apply new risk parameters")
                self._cancel_oco_orders()
                self._place_oco_orders()
                
            return True
        except Exception as e:
            self.logger.error(f"Failed to update stop loss/take profit: {e}")
            return False

    def verify_alignment_with_grid(self, grid_trader):
        """
        Verify that risk management settings align with current grid state
        with support for dynamic capital allocation
        
        Args:
            grid_trader: The grid trader instance to check against
            
        Returns:
            bool: True if aligned, False if needs adjustment
        """
        if not self.is_active or not grid_trader or not grid_trader.grid:
            return False
            
        try:
            # Check if the grid trader uses dynamic allocation
            has_dynamic_allocation = hasattr(grid_trader, 'dynamic_grid_levels')
            
            # Get grid price boundaries
            grid_prices = [level['price'] for level in grid_trader.grid if level.get('price')]
            if not grid_prices:
                self.logger.warning("Grid has no price levels, cannot verify alignment")
                return False
                
            grid_lowest = min(grid_prices)
            grid_highest = max(grid_prices)
            
            # Get current price
            current_price = self.binance_client.get_symbol_price(self.symbol)
            
            # For dynamic allocation, check if grid structure has changed significantly
            if has_dynamic_allocation:
                current_grid_levels = grid_trader.dynamic_grid_levels
                if hasattr(self, 'last_verified_grid_levels') and self.last_verified_grid_levels != current_grid_levels:
                    self.logger.info(f"Grid structure changed from {self.last_verified_grid_levels} to {current_grid_levels} levels")
                    self.update_stop_loss_take_profit(grid_lowest, grid_highest)
                    self.last_verified_grid_levels = current_grid_levels
                    return True
                    
                # Store current level count for future comparison
                self.last_verified_grid_levels = current_grid_levels
                
            # Calculate expected stop loss and take profit 
            expected_sl = grid_lowest * (1 - self.trailing_stop_loss_percent)
            expected_tp = grid_highest * (1 + self.trailing_take_profit_percent)
            
            # Check if current settings deviate significantly
            sl_deviation = abs(self.stop_loss_price - expected_sl) / expected_sl if expected_sl > 0 else 1
            tp_deviation = abs(self.take_profit_price - expected_tp) / expected_tp if expected_tp > 0 else 1
            
            # If deviation exceeds threshold, update
            if sl_deviation > 0.05 or tp_deviation > 0.05:  # 5% threshold
                self.logger.info(
                    f"Risk management not aligned with grid (deviation SL: {sl_deviation*100:.1f}%, TP: {tp_deviation*100:.1f}%). "
                    f"Updating to match grid boundaries: [{grid_lowest:.8f} - {grid_highest:.8f}]"
                )
                
                # Update to match grid
                self.update_stop_loss_take_profit(grid_lowest, grid_highest)
                return True
            
            self.logger.debug("Risk management aligned with current grid state")
            return False
            
        except Exception as e:
            self.logger.error(f"Error verifying risk alignment with grid: {e}")
            return False

    # Add this method to periodically check for capital changes and adjust OCO orders
    def check_capital_changes(self):
        """
        Check for significant changes in available capital and adjust risk orders if needed
        
        Returns:
            bool: True if adjustments were made, False otherwise
        """
        if not self.is_active or not self.grid_trader:
            return False
            
        # Only applicable for grid traders with dynamic allocation
        if not hasattr(self.grid_trader, 'total_available_capital'):
            return False
            
        # Get current total capital
        current_capital = self.grid_trader.total_available_capital
        
        # Check if we have a reference value to compare against
        if not hasattr(self, 'last_capital_check') or not hasattr(self, 'total_grid_capital'):
            # Initialize reference values
            self.last_capital_check = current_capital
            self.total_grid_capital = current_capital
            return False
            
        # Calculate capital change
        capital_change = abs(current_capital - self.last_capital_check) / self.last_capital_check
        
        # If significant change (>20%), readjust OCO orders
        if capital_change > 0.2:  # 20% threshold
            self.logger.info(f"Significant capital change detected: {capital_change*100:.1f}% ({self.last_capital_check:.2f}->{current_capital:.2f} USDT)")
            
            # Update reference values
            self.last_capital_check = current_capital
            self.total_grid_capital = current_capital
            
            # Cancel and replace OCO orders with new allocation
            if self.oco_order_id:
                self.logger.info("Cancelling existing OCO orders to adjust for capital change")
                self._cancel_oco_orders()
                
            # Place new OCO orders with updated allocations
            placement_result = self._place_oco_orders()
            return placement_result
        
        # Update reference value even if no significant change
        self.last_capital_check = current_capital
        return False