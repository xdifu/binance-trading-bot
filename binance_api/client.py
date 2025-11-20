import os
import logging
import time
from datetime import datetime, timedelta
from binance.spot import Spot
from binance.error import ClientError
import config
from utils.format_utils import format_price, format_quantity, get_precision_from_step_size

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
        self.api_secret = os.getenv("API_SECRET", getattr(config, "API_SECRET", None))
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
        self.can_sign_requests = False
        self._symbol_info_cache = {}  # Cache symbol info for precision formatting
        self.user_stream_subscription_id = None
        self.user_stream_mode = None  # "ws_api" or "listen_key"
        
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
            self.has_private_key = os.path.isfile(private_key_path)
            private_key_data = None
            if self.has_private_key:
                with open(private_key_path, 'rb') as f:
                    private_key_data = f.read()
            else:
                self.logger.warning(f"Private key file not found: {private_key_path} (will try HMAC/keyless mode)")
            self.has_hmac_secret = bool(self.api_secret)
            
            # Initialize REST client
            rest_kwargs = {
                "api_key": self.api_key,
                "base_url": base_url,
            }
            if self.has_private_key and private_key_data:
                rest_kwargs.update(
                    private_key=private_key_data,
                    private_key_pass=self.private_key_pass,
                )
                self.can_sign_requests = True
            elif self.has_hmac_secret:
                rest_kwargs.update(api_secret=self.api_secret)
                self.can_sign_requests = True
            else:
                # Public-only mode (suitable for simulations / dry runs that don't hit signed endpoints)
                self.logger.warning("No signing credentials found; REST client will operate in public-only mode (no trading or account calls).")
            
            self.rest_client = Spot(**rest_kwargs)
            
            # Synchronize time before making any API calls
            self._sync_time()
            
            # Verify connection is valid if we can sign requests
            if self.can_sign_requests:
                self.rest_client.account()
                self.logger.info("REST API client connected successfully (fallback ready)")
            else:
                self.logger.info("REST API client initialized for public endpoints only")
            
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
        """Synchronize local time with Binance server time with enhanced accuracy"""
        try:
            # Take multiple samples to improve accuracy
            samples = []
            for i in range(3):  # Take 3 samples
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
                        offset = server_time_ms - (request_start_ms + one_way_delay_ms)
                        
                        # Store sample with its RTT
                        samples.append((offset, rtt_ms))
                    
                    # Small delay between samples
                    time.sleep(0.5)
                except Exception as e:
                    self.logger.warning(f"Time sync sample {i+1} failed: {e}")
            
            if not samples:
                self.logger.error("All time synchronization samples failed")
                return False
            
            # Use the sample with the lowest RTT for best accuracy
            samples.sort(key=lambda x: x[1])  # Sort by RTT
            self.time_offset = samples[0][0]  # Use offset from lowest RTT sample
            
            # Log detailed information
            rtt_ms = samples[0][1]
            time_diff_sec = abs(self.time_offset) / 1000
            self.logger.info(f"Time sync with {self.base_url}: offset={self.time_offset:.2f}ms, RTT={rtt_ms:.2f}ms ({time_diff_sec:.2f}s)")
            
            # Record last sync time
            self.last_time_sync = int(time.time())
            
            # Check if time difference is significant
            if time_diff_sec > 1:
                self.logger.warning(f"Local time is {'ahead of' if self.time_offset < 0 else 'behind'} server by {time_diff_sec:.2f}s")
            
            return True
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
        ws_specific_kwargs = kwargs.pop('_ws_kwargs', None)
        rest_specific_kwargs = kwargs.pop('_rest_kwargs', None)

        base_kwargs = dict(kwargs)
        rest_kwargs = dict(base_kwargs)
        if rest_specific_kwargs:
            rest_kwargs.update(rest_specific_kwargs)

        if ws_specific_kwargs:
            ws_kwargs = dict(base_kwargs)
            ws_kwargs.update(ws_specific_kwargs)
        else:
            ws_kwargs = dict(rest_kwargs)

        # Periodically re-sync time
        current_time = int(time.time())
        if current_time - self.last_time_sync > self.time_sync_interval:
            self._sync_time()

        requires_signature = any(keyword in rest_method_name for keyword in ['account', 'order', 'oco', 'myTrades', 'openOrders'])

        if requires_signature and not getattr(self, "can_sign_requests", True):
            raise RuntimeError(
                f"{rest_method_name} requires signed credentials but no Ed25519 key or HMAC secret is configured. "
                "Provide PRIVATE_KEY or API_SECRET to enable trading/account calls."
            )

        # For methods that require signed requests, add adjusted timestamp
        if requires_signature:
            if 'timestamp' not in rest_kwargs:
                # Calculate adaptive safety offset based on current time offset
                # If already behind (negative offset), use smaller safety margin
                # If ahead (positive offset), use larger safety margin
                if self.time_offset < 0:
                    # Local time is ahead of server time - use full offset plus safety margin
                    # This ensures we compensate for the entire time difference plus extra
                    safety_offset = self.time_offset - 500  # Full offset plus 500ms safety margin
                else:
                    # Local time is behind server time, use standard safety offset
                    safety_offset = -500  # Standard 500ms behind

                timestamp_value = self._get_timestamp() + safety_offset
                rest_kwargs['timestamp'] = timestamp_value
                if 'timestamp' not in ws_kwargs:
                    ws_kwargs['timestamp'] = timestamp_value
                self.logger.debug(f"Added timestamp with adaptive safety offset {safety_offset}ms: {rest_kwargs['timestamp']} to {rest_method_name}")

        # Try WebSocket API first if available
        if self.websocket_available and self.ws_client:
            try:
                # Get the method from WebSocket client
                ws_method = getattr(self.ws_client, ws_method_name, None)
                if ws_method:
                    return ws_method(*args, **ws_kwargs)
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
                rest_method = getattr(self.rest_client, rest_method_name)
                self.logger.debug(f"Using REST API fallback for {rest_method_name}")
                return rest_method(*args, **rest_kwargs)
            except ClientError as e:
                # Enhanced handling for timestamp errors
                if hasattr(e, 'error_code') and e.error_code == -1021:
                    self.logger.warning(f"Timestamp error detected ({e}), performing emergency time sync")

                    # Force immediate time sync with multiple attempts
                    sync_success = False
                    for attempt in range(3):
                        if self._sync_time():
                            sync_success = True
                            self.logger.info(f"Emergency time sync successful on attempt {attempt+1}")
                            break
                        time.sleep(1)

                    if not sync_success:
                        self.logger.error("All emergency time sync attempts failed")
                        raise

                    # Retry the request with newly synced timestamp
                    if 'timestamp' in rest_kwargs:
                        # Use a conservative (far behind) timestamp to ensure success
                        adjusted_timestamp = self._get_timestamp() + (self.time_offset - 1000)  # Full offset plus 1000ms safety
                        rest_kwargs['timestamp'] = adjusted_timestamp
                        if 'timestamp' in ws_kwargs:
                            ws_kwargs['timestamp'] = adjusted_timestamp
                        self.logger.info(f"Retrying with adjusted timestamp: {rest_kwargs['timestamp']}")

                    # Retry the request
                    rest_method = getattr(self.rest_client, rest_method_name)
                    return rest_method(*args, **rest_kwargs)
                else:
                    # For other errors, just log and re-raise
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

    def _get_symbol_precisions(self, symbol):
        """
        Get price and quantity precision from exchange filters for a symbol.

        Returns (price_precision, quantity_precision)
        """
        price_precision = 8
        quantity_precision = 8
        try:
            if symbol in self._symbol_info_cache:
                symbol_info = self._symbol_info_cache[symbol]
            else:
                symbol_info = self.get_symbol_info(symbol)
                if symbol_info:
                    self._symbol_info_cache[symbol] = symbol_info

            if symbol_info and "filters" in symbol_info:
                for f in symbol_info["filters"]:
                    if f.get("filterType") == "PRICE_FILTER" and f.get("tickSize"):
                        price_precision = get_precision_from_step_size(f["tickSize"])
                    elif f.get("filterType") == "LOT_SIZE" and f.get("stepSize"):
                        quantity_precision = get_precision_from_step_size(f["stepSize"])
        except Exception as e:
            self.logger.debug(f"Failed to derive precisions for {symbol}: {e}")
        return price_precision, quantity_precision

    def _adjust_price_precision(self, price, symbol=None):
        """
        Format price with appropriate precision
        
        Args:
            price: Price value to format
            symbol: Optional symbol to derive tick size precision
            
        Returns:
            str: Formatted price string
        """
        precision = 8
        if symbol:
            precision, _ = self._get_symbol_precisions(symbol)
        return format_price(price, precision)

    def _adjust_quantity_precision(self, quantity, symbol=None):
        """
        Format quantity with appropriate precision
        
        Args:
            quantity: Quantity value to format
            symbol: Optional symbol to derive step size precision
            
        Returns:
            str: Formatted quantity string
        """
        precision = 8
        if symbol:
            _, precision = self._get_symbol_precisions(symbol)
        return format_quantity(quantity, precision)

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

    def get_order_book(self, symbol, limit=20):
        """Get order book depth for slippage estimation"""
        try:
            return self._execute_with_fallback(
                "depth",
                "depth",
                symbol=symbol,
                limit=limit
            )
        except Exception as e:
            self.logger.error(f"Failed to get order book for {symbol}: {e}")
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
                
            # Format price and quantity according to symbol filters
            formatted_price = self._adjust_price_precision(price_float, symbol)
            formatted_quantity = self._adjust_quantity_precision(quantity, symbol)
            
            # Ensure using string formats
            params = {
                'symbol': symbol,
                'side': side,
                'type': 'LIMIT',
                'timeInForce': 'GTC',
                'quantity': formatted_quantity,
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
            formatted_quantity = self._adjust_quantity_precision(quantity, symbol)
            params = {
                'symbol': symbol,
                'side': side,
                'type': 'MARKET',
                'quantity': formatted_quantity  # Ensure using string format
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
            formatted_stop_price = self._adjust_price_precision(stop_price_float, symbol)
            formatted_quantity = self._adjust_quantity_precision(quantity, symbol)
                
            params = {
                'symbol': symbol,
                'side': 'SELL',
                'type': 'STOP_LOSS',
                'quantity': formatted_quantity,
                'stopPrice': formatted_stop_price
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
            formatted_stop_price = self._adjust_price_precision(stop_price_float, symbol)
            formatted_quantity = self._adjust_quantity_precision(quantity, symbol)
                
            params = {
                'symbol': symbol,
                'side': 'SELL',
                'type': 'TAKE_PROFIT',
                'quantity': formatted_quantity,
                'stopPrice': formatted_stop_price
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
            price_precision, qty_precision = self._get_symbol_precisions(symbol)
            formatted_quantity = format_quantity(quantity, qty_precision)
            formatted_price = format_price(price, price_precision)
            formatted_stop = format_price(stopPrice, price_precision)

            base_params = {
                "symbol": symbol,
                "side": side,
                "quantity": formatted_quantity,
                "price": formatted_price,
                "stopPrice": formatted_stop
            }

            if stopLimitPrice:
                formatted_stop_limit = format_price(stopLimitPrice, price_precision)
                base_params["stopLimitPrice"] = formatted_stop_limit
                base_params["stopLimitTimeInForce"] = stopLimitTimeInForce

            rest_params = dict(base_params)
            ws_params = dict(base_params)
            ws_only_params = {}

            for key, value in kwargs.items():
                if key in ("aboveType", "belowType"):
                    ws_only_params[key] = value
                elif key not in rest_params:
                    rest_params[key] = value
                    ws_params[key] = value

            if aboveType is not None:
                ws_params["aboveType"] = aboveType
            elif "aboveType" in ws_only_params:
                ws_params["aboveType"] = ws_only_params["aboveType"]
            else:
                if side == "SELL":
                    ws_params.setdefault("aboveType", "LIMIT_MAKER")
                else:
                    ws_params.setdefault("aboveType", "STOP_LOSS")

            if belowType is not None:
                ws_params["belowType"] = belowType
            elif "belowType" in ws_only_params:
                ws_params["belowType"] = ws_only_params["belowType"]
            else:
                if side == "SELL":
                    ws_params.setdefault("belowType", "STOP_LOSS")
                else:
                    ws_params.setdefault("belowType", "LIMIT_MAKER")

            response = self._execute_with_fallback(
                "new_oco_order",
                "new_oco_order",
                _ws_kwargs=ws_params,
                **rest_params
            )
                
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

    def start_user_data_stream(self, on_event=None):
        """
        Prefer WS-API user data stream subscription; return True on success.
        Falls back to False so caller can decide whether to use REST/listenKey streams.
        """
        if self.websocket_available and self.ws_client:
            try:
                sub_id = self.ws_client.start_user_stream(on_event)
                if sub_id is not None:
                    self.user_stream_subscription_id = sub_id
                    self.user_stream_mode = "ws_api"
                    self.logger.info(f"Subscribed to user data stream via WS API (subscriptionId={sub_id})")
                    return True
                self.logger.warning("WS API user data stream subscription returned no subscriptionId")
            except Exception as e:
                self.logger.warning(f"WS API user data stream subscription failed: {e}")
                # mark unavailable only if connection issues
                if "connection" in str(e).lower() or "websocket" in str(e).lower():
                    self.websocket_available = False
        return False

    def stop_user_data_stream(self):
        """Stop user data stream if active via WS API."""
        if self.user_stream_mode == "ws_api" and self.ws_client and self.user_stream_subscription_id is not None:
            try:
                self.ws_client.stop_user_stream(self.user_stream_subscription_id)
            except Exception as e:
                self.logger.warning(f"Failed to unsubscribe user data stream: {e}")
            finally:
                self.user_stream_subscription_id = None
                self.user_stream_mode = None
    
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
    
    def _adjust_sync_interval(self):
        """Dynamically adjust time sync interval based on network conditions"""
        try:
            # Perform a quick RTT test to Binance
            import socket
            import time
            
            # Use TCP connection timing to measure network latency
            host = "api.binance.com"
            start = time.time()
            s = socket.create_connection((host, 443), timeout=5)
            s.close()
            rtt_ms = (time.time() - start) * 1000
            
            # Adjust interval based on RTT
            if rtt_ms > 300:  # High latency (Perth)
                interval = 60 * 5  # Sync every 5 minutes
                self.logger.info(f"High latency detected ({rtt_ms:.1f}ms), setting time sync interval to 5 minutes")
            else:  # Low latency (Tokyo)
                interval = 60 * 15  # Sync every 15 minutes
                self.logger.info(f"Low latency detected ({rtt_ms:.1f}ms), setting time sync interval to 15 minutes")
            
            self.time_sync_interval = interval
            return interval
        except Exception as e:
            # Default to 10 minutes if measurement fails
            self.logger.warning(f"Failed to measure network latency: {e}, using default interval")
            return 60 * 10  # Default 10 minute interval
