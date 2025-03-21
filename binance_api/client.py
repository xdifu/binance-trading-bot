import os
import logging
from datetime import datetime, timedelta
from binance.spot import Spot
from binance.error import ClientError
import config

class BinanceClient:
    def __init__(self):
        # Get API key from environment variables
        self.api_key = os.getenv("API_KEY", config.API_KEY)
        # Get private key file path
        private_key_path = os.getenv("PRIVATE_KEY", config.PRIVATE_KEY)
        self.private_key_pass = None if os.getenv("PRIVATE_KEY_PASS") == "None" else os.getenv("PRIVATE_KEY_PASS", 
                                getattr(config, 'PRIVATE_KEY_PASS', None))
        self.base_url = config.BASE_URL
        self.use_testnet = config.USE_TESTNET
        self.logger = logging.getLogger(__name__)
        
        # Initialize Binance API client
        try:
            # Read private key file content instead of path
            if os.path.isfile(private_key_path):
                with open(private_key_path, 'rb') as f:
                    private_key_data = f.read()
            else:
                self.logger.error(f"Private key file not found: {private_key_path}")
                raise FileNotFoundError(f"Private key file not found: {private_key_path}")
            
            # Set correct base_url
            base_url = "https://testnet.binance.vision" if self.use_testnet else self.base_url
            
            # Use correct initialization method
            self.client = Spot(
                api_key=self.api_key,
                base_url=base_url,
                private_key=private_key_data,
                private_key_pass=self.private_key_pass
            )
            
            # Verify connection is valid
            self.client.account()
            self.logger.info("Binance API connected successfully (using Ed25519 authentication)")
        except ClientError as e:
            self.logger.error(f"Binance API connection failed: {e}")
            
            # If API format error, try fallback with API_SECRET
            if hasattr(e, 'error_code') and e.error_code == -2014:
                self.logger.warning("API key format error, please ensure you're using correct ED25519 API keys")
                self.logger.warning("Please visit https://www.binance.com/en/support/faq/how-to-create-ed25519-keys-for-api-requests-on-binance-6b9a63f1e3384cf48a2eedb82767a69a")
            raise
            
    def get_exchange_info(self, symbol=None):
        """Get exchange information"""
        try:
            if symbol:
                return self.client.exchange_info(symbol=symbol)
            return self.client.exchange_info()
        except ClientError as e:
            self.logger.error(f"Failed to get exchange info: {e}")
            raise

    def get_symbol_info(self, symbol):
        """Get symbol information"""
        try:
            info = self.client.exchange_info(symbol=symbol)
            if info and "symbols" in info and len(info["symbols"]) > 0:
                return info["symbols"][0]
            return None
        except ClientError as e:
            self.logger.error(f"Failed to get symbol info: {e}")
            raise

    def get_account_info(self):
        """Get account information"""
        try:
            return self.client.account()
        except ClientError as e:
            self.logger.error(f"Failed to get account info: {e}")
            raise

    def get_symbol_price(self, symbol):
        """Get current price for symbol"""
        try:
            ticker = self.client.ticker_price(symbol=symbol)
            return float(ticker['price'])
        except ClientError as e:
            self.logger.error(f"Failed to get {symbol} price: {e}")
            raise
            
    def place_limit_order(self, symbol, side, quantity, price):
        """Place limit order"""
        try:
            # Ensure using string formats
            params = {
                'symbol': symbol,
                'side': side,
                'type': 'LIMIT',
                'timeInForce': 'GTC',
                'quantity': str(quantity),
                'price': str(price)
            }
            return self.client.new_order(**params)
        except ClientError as e:
            self.logger.error(f"Failed to place limit order: {e}")
            raise
            
    def place_market_order(self, symbol, side, quantity):
        """Place market order"""
        try:
            params = {
                'symbol': symbol,
                'side': side,
                'type': 'MARKET',
                'quantity': str(quantity)  # Ensure using string format
            }
            return self.client.new_order(**params)
        except ClientError as e:
            self.logger.error(f"Failed to place market order: {e}")
            raise
            
    def cancel_order(self, symbol, order_id):
        """Cancel order"""
        try:
            return self.client.cancel_order(symbol=symbol, orderId=order_id)
        except ClientError as e:
            self.logger.error(f"Failed to cancel order: {e}")
            raise
            
    def get_open_orders(self, symbol=None):
        """Get open orders"""
        try:
            if symbol:
                return self.client.get_open_orders(symbol=symbol)
            return self.client.get_open_orders()
        except ClientError as e:
            self.logger.error(f"Failed to get open orders: {e}")
            raise
            
    def get_historical_klines(self, symbol, interval, start_str=None, limit=500):
        """Get historical klines data"""
        try:
            return self.client.klines(
                symbol=symbol,
                interval=interval,
                startTime=start_str,
                limit=limit
            )
        except ClientError as e:
            self.logger.error(f"Failed to get klines data: {e}")
            raise

    def place_stop_loss_order(self, symbol, quantity, stop_price):
        """Place stop loss order"""
        try:
            params = {
                'symbol': symbol,
                'side': 'SELL',
                'type': 'STOP_LOSS',
                'quantity': quantity,
                'stopPrice': stop_price,
                'timeInForce': 'GTC'
            }
            return self.client.new_order(**params)
        except ClientError as e:
            self.logger.error(f"Failed to place stop loss order: {e}")
            raise
    
    def place_take_profit_order(self, symbol, quantity, stop_price):
        """Place take profit order"""
        try:
            params = {
                'symbol': symbol,
                'side': 'SELL',
                'type': 'TAKE_PROFIT',
                'quantity': quantity,
                'stopPrice': stop_price,
                'timeInForce': 'GTC'
            }
            return self.client.new_order(**params)
        except ClientError as e:
            self.logger.error(f"Failed to place take profit order: {e}")
            raise

    def new_oco_order(self, symbol, side, quantity, price, stopPrice, stopLimitPrice=None, stopLimitTimeInForce="GTC", **kwargs):
        """Create OCO (One-Cancels-the-Other) order"""
        try:
            params = {
                'symbol': symbol,
                'side': side,
                'quantity': str(quantity),
                'price': str(price),
                'stopPrice': str(stopPrice),
                'stopLimitTimeInForce': stopLimitTimeInForce
            }
            
            if stopLimitPrice:
                params['stopLimitPrice'] = str(stopLimitPrice)
                
            # Add other optional parameters
            params.update(kwargs)
            
            return self.client.new_oco_order(**params)
        except ClientError as e:
            self.logger.error(f"Failed to create OCO order: {e}")
            raise
            
    def check_balance(self, asset):
        """Check if balance is sufficient for an asset"""
        try:
            account = self.get_account_info()
            self.logger.debug(f"Looking for asset: {asset}")
            
            # Debug: Log all asset names and balances
            if 'balances' in account:
                for bal in account['balances']:
                    if float(bal.get('free', 0)) > 0:
                        self.logger.debug(f"Found asset: {bal.get('asset')}, free: {bal.get('free')}")
            
            # Case-insensitive search
            for balance in account['balances']:
                if balance['asset'].upper() == asset.upper():
                    free_balance = float(balance['free'])
                    self.logger.debug(f"Balance found for {asset}: {free_balance}")
                    return free_balance
            
            self.logger.warning(f"Asset {asset} not found in account balances")
            return 0.0
        except Exception as e:
            self.logger.error(f"Failed to check balance for {asset}: {e}")
            return 0.0