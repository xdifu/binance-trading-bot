import os
import logging
import time
from datetime import datetime, timedelta
from binance.spot import Spot
from binance.error import ClientError
import config
from utils.format_utils import format_price

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
        self.time_sync_interval = 60 * 10  # Sync time every 10 minutes (increased frequency)
        
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
        """Synchronize local time with Binance server time with RTT compensation"""
        try:
            # Record request start time
            request_start_ms = int(time.time() * 1000)
            
            # Get server time
            server_time = self.rest_client.time()
            
            # Record response received time
            request_end_ms = int(time.time() * 1000)
            
            if server_time and 'serverTime' in server_time:
                server_time_ms = server_time['serverTime']
                
                # Calculate RTT (Round Trip Time)
                rtt_ms = request_end_ms - request_start_ms
                
                # Estimate one-way network delay (assuming symmetric path)
                one_way_delay_ms = rtt_ms / 2
                
                # Calculate time offset with one-way delay compensation
                # server_time - (local_time_at_request + one_way_delay)
                self.time_offset = server_time_ms - (request_start_ms + one_way_delay_ms)
                
                # Log detailed information
                time_diff_sec = abs(self.time_offset) / 1000
                self.logger.info(f"Time sync with {self.base_url}: offset={self.time_offset:.2f}ms, RTT={rtt_ms:.2f}ms ({time_diff_sec:.2f}s)")
                
                # Record last sync time
                self.last_time_sync = int(time.time())
                
                # Check if time difference is significant
                if time_diff_sec > 1:
                    self.logger.warning(f"Local time is {'ahead of' if self.time_offset < 0 else 'behind'} server by {time_diff_sec:.2f}s")
                    
                    # Attempt system clock sync if running as root and time diff is large
                    if time_diff_sec > 1 and os.geteuid() == 0:
                        self.logger.info("Attempting to sync system clock with Binance server time")
                        # System clock synchronization code would go here
                
                return True
                
            return False
        except Exception as e:
            self.logger.error(f"Time synchronization failed: {e}")
            return False
    
    def _get_timestamp(self):
        """Get timestamp adjusted for server time offset"""
        current_time = int(time.time())
        if current_time - self.last_time_sync > self.time_sync_interval:
            self._sync_time()
        
        adjusted_time = int(time.time() * 1000) + self.time_offset
        self.logger.debug(f"Using adjusted timestamp: {adjusted_time} (offset: {self.time_offset}ms)")
        return adjusted_time
    
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
                # Calculate adaptive safety offset based on current time offset
                # If already behind (negative offset), use smaller safety margin
                # If ahead (positive offset), use larger safety margin
                if self.time_offset < 0:
                    # Already behind server time, use smaller safety offset
                    safety_offset = min(-100, int(self.time_offset / 10))  # At most -100ms or 10% of existing lag
                else:
                    # At or ahead of server time, use standard safety offset
                    safety_offset = -1000  # Standard 1 second behind

                kwargs['timestamp'] = self._get_timestamp() + safety_offset
                self.logger.debug(f"Added timestamp with adaptive safety offset {safety_offset}ms: {kwargs['timestamp']} to {rest_method_name}")
                
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
        Format price with appropriate precision
        
        Args:
            price: Price value to format
            
        Returns:
            str: Formatted price string
        """
        # 使用默认精度 8 作为安全值，因为客户端可能没有获取具体交易对的精度
        precision = 8
        return format_price(price, precision)

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
            # Validate and format price before sending
            try:
                price_float = float(price)
                if price_float <= 0:
                    self.logger.error(f"Invalid price value: {price} for {side} order")
                    raise ValueError(f"Invalid price value: {price}")
            except (ValueError, TypeError):
                self.logger.error(f"Non-numeric price value: {price} for {side} order")
                raise ValueError(f"Non-numeric price value: {price}")
                
            # Format price to ensure it's a valid value and proper string format
            formatted_price = self._adjust_price_precision(price_float)
            
            # Ensure using string formats
            params = {
                'symbol': symbol,
                'side': side,
                'type': 'LIMIT',
                'timeInForce': 'GTC',
                'quantity': str(quantity),
                'price': formatted_price  # Use formatted price
            }
            
            self.logger.debug(f"Placing {side} limit order: {quantity} @ {formatted_price}")
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
    
    # Add get_klines as alias for get_historical_klines for compatibility
    def get_klines(self, symbol, interval, start_str=None, limit=500):
        """
        Alias for get_historical_klines for compatibility with grid_trader
        
        Args:
            symbol: Trading pair symbol
            interval: Kline interval (e.g. 1h, 4h, 1d)
            start_str: Optional start time in milliseconds
            limit: Number of klines to return
            
        Returns:
            list: List of klines data
        """
        self.logger.debug("Using get_klines alias for get_historical_klines")
        return self.get_historical_klines(symbol, interval, start_str, limit)

    def place_stop_loss_order(self, symbol, quantity, stop_price):
        """Place stop loss order"""
        try:
            # Validate and format price
            try:
                stop_price_float = float(stop_price)
                if stop_price_float <= 0:
                    self.logger.error(f"Invalid stop price: {stop_price}")
                    raise ValueError(f"Invalid stop price: {stop_price}")
            except (ValueError, TypeError):
                self.logger.error(f"Non-numeric stop price: {stop_price}")
                raise ValueError(f"Non-numeric stop price: {stop_price}")
                
            # Format stop price
            formatted_stop_price = self._adjust_price_precision(stop_price_float)
                
            params = {
                'symbol': symbol,
                'side': 'SELL',
                'type': 'STOP_LOSS',
                'quantity': str(quantity),
                'stopPrice': formatted_stop_price,
                'timeInForce': 'GTC'
            }
            return self._execute_with_fallback("new_order", "new_order", **params)
        except Exception as e:
            self.logger.error(f"Failed to place stop loss order: {e}")
            raise
    
    def place_take_profit_order(self, symbol, quantity, stop_price):
        """Place take profit order"""
        try:
            # Validate and format price
            try:
                stop_price_float = float(stop_price)
                if stop_price_float <= 0:
                    self.logger.error(f"Invalid stop price: {stop_price}")
                    raise ValueError(f"Invalid stop price: {stop_price}")
            except (ValueError, TypeError):
                self.logger.error(f"Non-numeric stop price: {stop_price}")
                raise ValueError(f"Non-numeric stop price: {stop_price}")
                
            # Format stop price
            formatted_stop_price = self._adjust_price_precision(stop_price_float)
                
            params = {
                'symbol': symbol,
                'side': 'SELL',
                'type': 'TAKE_PROFIT',
                'quantity': str(quantity),
                'stopPrice': formatted_stop_price,
                'timeInForce': 'GTC'
            }
            return self._execute_with_fallback("new_order", "new_order", **params)
        except Exception as e:
            self.logger.error(f"Failed to place take profit order: {e}")
            raise

    def new_oco_order(self, symbol, side, quantity, price, stopPrice, stopLimitPrice=None, 
                     stopLimitTimeInForce="GTC", aboveType=None, belowType=None, **kwargs):
        """
        Create OCO (One-Cancels-the-Other) order
        
        Note: aboveType and belowType parameters are included for backwards compatibility
        but are no longer required by Binance API
        
        Args:
            symbol: Trading pair symbol
            side: Order side (BUY/SELL)
            quantity: Order quantity
            price: Limit order price
            stopPrice: Stop price
            stopLimitPrice: Stop limit price (optional)
            stopLimitTimeInForce: Time in force for stop limit order (default: GTC)
            aboveType: Above leg order type (optional)
            belowType: Below leg order type (optional)
            **kwargs: Additional parameters to pass to the API
        """
        try:
            # Create params dictionary with all parameters
            all_params = {
                "symbol": symbol,
                "side": side,
                "quantity": quantity,
                "price": price,
                "stopPrice": stopPrice
            }
            
            # Add optional parameters
            if stopLimitPrice:
                all_params["stopLimitPrice"] = stopLimitPrice
                all_params["stopLimitTimeInForce"] = stopLimitTimeInForce
            
            # Handle aboveType and belowType parameters (for REST API only)
            # These parameters are ignored by WebSocket API
            if aboveType is not None:
                all_params["aboveType"] = aboveType
            elif "aboveType" not in kwargs:
                all_params["aboveType"] = "LIMIT_MAKER"  # Default value
                
            if belowType is not None:
                all_params["belowType"] = belowType
            elif "belowType" not in kwargs:
                all_params["belowType"] = "LIMIT_MAKER"  # Default value
            
            # Merge other parameters
            for k, v in kwargs.items():
                if k not in all_params:
                    all_params[k] = v
            
            # Call WebSocket or REST API
            response = self._execute_with_fallback("new_oco_order", "new_oco_order", **all_params)
                
            # Check if response contains error information
            if isinstance(response, dict) and 'error' in response:
                error_code = response.get('error', {}).get('code')
                error_msg = response.get('error', {}).get('msg', 'Unknown error')
                self.logger.error(f"OCO order failed with error {error_code}: {error_msg}")
                # Return response with error info so caller knows order failed
                return {"result": None, "error": response.get('error'), "success": False}
            
            # If using WebSocket API, also check status field
            if isinstance(response, dict) and response.get('status', 200) != 200:
                error_code = response.get('status')
                error_msg = "Non-success status code"
                self.logger.error(f"OCO order failed with status {error_code}")
                return {"result": None, "error": {"code": error_code, "msg": error_msg}, "success": False}
                
            # Standardize response format and add success flag
            standardized_response = self._standardize_oco_response(response)
            if "result" in standardized_response and standardized_response["result"]:
                standardized_response["success"] = True
            else:
                standardized_response["success"] = False
                
            return standardized_response
        except Exception as e:
            self.logger.error(f"Failed to create OCO order: {e}")
            return {"result": None, "error": {"code": -1000, "msg": str(e)}, "success": False}
            
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
    
    def _standardize_oco_response(self, response):
        """Standardize OCO order response format to ensure dict-like access"""
        
        # Handle StandardizedMessage objects - convert them to dictionaries
        if hasattr(response, '__class__') and response.__class__.__name__ == 'StandardizedMessage':
            # Convert StandardizedMessage to dictionary
            response_dict = {}
            for attr in dir(response):
                # Skip private attributes and methods
                if not attr.startswith('_') and not callable(getattr(response, attr)):
                    value = getattr(response, attr)
                    # Recursively convert nested StandardizedMessage objects
                    if hasattr(value, '__class__') and value.__class__.__name__ == 'StandardizedMessage':
                        value = self._standardize_oco_response(value)
                    response_dict[attr] = value
            return {"result": response_dict}
        
        # If not a dict or StandardizedMessage, wrap in a dict with 'result' key
        if not isinstance(response, dict):
            return {"result": response}
            
        # If it's WebSocket API response format (contains result field)
        if 'result' in response and isinstance(response['result'], dict):
            return response
        
        # If it's REST API response format, wrap it
        return {"result": response}