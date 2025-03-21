import logging
import time
from datetime import datetime
from utils.format_utils import format_price, format_quantity, get_precision_from_filters
import config

class RiskManager:
    def __init__(self, binance_client, telegram_bot=None):
        self.binance_client = binance_client
        self.telegram_bot = telegram_bot
        self.symbol = config.SYMBOL
        self.trailing_stop_loss_percent = config.TRAILING_STOP_LOSS_PERCENT / 100  # Convert to decimal
        self.trailing_take_profit_percent = config.TRAILING_TAKE_PROFIT_PERCENT / 100  # Convert to decimal
        
        # Get symbol information and set precision
        self.symbol_info = self.binance_client.get_symbol_info(self.symbol)
        self.price_precision = self._get_price_precision()
        self.quantity_precision = self._get_quantity_precision()
        
        self.stop_loss_price = None
        self.highest_price = None
        self.lowest_price = None
        self.take_profit_price = None
        self.oco_order_id = None
        
        self.is_active = False
        self.logger = logging.getLogger(__name__)

    def _get_price_precision(self):
        """Get price precision"""
        if not self.symbol_info or 'filters' not in self.symbol_info:
            return 2  # Default price precision
        return get_precision_from_filters(self.symbol_info['filters'], 'PRICE_FILTER', 'tickSize')
    
    def _get_quantity_precision(self):
        """Get quantity precision"""
        if not self.symbol_info or 'filters' not in self.symbol_info:
            return 5  # Default quantity precision
        return get_precision_from_filters(self.symbol_info['filters'], 'LOT_SIZE', 'stepSize')
        
    def _adjust_price_precision(self, price):
        """Adjust price precision"""
        return format_price(price, self.price_precision)
    
    def _adjust_quantity_precision(self, quantity):
        """Adjust quantity precision"""
        return format_quantity(quantity, self.quantity_precision)
        
    def activate(self, grid_lowest, grid_highest):
        """Activate risk management"""
        try:
            # Get current price
            current_price = self.binance_client.get_symbol_price(self.symbol)
            
            # Set initial stop loss and take profit prices
            self.stop_loss_price = grid_lowest * (1 - self.trailing_stop_loss_percent)
            self.take_profit_price = grid_highest * (1 + self.trailing_take_profit_percent)
            
            # Set historical high and low prices
            self.highest_price = current_price
            self.lowest_price = current_price
            
            message = (f"Risk Management Activated\n"
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
            
    def deactivate(self):
        """Deactivate risk management"""
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
            
            message = "Risk Management Deactivated"
            self.logger.info(message)
            if self.telegram_bot:
                self.telegram_bot.send_message(message)
        except Exception as e:
            self.logger.error(f"Failed to deactivate risk management: {e}")

    def check_price(self, current_price):
        """Check if price requires updating trailing stop loss or take profit"""
        if not self.is_active:
            return None
        
        update_required = False
        
        # Update historical high and low prices
        if current_price > self.highest_price:
            self.highest_price = current_price
            # Update trailing take profit price - FIXED: using addition instead of subtraction
            new_take_profit = current_price * (1 + self.trailing_take_profit_percent)
            # FIXED: only update if new take profit is higher (trailing behavior)
            if new_take_profit > self.take_profit_price:
                self.take_profit_price = new_take_profit
                update_required = True
                self.logger.info(f"Updated trailing take profit price: {self.take_profit_price}")
        
        if current_price < self.lowest_price:
            self.lowest_price = current_price
            # Update trailing stop loss price
            new_stop_loss = current_price * (1 - self.trailing_stop_loss_percent)
            # Only update if new stop loss is lower than current one (trailing behavior)
            if new_stop_loss < self.stop_loss_price:
                self.stop_loss_price = new_stop_loss
                update_required = True
                self.logger.info(f"Updated trailing stop loss price: {self.stop_loss_price}")
        
        # Update orders if needed
        if update_required and self.oco_order_id:
            self._cancel_oco_orders()
            self._place_oco_orders()
            
        return None  # No direct operation is returned, executed via OCO orders

    def _place_oco_orders(self):
        """Create OCO (One-Cancels-the-Other) order pair for stop loss and take profit"""
        try:
            # Get account balance
            balance = self.binance_client.get_account_info()
            asset = self.symbol.replace('USDT', '')
            
            # Find corresponding asset balance
            asset_balance = None
            for item in balance['balances']:
                if item['asset'] == asset:
                    asset_balance = float(item['free'])
                    break
            
            if not asset_balance or asset_balance <= 0:
                self.logger.info(f"No available {asset} for risk management")
                return
            
            # Set quantity (all available assets) and format
            quantity = self._adjust_quantity_precision(asset_balance)
            
            # Get current price
            current_price = self.binance_client.get_symbol_price(self.symbol)
            
            # Format stop loss and take profit prices
            stop_price = self._adjust_price_precision(self.stop_loss_price)
            stop_limit_price = self._adjust_price_precision(self.stop_loss_price * 0.99)
            limit_price = self._adjust_price_precision(self.take_profit_price)
            
            # Place OCO order
            response = self.binance_client.new_oco_order(
                symbol=self.symbol,
                side="SELL",  # Sell assets
                quantity=quantity,
                price=limit_price,  # Take profit price
                stopPrice=stop_price,  # Stop loss trigger price
                stopLimitPrice=stop_limit_price,  # Stop limit price
                stopLimitTimeInForce="GTC"  # Good Till Cancel
            )
            
            self.oco_order_id = response.get('orderListId')
            
            message = (f"Risk Management OCO Order Created\n"
                      f"Quantity: {quantity} {asset}\n"
                      f"Stop loss trigger: {stop_price}\n"
                      f"Take profit price: {limit_price}")
            self.logger.info(message)
            if self.telegram_bot:
                self.telegram_bot.send_message(message)
                
        except Exception as e:
            self.logger.error(f"Failed to create OCO order: {e}")

    def _cancel_oco_orders(self):
        """Cancel existing OCO orders"""
        if not self.oco_order_id:
            return
            
        try:
            self.binance_client.client.cancel_oco_order(
                symbol=self.symbol,
                orderListId=self.oco_order_id
            )
            self.logger.info(f"Cancelled OCO order ID: {self.oco_order_id}")
            self.oco_order_id = None
        except Exception as e:
            # Ignore errors for orders that can't be found - likely already executed or cancelled
            self.logger.warning(f"Failed to cancel OCO order: {e}")
            self.oco_order_id = None

    def execute_stop_loss(self):
        """Execute stop loss operation"""
        try:
            # Get account balance and close all positions
            balance = self.binance_client.get_account_info()
            asset = self.symbol.replace('USDT', '')
            
            # Find corresponding asset balance
            asset_balance = None
            for item in balance['balances']:
                if item['asset'] == asset:
                    asset_balance = float(item['free'])
                    break
            
            if asset_balance and asset_balance > 0:
                # Format quantity
                formatted_quantity = self._adjust_quantity_precision(asset_balance)
                
                # Market sell all assets
                self.binance_client.place_market_order(self.symbol, "SELL", formatted_quantity)
                message = f"Stop Loss Executed: Market sold {formatted_quantity} {asset}"
                self.logger.info(message)
                if self.telegram_bot:
                    self.telegram_bot.send_message(message)
        except Exception as e:
            self.logger.error(f"Failed to execute stop loss: {e}")

    def execute_take_profit(self):
        """Execute take profit operation"""
        try:
            # Get account balance and close all positions
            balance = self.binance_client.get_account_info()
            asset = self.symbol.replace('USDT', '')
            
            # Find corresponding asset balance
            asset_balance = None
            for item in balance['balances']:
                if item['asset'] == asset:
                    asset_balance = float(item['free'])
                    break
            
            if asset_balance and asset_balance > 0:
                # Format quantity
                formatted_quantity = self._adjust_quantity_precision(asset_balance)
                
                # Market sell all assets
                self.binance_client.place_market_order(self.symbol, "SELL", formatted_quantity)
                message = f"Take Profit Executed: Market sold {formatted_quantity} {asset}"
                self.logger.info(message)
                if self.telegram_bot:
                    self.telegram_bot.send_message(message)
        except Exception as e:
            self.logger.error(f"Failed to execute take profit: {e}")