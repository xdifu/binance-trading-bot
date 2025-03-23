import os
import logging
import time
from datetime import datetime, timedelta
from binance.spot import Spot
from binance.error import ClientError
import config

# Import the WebSocket API client
try:
    from .websocket_api_client import BinanceWSClient
    WEBSOCKET_API_AVAILABLE = True
except ImportError:
    WEBSOCKET_API_AVAILABLE = False

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
        
        # Flags to track client status
        self.websocket_available = False
        self.ws_client = None
        self.rest_client = None
        
        # Add time offset variable for server time synchronization
        self.time_offset = 0
        self.last_time_sync = 0
        self.time_sync_interval = 60 * 60  # Sync time every hour
        
        # Initialize WebSocket API client first (if available)
        if WEBSOCKET_API_AVAILABLE:
            try:
                self.logger.info("Initializing WebSocket API client...")
                self.ws_client = BinanceWSClient(
                    api_key=self.api_key,
                    private_key_path=private_key_path,
                    private_key_pass=self.private_key_pass,
                    use_testnet=self.use_testnet
                )
                
                # Test connection
                test_result = self.ws_client.client.ping_server()
                if test_result and test_result.get('status') == 200:
                    self.websocket_available = True
                    self.logger.info("WebSocket API client connected successfully")
                else:
                    self.logger.warning("WebSocket API client connection test failed")
            except Exception as e:
                self.logger.warning(f"Failed to initialize WebSocket API client: {e}")
                self.ws_client = None
        
        # Initialize REST client as fallback
        try:
            # Set correct base_url
            base_url = "https://testnet.binance.vision" if self.use_testnet else self.base_url
            
            # Read private key file content
            if os.path.isfile(private_key_path):
                with open(private_key_path, 'rb') as f:
                    private_key_data = f.read()
            else:
                self.logger.error(f"Private key file not found: {private_key_path}")
                raise FileNotFoundError(f"Private key file not found: {private_key_path}")
            
            # Initialize REST client
            self.rest_client = Spot(
                api_key=self.api_key,
                base_url=base_url,
                private_key=private_key_data,
                private_key_pass=self.private_key_pass
            )
            
            # Synchronize time before making any API calls
            self._sync_time()
            
            # Verify connection is valid
            self.rest_client.account()
            self.logger.info("REST API client connected successfully (fallback ready)")
            
            # If WebSocket client failed, log a warning
            if not self.websocket_available:
                self.logger.warning("Operating in REST API fallback mode only")
                
        except ClientError as e:
            self.logger.error(f"REST API connection failed: {e}")
            
            # If API format error, give guidance
            if hasattr(e, 'error_code') and e.error_code == -2014:
                self.logger.warning("API key format error, please ensure you're using correct ED25519 API keys")
                self.logger.warning("Please visit https://www.binance.com/en/support/faq/how-to-create-ed25519-keys-for-api-requests-on-binance-6b9a63f1e3384cf48a2eedb82767a69a")
            
            # If both clients failed, we have to raise an error
            if not self.websocket_available:
                self.logger.error("Both WebSocket API and REST API clients failed to initialize")
                raise
    
    def _sync_time(self):
        """
        Synchronize local time with Binance server time
        This helps prevent 'Timestamp for this request was 1000ms ahead of the server's time' errors
        """
        try:
            # Skip if we've synced recently
            current_time = int(time.time())
            if current_time - self.last_time_sync < self.time_sync_interval and self.last_time_sync > 0:
                return True
                
            # Get server time
            if self.rest_client:
                server_time = self.rest_client.time()
                if server_time and 'serverTime' in server_time:
                    server_time_ms = server_time['serverTime']
                    local_time_ms = int(time.time() * 1000)
                    self.time_offset = server_time_ms - local_time_ms
                    
                    # Log the time difference
                    time_diff_sec = abs(self.time_offset) / 1000
                    if time_diff_sec > 1:
                        self.logger.info(f"Local time is {'ahead of' if self.time_offset < 0 else 'behind'} server by {time_diff_sec:.2f} seconds. Offset applied: {self.time_offset}ms")
                    
                    self.last_time_sync = current_time
                    return True
            
            return False
        except Exception as e:
            self.logger.error(f"Time synchronization failed: {e}")
            return False
    
    def _get_timestamp(self):
        """
        Get timestamp adjusted for server time offset
        Returns: int - Server-adjusted timestamp in milliseconds
        """
        return int(time.time() * 1000) + self.time_offset
    
    def _execute_with_fallback(self, ws_method_name, rest_method_name, *args, **kwargs):
        """
        Execute a method with WebSocket API first, falling back to REST API if needed
        
        Args:
            ws_method_name: Method name to call on the WebSocket client
            rest_method_name: Method name to call on the REST client
            *args, **kwargs: Arguments to pass to both methods
        
        Returns:
            Result from either WebSocket or REST API
        """
        # Periodically re-sync time
        current_time = int(time.time())
        if current_time - self.last_time_sync > self.time_sync_interval:
            self._sync_time()
            
        # For methods that require signed requests, add adjusted timestamp
        if any(keyword in rest_method_name for keyword in ['account', 'order', 'oco', 'myTrades', 'openOrders']):
            if 'timestamp' not in kwargs:
                kwargs['timestamp'] = self._get_timestamp()
                self.logger.debug(f"Added adjusted timestamp: {kwargs['timestamp']} to {rest_method_name}")
                
        # Try WebSocket API first if available
        if self.websocket_available and self.ws_client:
            try:
                # Get the method from WebSocket client
                ws_method = getattr(self.ws_client, ws_method_name, None)
                if ws_method:
                    return ws_method(*args, **kwargs)
                else:
                    self.logger.debug(f"Method {ws_method_name} not found in WebSocket client")
            except Exception as e:
                self.logger.warning(f"WebSocket API call failed for {ws_method_name}: {e}")
                # If connection error, mark WebSocket as unavailable
                if "connection" in str(e).lower() or "websocket" in str(e).lower():
                    self.websocket_available = False
                    self.logger.warning("WebSocket API marked as unavailable, switching to REST fallback")
                
        # Fall back to REST API
        if self.rest_client:
            try:
                # For timestamp errors, try to re-sync and retry once
                try:
                    rest_method = getattr(self.rest_client, rest_method_name)
                    self.logger.debug(f"Using REST API fallback for {rest_method_name}")
                    return rest_method(*args, **kwargs)
                except ClientError as e:
                    # Check for timestamp error and retry once with fresh sync
                    if hasattr(e, 'error_code') and e.error_code == -1021:
                        self.logger.warning("Timestamp error detected, re-syncing time and retrying...")
                        self._sync_time()
                        
                        # Update timestamp in kwargs if it was a signed request
                        if 'timestamp' in kwargs:
                            kwargs['timestamp'] = self._get_timestamp()
                            
                        # Retry with updated timestamp
                        rest_method = getattr(self.rest_client, rest_method_name)
                        return rest_method(*args, **kwargs)
                    else:
                        # If not a timestamp error, re-raise
                        raise
            except Exception as e:
                self.logger.error(f"REST API fallback call failed for {rest_method_name}: {e}")
                raise
        else:
            raise RuntimeError("Neither WebSocket nor REST API client is available")
    
    def _retry_websocket_connection(self):
        """Try to reconnect to WebSocket API if it was previously unavailable"""
        if not self.websocket_available and self.ws_client:
            try:
                test_result = self.ws_client.client.ping_server()
                if test_result and test_result.get('status') == 200:
                    self.websocket_available = True
                    self.logger.info("WebSocket API connection restored")
                    return True
            except Exception:
                pass
        return False

    def _adjust_price_precision(self, price):
        """
        Format price to appropriate precision and validate it's not zero
        
        Args:
            price: Price value to format
            
        Returns:
            str: Formatted price string with appropriate precision
        """
        # Validate price is greater than 0
        if price <= 0:
            self.logger.warning(f"Attempted to format invalid price: {price}, using minimum price")
            # Use a small valid price as fallback
            price = 0.00000001
        
        # Format with 8 decimal places as a safe default
        # In production, this should use symbol-specific precision rules
        formatted_price = "{:.8f}".format(price)
        
        # Remove trailing zeros and return
        return formatted_price.rstrip('0').rstrip('.') if '.' in formatted_price else formatted_price

    def get_exchange_info(self, symbol=None):
        """Get exchange information"""
        # Periodically retry WebSocket connection if it's down
        if not self.websocket_available and (int(time.time()) % 60 == 0):
            self._retry_websocket_connection()
            
        try:
            if symbol:
                return self._execute_with_fallback("exchange_info", "exchange_info", symbol=symbol)
            return self._execute_with_fallback("exchange_info", "exchange_info")
        except Exception as e:
            self.logger.error(f"Failed to get exchange info: {e}")
            raise

    def get_symbol_info(self, symbol):
        """Get symbol information"""
        try:
            info = self.get_exchange_info(symbol=symbol)
            if info and "symbols" in info and len(info["symbols"]) > 0:
                return info["symbols"][0]
            return None
        except Exception as e:
            self.logger.error(f"Failed to get symbol info: {e}")
            raise

    def get_account_info(self):
        """Get account information"""
        try:
            return self._execute_with_fallback("account", "account")
        except Exception as e:
            self.logger.error(f"Failed to get account info: {e}")
            raise

    def get_symbol_price(self, symbol):
        """Get current price for symbol"""
        try:
            ticker = self._execute_with_fallback("ticker_price", "ticker_price", symbol=symbol)
            return float(ticker['price'] if isinstance(ticker, dict) else ticker['result']['price'])
        except Exception as e:
            self.logger.error(f"Failed to get {symbol} price: {e}")
            raise
            
    def place_limit_order(self, symbol, side, quantity, price):
        """Place limit order"""
        try:
            # Validate price before sending
            if float(price) <= 0:
                self.logger.error(f"Invalid price value: {price} for {side} order")
                raise ValueError(f"Invalid price value: {price}")
                
            # Ensure using string formats
            params = {
                'symbol': symbol,
                'side': side,
                'type': 'LIMIT',
                'timeInForce': 'GTC',
                'quantity': str(quantity),
                'price': str(price)
            }
            return self._execute_with_fallback("new_order", "new_order", **params)
        except Exception as e:
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
            return self._execute_with_fallback("new_order", "new_order", **params)
        except Exception as e:
            self.logger.error(f"Failed to place market order: {e}")
            raise
            
    def cancel_order(self, symbol, order_id):
        """Cancel order"""
        try:
            return self._execute_with_fallback("cancel_order", "cancel_order", symbol=symbol, orderId=order_id)
        except Exception as e:
            self.logger.error(f"Failed to cancel order: {e}")
            raise
            
    def get_open_orders(self, symbol=None):
        """Get open orders"""
        try:
            if symbol:
                return self._execute_with_fallback("get_open_orders", "get_open_orders", symbol=symbol)
            return self._execute_with_fallback("get_open_orders", "get_open_orders")
        except Exception as e:
            self.logger.error(f"Failed to get open orders: {e}")
            raise
            
    def get_historical_klines(self, symbol, interval, start_str=None, limit=500):
        """Get historical klines data"""
        try:
            return self._execute_with_fallback("klines", "klines",
                symbol=symbol,
                interval=interval,
                startTime=start_str,
                limit=limit
            )
        except Exception as e:
            self.logger.error(f"Failed to get klines data: {e}")
            raise

    def place_stop_loss_order(self, symbol, quantity, stop_price):
        """Place stop loss order"""
        try:
            # Validate price
            if float(stop_price) <= 0:
                self.logger.error(f"Invalid stop price: {stop_price}")
                raise ValueError(f"Invalid stop price: {stop_price}")
                
            params = {
                'symbol': symbol,
                'side': 'SELL',
                'type': 'STOP_LOSS',
                'quantity': quantity,
                'stopPrice': stop_price,
                'timeInForce': 'GTC'
            }
            return self._execute_with_fallback("new_order", "new_order", **params)
        except Exception as e:
            self.logger.error(f"Failed to place stop loss order: {e}")
            raise
    
    def place_take_profit_order(self, symbol, quantity, stop_price):
        """Place take profit order"""
        try:
            # Validate price
            if float(stop_price) <= 0:
                self.logger.error(f"Invalid stop price: {stop_price}")
                raise ValueError(f"Invalid stop price: {stop_price}")
                
            params = {
                'symbol': symbol,
                'side': 'SELL',
                'type': 'TAKE_PROFIT',
                'quantity': quantity,
                'stopPrice': stop_price,
                'timeInForce': 'GTC'
            }
            return self._execute_with_fallback("new_order", "new_order", **params)
        except Exception as e:
            self.logger.error(f"Failed to place take profit order: {e}")
            raise

    def new_oco_order(self, symbol, side, quantity, price, stopPrice, stopLimitPrice=None, stopLimitTimeInForce="GTC", **kwargs):
        """Create OCO (One-Cancels-the-Other) order"""
        try:
            # Validate prices
            if float(price) <= 0 or float(stopPrice) <= 0:
                self.logger.error(f"Invalid prices for OCO order: price={price}, stopPrice={stopPrice}")
                raise ValueError(f"Invalid prices for OCO order")
                
            # Build base parameters (removing the incorrect aboveType and belowType)
            params = {
                'symbol': symbol,
                'side': side,
                'quantity': str(quantity),
                'price': str(price),
                'stopPrice': str(stopPrice),
                'stopLimitTimeInForce': stopLimitTimeInForce
            }
            
            # Add stopLimitPrice if provided or calculate based on stopPrice
            if stopLimitPrice:
                if float(stopLimitPrice) <= 0:
                    self.logger.warning(f"Invalid stopLimitPrice: {stopLimitPrice}, calculating based on stopPrice")
                    stop_limit_price = float(stopPrice) * 0.99  # 1% below stop price
                    params['stopLimitPrice'] = self._adjust_price_precision(stop_limit_price)
                else:
                    params['stopLimitPrice'] = str(stopLimitPrice)
            else:
                # If not provided, use a default value slightly below stopPrice
                stop_limit_price = float(stopPrice) * 0.99  # 1% below stop price
                params['stopLimitPrice'] = self._adjust_price_precision(stop_limit_price)
                
            # Add other optional parameters
            params.update(kwargs)
            
            # Log the parameters for debugging
            self.logger.debug(f"Sending OCO order with parameters: {params}")
            
            return self._execute_with_fallback("new_oco_order", "new_oco_order", **params)
        except Exception as e:
            self.logger.error(f"Failed to create OCO order: {e}")
            raise
            
    def check_balance(self, asset):
        """Check if balance is sufficient for an asset"""
        try:
            account = self.get_account_info()
            self.logger.debug(f"Looking for asset: {asset}")
            
            # Handle different response formats between WS and REST
            balances = account.get('balances') if 'balances' in account else account.get('result', {}).get('balances', [])
            
            # Debug: Log all asset names and balances
            for bal in balances:
                if float(bal.get('free', 0)) > 0:
                    self.logger.debug(f"Found asset: {bal.get('asset')}, free: {bal.get('free')}")
            
            # Case-insensitive search
            for balance in balances:
                if balance['asset'].upper() == asset.upper():
                    free_balance = float(balance['free'])
                    self.logger.debug(f"Balance found for {asset}: {free_balance}")
                    return free_balance
            
            self.logger.warning(f"Asset {asset} not found in account balances")
            return 0.0
        except Exception as e:
            self.logger.error(f"Failed to check balance for {asset}: {e}")
            return 0.0

    # Additional utility methods
    
    def get_client_status(self):
        """Get the status of both WebSocket and REST clients"""
        return {
            "websocket_available": self.websocket_available,
            "rest_available": self.rest_client is not None
        }
    
    def force_websocket_reconnect(self):
        """Force reconnection to WebSocket API"""
        if self.ws_client:
            try:
                # Close current connection if any
                if hasattr(self.ws_client.client, "close"):
                    self.ws_client.client.close()
                
                # Reinitialize the client
                self.ws_client = BinanceWSClient(
                    api_key=self.api_key,
                    private_key_path=os.getenv("PRIVATE_KEY", config.PRIVATE_KEY),
                    private_key_pass=self.private_key_pass,
                    use_testnet=self.use_testnet
                )
                
                # Test connection
                test_result = self.ws_client.client.ping_server()
                if test_result and test_result.get('status') == 200:
                    self.websocket_available = True
                    self.logger.info("WebSocket API reconnected successfully")
                    return True
            except Exception as e:
                self.logger.error(f"Failed to reconnect to WebSocket API: {e}")
        
        self.websocket_available = False
        return False
        
    def manual_time_sync(self):
        """
        Manually trigger time synchronization
        Returns: bool - Whether the synchronization was successful
        """
        return self._sync_time()