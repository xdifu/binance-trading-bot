import os
import json
import time
import uuid
import hmac
import base64
import logging
import threading
from queue import Queue
from hashlib import sha256
from typing import Optional, Dict, Any, Callable, List, Union
import websocket
from cryptography.hazmat.primitives.serialization import load_pem_private_key

class BinanceWebSocketAPIClient:
    """
    Binance WebSocket API Client
    
    This client implements the WebSocket API functionality for Binance trading operations.
    It provides a more efficient alternative to REST API calls, with lower latency and
    reduced connection overhead.
    """
    
    def __init__(
        self, 
        api_key: str = None, 
        api_secret: str = None,
        private_key_path: str = None,
        private_key_pass: str = None,
        use_testnet: bool = False,
        auto_reconnect: bool = True,
        ping_interval: int = 20,
        timeout: int = 10
    ):
        """
        Initialize the WebSocket API client
        
        Args:
            api_key: Binance API key
            api_secret: Binance API secret (for HMAC authentication)
            private_key_path: Path to private key file (for RSA/Ed25519 authentication)
            private_key_pass: Password for private key file
            use_testnet: Whether to use testnet
            auto_reconnect: Whether to automatically reconnect when connection is lost
            ping_interval: Interval in seconds to send ping frames
            timeout: Timeout for requests in seconds
        """
        self.logger = logging.getLogger(__name__)
        
        # Authentication details
        self.api_key = api_key
        self.api_secret = api_secret
        self.private_key_path = private_key_path
        self.private_key_pass = private_key_pass
        self.private_key = None
        
        # Load private key if provided
        if private_key_path and os.path.isfile(private_key_path):
            try:
                with open(private_key_path, 'rb') as f:
                    private_key_data = f.read()
                    if private_key_pass is not None:
                        self.private_key = load_pem_private_key(
                            private_key_data, 
                            password=private_key_pass.encode('utf-8') if private_key_pass else None
                        )
                    else:
                        self.private_key = load_pem_private_key(
                            private_key_data,
                            password=None
                        )
            except Exception as e:
                self.logger.error(f"Failed to load private key: {e}")
                raise
        
        # Connection details
        self.ws_base_url = "wss://testnet.binance.vision/ws-api/v3" if use_testnet else "wss://ws-api.binance.com/ws-api/v3"
        self.timeout = timeout
        self.ping_interval = ping_interval
        self.auto_reconnect = auto_reconnect
        
        # Response handling
        self.ws = None
        self.ws_connected = False
        self.ws_authenticated = False
        self.last_received_time = 0
        
        # Request-response mapping
        self.request_callbacks = {}
        self.response_queue = Queue()
        self.lock = threading.Lock()
        
        # Background threads
        self.listen_thread = None
        self.ping_thread = None
        self._running = False
        self.is_closed_by_user = False
        
        # Session status
        self.session_initialized = False
        self.session_id = None
        
        # Connect to WebSocket
        self.connect()
    
    def connect(self) -> bool:
        """
        Establish connection to WebSocket API
        
        Returns:
            bool: True if connection successful, False otherwise
        """
        if self.ws_connected:
            return True
        
        self.is_closed_by_user = False
        
        try:
            self.logger.info(f"Connecting to {self.ws_base_url}...")
            self.ws = websocket.create_connection(
                self.ws_base_url,
                timeout=self.timeout
            )
            self.ws_connected = True
            self.last_received_time = time.time()
            
            self._running = True
            
            # Start listener thread
            self.listen_thread = threading.Thread(target=self._listen_forever)
            self.listen_thread.daemon = True
            self.listen_thread.start()
            
            # Start ping thread
            self.ping_thread = threading.Thread(target=self._ping_forever)
            self.ping_thread.daemon = True
            self.ping_thread.start()
            
            # Test connection
            self.ping_server()
            self.logger.info("Connected to Binance WebSocket API")
            
            return True
        except Exception as e:
            self.logger.error(f"Failed to connect to WebSocket API: {e}")
            self.ws_connected = False
            return False
    
    def _listen_forever(self):
        """Background thread that listens for incoming messages and handles them"""
        while self._running and self.ws_connected:
            try:
                message = self.ws.recv()
                self.last_received_time = time.time()
                
                if not message:
                    continue
                
                self._handle_message(message)
            except websocket.WebSocketConnectionClosedException:
                if not self.is_closed_by_user:
                    self.logger.error("WebSocket connection closed unexpectedly")
                    self._handle_disconnect()
                break
            except Exception as e:
                self.logger.error(f"Error while listening for messages: {e}")
                if not self.is_closed_by_user:
                    self._handle_disconnect()
                break
    
    def _ping_forever(self):
        """Background thread that sends ping frames to keep the connection alive"""
        while self._running and self.ws_connected:
            try:
                time.sleep(self.ping_interval)
                if self.ws_connected:
                    self.ws.ping()
            except Exception as e:
                self.logger.error(f"Error sending ping: {e}")
    
    def _handle_disconnect(self):
        """Handle unexpected disconnections"""
        self.logger.warning("Connection lost, cleaning up...")
        self.ws_connected = False
        self.ws_authenticated = False
        self.session_initialized = False
        
        if self.ws:
            try:
                self.ws.close()
            except:
                pass
        
        # Auto-reconnect if enabled
        if self.auto_reconnect and not self.is_closed_by_user:
            self.logger.info("Attempting to reconnect...")
            
            # Exponential backoff for reconnection attempts
            attempts = 0
            max_attempts = 5
            while attempts < max_attempts and not self.ws_connected:
                wait_time = min(60, (2 ** attempts))
                self.logger.info(f"Reconnecting in {wait_time} seconds (attempt {attempts+1}/{max_attempts})...")
                time.sleep(wait_time)
                
                if self.connect():
                    self.logger.info("Reconnected successfully")
                    break
                
                attempts += 1
                
            if not self.ws_connected:
                self.logger.error(f"Failed to reconnect after {max_attempts} attempts")
    
    def close(self):
        """Close the WebSocket connection gracefully"""
        self.logger.info("Closing WebSocket connection...")
        self.is_closed_by_user = True
        self._running = False
        
        if self.ws:
            try:
                self.ws.close()
            except:
                pass
        
        self.ws_connected = False
        self.ws_authenticated = False
        self.session_initialized = False
        self.logger.info("WebSocket connection closed")
        
    def _generate_request_id(self) -> str:
        """Generate a unique request ID"""
        return str(uuid.uuid4())
    
    def _handle_message(self, message: str):
        """
        Process incoming message from WebSocket
        
        Args:
            message: Raw message string from WebSocket
        """
        try:
            # Parse message
            data = json.loads(message)
            request_id = data.get('id')
            
            # Handle ping/pong frames
            if 'method' in data and data['method'] == 'pong':
                return
            
            # Log error responses
            if 'error' in data:
                error_code = data['error'].get('code')
                error_msg = data['error'].get('msg')
                self.logger.error(f"Error {error_code}: {error_msg}")
            
            # Handle responses to requests
            if request_id and request_id in self.request_callbacks:
                callback = self.request_callbacks.pop(request_id)
                if callback:
                    callback(data)
                self.response_queue.put(data)
            # Handle event notifications (e.g. account updates)
            elif 'event' in data:
                # Process event data
                # This could be handled by registering callbacks for specific event types
                self.logger.debug(f"Received event: {data['event']}")
            else:
                self.logger.debug(f"Unhandled message: {message}")
                
        except json.JSONDecodeError:
            self.logger.error(f"Failed to parse message as JSON: {message}")
        except Exception as e:
            self.logger.error(f"Error handling message: {e}")
    
    def _send_request(
        self, 
        method: str, 
        params: Optional[Dict] = None, 
        callback: Optional[Callable] = None
    ) -> str:
        """
        Send a request to the WebSocket API
        
        Args:
            method: API method name
            params: Request parameters
            callback: Optional callback function for handling the response
            
        Returns:
            str: Request ID
        """
        if not self.ws_connected:
            if not self.connect():
                raise ConnectionError("Failed to connect to WebSocket API")
        
        request_id = self._generate_request_id()
        request = {
            "id": request_id,
            "method": method
        }
        
        if params:
            request["params"] = params
        
        # Register callback for response
        if callback:
            self.request_callbacks[request_id] = callback
        
        # Send request
        self.logger.debug(f"Sending request: {request}")
        with self.lock:
            self.ws.send(json.dumps(request))
        
        return request_id
    
    def _send_signed_request(
        self, 
        method: str, 
        params: Optional[Dict] = None, 
        callback: Optional[Callable] = None
    ) -> str:
        """
        Send a signed request to the WebSocket API
        
        Args:
            method: API method name
            params: Request parameters
            callback: Optional callback function for handling the response
            
        Returns:
            str: Request ID
        """
        if not params:
            params = {}
        
        # Add timestamp for signature
        params['timestamp'] = int(time.time() * 1000)
        
        # Add API key
        params['apiKey'] = self.api_key
        
        # Generate signature
        params['signature'] = self._generate_signature(params)
        
        return self._send_request(method, params, callback)
    
    def _generate_signature(self, params: Dict) -> str:
        """
        Generate signature for API request
        
        Args:
            params: Request parameters
            
        Returns:
            str: Signature
        """
        # Sort parameters by key name
        sorted_params = sorted(params.items())
        
        # Convert to query string format
        query_string = '&'.join([f"{k}={v}" for k, v in sorted_params])
        
        # Sign with appropriate method
        if self.private_key:
            # Try Ed25519 first, then RSA if that fails
            try:
                # Ed25519 signature
                signature = self.private_key.sign(query_string.encode('utf-8'))
                return base64.b64encode(signature).decode('utf-8')
            except ValueError:
                # RSA signature
                from cryptography.hazmat.primitives import hashes
                from cryptography.hazmat.primitives.asymmetric import padding
                
                signature = self.private_key.sign(
                    query_string.encode('utf-8'),
                    padding.PKCS1v15(),
                    hashes.SHA256()
                )
                return base64.b64encode(signature).decode('utf-8')
        elif self.api_secret:
            # HMAC signature
            signature = hmac.new(
                self.api_secret.encode('utf-8'),
                query_string.encode('utf-8'),
                sha256
            ).hexdigest()
            return signature
        else:
            raise ValueError("No authentication method available")
    
    def _wait_for_response(self, request_id: str, timeout: int = None) -> Dict:
        """
        Wait for response to a specific request
        
        Args:
            request_id: Request ID to wait for
            timeout: Maximum time to wait in seconds
            
        Returns:
            Dict: Response data
        """
        if timeout is None:
            timeout = self.timeout
        
        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                response = self.response_queue.get(timeout=0.1)
                if response.get('id') == request_id:
                    return response
                else:
                    # Put it back if it's not the one we're waiting for
                    self.response_queue.put(response)
            except:
                pass
        
        raise TimeoutError(f"Timed out waiting for response to request {request_id}")

    # ===== General API Methods =====
    
    def ping_server(self) -> Dict:
        """
        Test connectivity to the WebSocket API
        
        Returns:
            Dict: Response from server
        """
        request_id = self._send_request("ping")
        return self._wait_for_response(request_id)
    
    def get_server_time(self) -> Dict:
        """
        Get current server time
        
        Returns:
            Dict: Response from server with current time
        """
        request_id = self._send_request("time")
        return self._wait_for_response(request_id)
    
    def get_exchange_info(self, **kwargs) -> Dict:
        """
        Get exchange trading rules and symbol information
        
        Keyword Args:
            symbol (str): Symbol to get info for
            symbols (list): List of symbols to get info for
            permissions (list): Permissions to filter by
            
        Returns:
            Dict: Exchange information
        """
        request_id = self._send_request("exchangeInfo", kwargs)
        return self._wait_for_response(request_id)
    
    # ===== Market Data Methods =====
    
    def get_order_book(self, symbol: str, limit: int = 100) -> Dict:
        """
        Get order book for a symbol
        
        Args:
            symbol: Symbol to get order book for
            limit: Depth of order book (default: 100, max: 5000)
            
        Returns:
            Dict: Order book data
        """
        params = {"symbol": symbol}
        if limit != 100:
            params["limit"] = limit
            
        request_id = self._send_request("depth", params)
        return self._wait_for_response(request_id)
    
    def get_recent_trades(self, symbol: str, limit: int = 500) -> Dict:
        """
        Get recent trades for a symbol
        
        Args:
            symbol: Symbol to get trades for
            limit: Number of trades to get (default: 500, max: 1000)
            
        Returns:
            Dict: Recent trades data
        """
        params = {"symbol": symbol}
        if limit != 500:
            params["limit"] = limit
            
        request_id = self._send_request("trades.recent", params)
        return self._wait_for_response(request_id)
    
    def get_klines(self, symbol: str, interval: str, **kwargs) -> Dict:
        """
        Get klines/candlestick data for a symbol
        
        Args:
            symbol: Symbol to get klines for
            interval: Kline interval (e.g., 1m, 1h, 1d)
            
        Keyword Args:
            startTime (int): Start time in milliseconds
            endTime (int): End time in milliseconds
            limit (int): Number of klines to get (default: 500, max: 1000)
            
        Returns:
            Dict: Klines data
        """
        params = {"symbol": symbol, "interval": interval}
        params.update(kwargs)
            
        request_id = self._send_request("klines", params)
        return self._wait_for_response(request_id)
    
    def get_ticker_price(self, symbol: str = None) -> Dict:
        """
        Get price ticker for a symbol or all symbols
        
        Args:
            symbol: Symbol to get ticker for (None for all symbols)
            
        Returns:
            Dict: Ticker price data
        """
        params = {}
        if symbol:
            params["symbol"] = symbol
            
        request_id = self._send_request("ticker.price", params)
        return self._wait_for_response(request_id)
    
    def get_ticker_24hr(self, symbol: str = None) -> Dict:
        """
        Get 24-hour price change statistics
        
        Args:
            symbol: Symbol to get statistics for (None for all symbols)
            
        Returns:
            Dict: 24-hour statistics
        """
        params = {}
        if symbol:
            params["symbol"] = symbol
            
        request_id = self._send_request("ticker.24hr", params)
        return self._wait_for_response(request_id)
    
    # ===== Trading Methods =====
    
    def place_order(self, symbol: str, side: str, order_type: str, **kwargs) -> Dict:
        """
        Place a new order
        
        Args:
            symbol: Trading symbol
            side: Order side (BUY or SELL)
            order_type: Order type (LIMIT, MARKET, etc.)
            
        Keyword Args:
            timeInForce (str): Time in force (GTC, IOC, FOK)
            quantity (str): Order quantity
            quoteOrderQty (str): Quote order quantity
            price (str): Order price
            newClientOrderId (str): Client order ID
            stopPrice (str): Stop price
            icebergQty (str): Iceberg quantity
            
        Returns:
            Dict: Order response
        """
        params = {
            "symbol": symbol,
            "side": side,
            "type": order_type
        }
        params.update(kwargs)
        
        request_id = self._send_signed_request("order.place", params)
        return self._wait_for_response(request_id)
    
    def test_order(self, symbol: str, side: str, order_type: str, **kwargs) -> Dict:
        """
        Test new order (validates parameters without placing order)
        
        Args:
            symbol: Trading symbol
            side: Order side (BUY or SELL)
            order_type: Order type (LIMIT, MARKET, etc.)
            
        Keyword Args:
            Same as place_order
            
        Returns:
            Dict: Validation response
        """
        params = {
            "symbol": symbol,
            "side": side,
            "type": order_type
        }
        params.update(kwargs)
        
        request_id = self._send_signed_request("order.test", params)
        return self._wait_for_response(request_id)
    
    def get_order(self, symbol: str, **kwargs) -> Dict:
        """
        Get order details
        
        Args:
            symbol: Trading symbol
            
        Keyword Args:
            orderId (int): Order ID
            origClientOrderId (str): Original client order ID
            
        Returns:
            Dict: Order details
        """
        params = {"symbol": symbol}
        params.update(kwargs)
        
        request_id = self._send_signed_request("order.status", params)
        return self._wait_for_response(request_id)
    
    def cancel_order(self, symbol: str, **kwargs) -> Dict:
        """
        Cancel an active order
        
        Args:
            symbol: Trading symbol
            
        Keyword Args:
            orderId (int): Order ID
            origClientOrderId (str): Original client order ID
            newClientOrderId (str): New client order ID for the cancel
            
        Returns:
            Dict: Cancellation response
        """
        params = {"symbol": symbol}
        params.update(kwargs)
        
        request_id = self._send_signed_request("order.cancel", params)
        return self._wait_for_response(request_id)
    
    def cancel_replace_order(
        self, 
        symbol: str, 
        cancel_replace_mode: str,
        side: str,
        order_type: str,
        **kwargs
    ) -> Dict:
        """
        Cancel an order and place a new one atomically
        
        Args:
            symbol: Trading symbol
            cancel_replace_mode: Mode (STOP_ON_FAILURE or ALLOW_FAILURE)
            side: Order side (BUY or SELL)
            order_type: Order type (LIMIT, MARKET, etc.)
            
        Keyword Args:
            cancelOrderId (int): Order ID to cancel
            cancelOrigClientOrderId (str): Original client order ID to cancel
            timeInForce (str): Time in force for new order
            quantity (str): Order quantity for new order
            price (str): Order price for new order
            
        Returns:
            Dict: Cancel-replace response
        """
        params = {
            "symbol": symbol,
            "cancelReplaceMode": cancel_replace_mode,
            "side": side,
            "type": order_type
        }
        params.update(kwargs)
        
        request_id = self._send_signed_request("order.cancelReplace", params)
        return self._wait_for_response(request_id)
    
    def get_open_orders(self, symbol: str = None) -> Dict:
        """
        Get open orders
        
        Args:
            symbol: Symbol to get open orders for (None for all symbols)
            
        Returns:
            Dict: Open orders
        """
        params = {}
        if symbol:
            params["symbol"] = symbol
        
        request_id = self._send_signed_request("openOrders.status", params)
        return self._wait_for_response(request_id)
    
    def cancel_open_orders(self, symbol: str) -> Dict:
        """
        Cancel all open orders on a symbol
        
        Args:
            symbol: Trading symbol
            
        Returns:
            Dict: Cancellation response
        """
        params = {"symbol": symbol}
        
        request_id = self._send_signed_request("openOrders.cancelAll", params)
        return self._wait_for_response(request_id)
    
    def place_oco_order(
        self,
        symbol: str,
        side: str,
        quantity: str,
        price: str,
        stop_price: str,
        **kwargs
    ) -> Dict:
        """
        Place OCO (One Cancels the Other) order
        
        Args:
            symbol: Trading symbol
            side: Order side (BUY or SELL)
            quantity: Order quantity
            price: Limit order price
            stop_price: Stop order price
            
        Keyword Args:
            stopLimitPrice (str): Stop limit price
            stopLimitTimeInForce (str): Time in force for stop limit leg
            
        Returns:
            Dict: OCO order response
        """
        params = {
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "price": price,
            "stopPrice": stop_price
        }
        params.update(kwargs)
        
        request_id = self._send_signed_request("orderList.place", params)
        return self._wait_for_response(request_id)
    
    # ===== Account Methods =====
    
    def get_account_info(self) -> Dict:
        """
        Get account information
        
        Returns:
            Dict: Account information
        """
        request_id = self._send_signed_request("account.status")
        return self._wait_for_response(request_id)
    
    def get_account_trades(self, symbol: str, **kwargs) -> Dict:
        """
        Get account's trade history
        
        Args:
            symbol: Trading symbol
            
        Keyword Args:
            fromId (int): Trade ID to fetch from
            startTime (int): Start time in milliseconds
            endTime (int): End time in milliseconds
            limit (int): Number of trades to get (default: 500, max: 1000)
            
        Returns:
            Dict: Trade history
        """
        params = {"symbol": symbol}
        params.update(kwargs)
        
        request_id = self._send_signed_request("myTrades", params)
        return self._wait_for_response(request_id)
    
    def get_order_history(self, **kwargs) -> Dict:
        """
        Get order history
        
        Keyword Args:
            symbol (str): Trading symbol
            orderId (int): Order ID
            startTime (int): Start time in milliseconds
            endTime (int): End time in milliseconds
            limit (int): Number of orders to get (default: 500, max: 1000)
            
        Returns:
            Dict: Order history
        """
        request_id = self._send_signed_request("allOrders", kwargs)
        return self._wait_for_response(request_id)
    
    # ===== User Stream Methods =====
    
    def start_user_data_stream(self) -> Dict:
        """
        Start a user data stream
        
        Returns:
            Dict: Response with listen key
        """
        request_id = self._send_request("userDataStream.start", {"apiKey": self.api_key})
        return self._wait_for_response(request_id)
    
    def ping_user_data_stream(self, listen_key: str) -> Dict:
        """
        Ping a user data stream to keep it alive
        
        Args:
            listen_key: Listen key from start_user_data_stream
            
        Returns:
            Dict: Response
        """
        params = {
            "apiKey": self.api_key,
            "listenKey": listen_key
        }
        
        request_id = self._send_request("userDataStream.ping", params)
        return self._wait_for_response(request_id)
    
    def stop_user_data_stream(self, listen_key: str) -> Dict:
        """
        Stop a user data stream
        
        Args:
            listen_key: Listen key from start_user_data_stream
            
        Returns:
            Dict: Response
        """
        params = {
            "apiKey": self.api_key,
            "listenKey": listen_key
        }
        
        request_id = self._send_request("userDataStream.stop", params)
        return self._wait_for_response(request_id)


# Compatibility layer with client.py
class BinanceWSClient:
    """
    Compatibility layer that provides the same interface as the REST client
    but uses WebSocket API under the hood.
    """
    
    def __init__(self, api_key=None, api_secret=None, private_key_path=None, 
                 private_key_pass=None, use_testnet=False):
        self.client = BinanceWebSocketAPIClient(
            api_key=api_key,
            api_secret=api_secret,
            private_key_path=private_key_path,
            private_key_pass=private_key_pass,
            use_testnet=use_testnet
        )
        self.logger = logging.getLogger(__name__)
    
    def exchange_info(self, symbol=None):
        """Get exchange information"""
        try:
            params = {}
            if symbol:
                params["symbol"] = symbol
                
            response = self.client.get_exchange_info(**params)
            if response["status"] == 200:
                return response["result"]
            else:
                self.logger.error(f"Failed to get exchange info: {response}")
                return None
        except Exception as e:
            self.logger.error(f"Failed to get exchange info: {e}")
            raise
    
    def get_symbol_info(self, symbol):
        """Get symbol information"""
        try:
            info = self.exchange_info(symbol=symbol)
            if info and "symbols" in info and len(info["symbols"]) > 0:
                return info["symbols"][0]
            return None
        except Exception as e:
            self.logger.error(f"Failed to get symbol info: {e}")
            raise
    
    def account(self):
        """Get account information"""
        try:
            response = self.client.get_account_info()
            if response["status"] == 200:
                return response["result"]
            else:
                self.logger.error(f"Failed to get account info: {response}")
                return None
        except Exception as e:
            self.logger.error(f"Failed to get account info: {e}")
            raise
    
    def ticker_price(self, symbol=None):
        """Get current price for symbol"""
        try:
            response = self.client.get_ticker_price(symbol=symbol)
            if response["status"] == 200:
                return response["result"]
            else:
                self.logger.error(f"Failed to get ticker price: {response}")
                return None
        except Exception as e:
            self.logger.error(f"Failed to get ticker price: {e}")
            raise
    
    def new_order(self, **params):
        """Place a new order"""
        try:
            symbol = params.pop("symbol")
            side = params.pop("side")
            order_type = params.pop("type")
            
            response = self.client.place_order(
                symbol=symbol,
                side=side,
                order_type=order_type,
                **params
            )
            
            if response["status"] == 200:
                return response["result"]
            else:
                self.logger.error(f"Failed to place order: {response}")
                return None
        except Exception as e:
            self.logger.error(f"Failed to place order: {e}")
            raise
    
    def cancel_order(self, symbol, orderId=None, origClientOrderId=None):
        """Cancel an existing order"""
        try:
            params = {"symbol": symbol}
            if orderId:
                params["orderId"] = orderId
            if origClientOrderId:
                params["origClientOrderId"] = origClientOrderId
                
            response = self.client.cancel_order(**params)
            if response["status"] == 200:
                return response["result"]
            else:
                self.logger.error(f"Failed to cancel order: {response}")
                return None
        except Exception as e:
            self.logger.error(f"Failed to cancel order: {e}")
            raise
    
    def get_open_orders(self, symbol=None):
        """Get open orders"""
        try:
            response = self.client.get_open_orders(symbol=symbol)
            if response["status"] == 200:
                return response["result"]
            else:
                self.logger.error(f"Failed to get open orders: {response}")
                return None
        except Exception as e:
            self.logger.error(f"Failed to get open orders: {e}")
            raise
    
    def klines(self, symbol, interval, startTime=None, endTime=None, limit=500):
        """Get klines/candlestick data"""
        try:
            params = {}
            if startTime:
                params["startTime"] = startTime
            if endTime:
                params["endTime"] = endTime
            if limit:
                params["limit"] = limit
                
            response = self.client.get_klines(
                symbol=symbol,
                interval=interval,
                **params
            )
            
            if response["status"] == 200:
                return response["result"]
            else:
                self.logger.error(f"Failed to get klines: {response}")
                return None
        except Exception as e:
            self.logger.error(f"Failed to get klines: {e}")
            raise
    
    def new_oco_order(self, symbol, side, quantity, price, stopPrice, **kwargs):
        """Create OCO (One-Cancels-the-Other) order"""
        try:
            response = self.client.place_oco_order(
                symbol=symbol,
                side=side,
                quantity=quantity,
                price=price,
                stop_price=stopPrice,
                **kwargs
            )
            
            if response["status"] == 200:
                return response["result"]
            else:
                self.logger.error(f"Failed to create OCO order: {response}")
                return None
        except Exception as e:
            self.logger.error(f"Failed to create OCO order: {e}")
            raise