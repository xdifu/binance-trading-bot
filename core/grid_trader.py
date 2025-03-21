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
        
        # Initialize logger (moved to top)
        self.logger = logging.getLogger(__name__)
        
        # Get symbol information and set precision
        self.symbol_info = self.binance_client.get_symbol_info(self.symbol)
        self.price_precision = self._get_price_precision()
        self.quantity_precision = self._get_quantity_precision()
        
        self.logger.info(f"Trading pair {self.symbol} price precision: {self.price_precision}, quantity precision: {self.quantity_precision}")
        
        self.grid = []  # [{'price': float, 'order_id': int, 'side': str}]
        self.last_recalculation = None
        self.current_market_price = 0
        self.is_running = False
        self.simulation_mode = False  # Add simulation mode flag
    
    def start(self, simulation=False):
        """Start grid trading system"""
        if self.is_running:
            return "System already running"
        
        self.is_running = True
        self.simulation_mode = simulation
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
        
        message = f"Grid trading system started!\nCurrent price: {self.current_market_price}\nGrid range: {len(self.grid)} levels"
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
        """Adjust price precision"""
        return format_price(price, self.price_precision)
    
    def _adjust_quantity_precision(self, quantity):
        """Adjust quantity precision"""
        return format_quantity(quantity, self.quantity_precision)
    
    def _setup_grid(self):
        """Set up grid"""
        # Calculate grid levels
        self.grid = self._calculate_grid_levels()
        
        # Place grid orders
        for level in self.grid:
            price = level['price']
            side = level['side']
            
            # Calculate order quantity
            if side == 'BUY':
                quantity = self.capital_per_level / price
            else:  # SELL
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
            return None
    
    def _cancel_all_open_orders(self):
        """Cancel all open orders"""
        try:
            if self.simulation_mode:
                self.logger.info("Simulation mode - Would cancel all open orders")
                return
                
            open_orders = self.binance_client.get_open_orders(self.symbol)
            for order in open_orders:
                self.binance_client.cancel_order(self.symbol, order['orderId'])
                self.logger.info(f"Order cancelled: {order['orderId']}")
        except Exception as e:
            self.logger.error(f"Failed to cancel orders: {e}")
    
    def handle_order_update(self, order_data):
        """Handle order update"""
        if not self.is_running:
            return
        
        # Handle order filled event
        if order_data['X'] == 'FILLED':
            order_id = order_data['i']
            symbol = order_data['s']
            side = order_data['S']
            price = float(order_data['p'])
            quantity = float(order_data['q'])
            
            if symbol != self.symbol:
                return
            
            # Find matching grid level
            for i, level in enumerate(self.grid):
                if level['order_id'] == order_id:
                    self.logger.info(f"Order filled: {side} {quantity} @ {price}")
                    
                    # Create opposite order
                    new_side = "SELL" if side == "BUY" else "BUY"
                    
                    # Adjust quantity and price precision
                    formatted_quantity = self._adjust_quantity_precision(quantity)
                    formatted_price = self._adjust_price_precision(price)
                    
                    try:
                        if self.simulation_mode:
                            self.logger.info(f"Simulation - Would place opposite order: {new_side} {formatted_quantity} @ {formatted_price}")
                            level['side'] = new_side
                            level['order_id'] = f"sim_{int(time.time())}_{new_side}_{formatted_price}"
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
                        
                        # Place opposite order
                        new_order = self.binance_client.place_limit_order(
                            self.symbol, 
                            new_side, 
                            formatted_quantity, 
                            formatted_price
                        )
                        
                        # Update grid
                        self.grid[i]['side'] = new_side
                        self.grid[i]['order_id'] = new_order['orderId']
                        
                        message = f"Grid trade executed: {side} {formatted_quantity} @ {formatted_price}\nOpposite order placed: {new_side} {formatted_quantity} @ {formatted_price}"
                        self.logger.info(message)
                        if self.telegram_bot:
                            self.telegram_bot.send_message(message)
                    except Exception as e:
                        self.logger.error(f"Failed to place opposite order: {e}")
                    break
    
    def check_grid_recalculation(self):
        """Check if grid recalculation is needed"""
        if not self.is_running or not self.last_recalculation:
            return
        
        days_since_last = (datetime.now() - self.last_recalculation).days
        
        if days_since_last >= self.recalculation_period:
            self.logger.info("Starting grid recalculation...")
            
            # Update current market price
            self.current_market_price = self.binance_client.get_symbol_price(self.symbol)
            
            # Cancel all open orders
            self._cancel_all_open_orders()
            
            # Recalculate and set grid
            self._setup_grid()
            
            self.last_recalculation = datetime.now()
            
            message = f"Grid recalculated!\nCurrent price: {self.current_market_price}\nGrid range: {len(self.grid)} levels"
            self.logger.info(message)
            if self.telegram_bot:
                self.telegram_bot.send_message(message)
    
    def get_status(self):
        """Get current status"""
        if not self.is_running:
            return "System not running"
        
        # Get current price
        try:
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
                    f"Grid details:\n" + "\n".join(grid_info)
            
            return status
        except Exception as e:
            self.logger.error(f"Failed to get status: {e}")
            return f"Failed to get status: {e}"