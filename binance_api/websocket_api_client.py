import os
import json
import time
import uuid
import base64
import logging
import threading
import ssl
from queue import Queue, Empty
from typing import Optional, Dict, Any, Callable
import websocket
from websocket import WebSocketConnectionClosedException, ABNF
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from cryptography.hazmat.primitives.asymmetric import ed25519

class BinanceWebSocketAPIClient:
    """
    Binance WebSocket API Client
    
    Implements the Binance WebSocket API for trading operations with Ed25519
    signing, proper connection management and thread safety.
    """
    
    def __init__(
        self, 
        api_key: str = None, 
        private_key_path: str = None,
        private_key_pass: str = None,
        use_testnet: bool = False,
        auto_reconnect: bool = True,
        ping_interval: int = 20,
        timeout: int = 20
    ):
        """
        Initialize the WebSocket API client with Ed25519 authentication
        
        Args:
            api_key: Binance API key
            private_key_path: Path to Ed25519 private key file
            private_key_pass: Password for private key if encrypted
            use_testnet: Whether to use testnet instead of production
            auto_reconnect: Whether to automatically reconnect on disconnection
            ping_interval: How often to send ping frames (seconds)
            timeout: Request timeout (seconds)
        """
        self.logger = logging.getLogger(__name__)
        
        # Authentication details
        self.api_key = api_key
        self.private_key_path = private_key_path
        self.private_key_pass = private_key_pass
        self.private_key = None
        
        # Load Ed25519 private key
        if private_key_path:
            self._load_ed25519_key()
        
        # Connection details
        self.ws_base_url = "wss://testnet.binance.vision/ws-api/v3" if use_testnet else "wss://ws-api.binance.com/ws-api/v3"
        self.timeout = timeout
        self.ping_interval = ping_interval
        self.auto_reconnect = auto_reconnect
        
        # WebSocket state with thread safety
        self.ws = None
        self.state_lock = threading.RLock()
        with self.state_lock:
            self.ws_connected = False
            self.last_received_time = 0
            self.last_ping_time = 0
            self._running = False
            self.is_closed_by_user = False
            self.session_authenticated = False
        
        # Request-response management
        self.request_callbacks = {}
        self.response_queue = Queue()
        self.lock = threading.RLock()
        
        # Background threads
        self.listen_thread = None
        self.ping_thread = None
        
        # Configuration
        self.max_request_attempts = 3
        self.max_reconnect_attempts = 5
        self.exponential_backoff_base = 2
        
        # Connect to WebSocket
        self.connect()
    
    def _load_ed25519_key(self):
        """Load the Ed25519 private key from file"""
        if not self.private_key_path or not os.path.isfile(self.private_key_path):
            self.logger.error(f"Private key file not found: {self.private_key_path}")
            raise FileNotFoundError(f"Private key file not found: {self.private_key_path}")
        
        try:
            with open(self.private_key_path, 'rb') as f:
                private_key_data = f.read()
                
            # Handle password correctly
            password = None
            if self.private_key_pass and self.private_key_pass.lower() != 'none':
                password = self.private_key_pass.encode('utf-8')
                
            # Load the private key
            loaded_key = load_pem_private_key(
                private_key_data, 
                password=password
            )
            
            # Verify it's an Ed25519 key
            if not isinstance(loaded_key, ed25519.Ed25519PrivateKey):
                self.logger.error("The provided key is not an Ed25519 private key.")
                raise ValueError("WebSocket API requires an Ed25519 private key")
                
            self.private_key = loaded_key
            self.logger.info("Ed25519 private key loaded successfully")
            
        except Exception as e:
            self.logger.error(f"Failed to load Ed25519 private key: {e}")
            raise
    
    def connect(self) -> bool:
        """
        Establish connection to WebSocket API and authenticate session
        
        Returns:
            bool: True if connection was successful, False otherwise
        """
        with self.state_lock:
            if self.ws_connected:
                return True
            
            # Reset state
            self.is_closed_by_user = False
            self.session_authenticated = False
        
        try:
            self.logger.info(f"Connecting to {self.ws_base_url}...")
            
            # Create WebSocket connection with proper settings
            ws = websocket.create_connection(
                self.ws_base_url,
                timeout=30,
                sslopt={"cert_reqs": ssl.CERT_REQUIRED},
                enable_multithread=True,
                skip_utf8_validation=True
            )
            
            with self.state_lock:
                self.ws = ws
                self.ws_connected = True
                self.last_received_time = time.time()
                self.last_ping_time = time.time()
                self._running = True
            
            # Start listener thread
            self.listen_thread = threading.Thread(target=self._listen_forever, daemon=True)
            self.listen_thread.start()
            
            # Start ping thread
            self.ping_thread = threading.Thread(target=self._ping_forever, daemon=True)
            self.ping_thread.start()
            
            # Test connection with ping (with retry)
            ping_success = False
            for attempt in range(1, self.max_request_attempts + 1):
                try:
                    self.logger.debug(f"Ping server attempt {attempt}/{self.max_request_attempts}")
                    ping_response = self.ping_server()
                    if ping_response and ping_response.get("status") == 200:
                        ping_success = True
                        self.logger.info("Connectivity check successful")
                        break
                    else:
                        self.logger.warning(f"Ping response not successful: {ping_response}")
                        time.sleep(1)
                except Exception as e:
                    self.logger.warning(f"Ping server attempt {attempt} failed: {e}")
                    if attempt < self.max_request_attempts:
                        time.sleep(2)
            
            if not ping_success:
                self.logger.error("All ping attempts failed")
                self.close()
                return False
            
            # Authenticate session if we have an API key and Ed25519 key
            if self.api_key and self.private_key:
                if not self.authenticate_session():
                    self.logger.warning("Session authentication failed, will sign each request individually")
                
            self.logger.info("Connected to Binance WebSocket API")
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to connect to WebSocket API: {e}")
            with self.state_lock:
                self.ws_connected = False
            return False
    
    def authenticate_session(self) -> bool:
        """
        Authenticate the WebSocket session using Ed25519 key
        
        Returns:
            bool: True if authentication was successful, False otherwise
        """
        if not self.api_key or not self.private_key:
            self.logger.warning("Cannot authenticate session: API key or Ed25519 private key is missing")
            return False
        
        timestamp = int(time.time() * 1000)
        
        params = {
            "apiKey": self.api_key,
            "timestamp": timestamp
        }
        
        # Generate signature
        sorted_params = sorted(params.items())
        query_string = '&'.join([f"{k}={v}" for k, v in sorted_params])
        
        try:
            signature = self.private_key.sign(query_string.encode('utf-8'))
            params["signature"] = base64.b64encode(signature).decode('utf-8')
            
            self.logger.debug(f"Auth query string: {query_string}")
            self.logger.debug(f"Auth signature: {params['signature'][:10]}...{params['signature'][-10:]}")
            
            request_id = self._send_request("session.logon", params)
            response = self._wait_for_response(request_id, timeout=20)
            
            with self.state_lock:
                if response and response.get("status") == 200:
                    self.session_authenticated = True
                    self.logger.info("Successfully authenticated WebSocket API session")
                    return True
                else:
                    error_code = response.get('error', {}).get('code') if response else "No error code"
                    error_msg = response.get('error', {}).get('msg', 'Unknown error') if response else "No response"
                    self.logger.error(f"Error {error_code}: {error_msg}")
                    self.logger.error(f"Failed to authenticate session: {error_msg}")
                    return False
        except Exception as e:
            self.logger.error(f"Error authenticating session: {e}")
            return False
    
    def _listen_forever(self):
        """Background thread that listens for incoming messages"""
        while True:
            with self.state_lock:
                if not self._running or not self.ws_connected:
                    break
            
            try:
                # Set a specific receive timeout to prevent blocking indefinitely
                with self.state_lock:
                    if self.ws:
                        self.ws.settimeout(5.0)
                
                # Use recv_data_frame to properly handle all frame types
                op_code, frame = None, None
                with self.state_lock:
                    if self.ws and self.ws_connected:
                        op_code, frame = self.ws.recv_data_frame(True)
                
                if not frame:
                    continue
                
                with self.state_lock:
                    self.last_received_time = time.time()
                
                # Handle different frame types
                if op_code == ABNF.OPCODE_TEXT:
                    message = frame.data.decode("utf-8")
                    self._handle_message(message)
                elif op_code == ABNF.OPCODE_BINARY:
                    self.logger.debug("Received binary frame")
                elif op_code == ABNF.OPCODE_PING:
                    self.logger.debug("Received ping frame, sending pong")
                    try:
                        with self.state_lock:
                            if self.ws and self.ws_connected:
                                self.ws.pong(frame.data)
                    except Exception as e:
                        self.logger.error(f"Failed to send pong: {e}")
                elif op_code == ABNF.OPCODE_PONG:
                    self.logger.debug("Received pong frame")
                elif op_code == ABNF.OPCODE_CLOSE:
                    self.logger.info("Received close frame")
                    with self.state_lock:
                        if not self.is_closed_by_user:
                            self._handle_disconnect()
                    break
                
            except websocket.WebSocketTimeoutException:
                # This is normal - just a timeout on receive, continue
                continue
            except WebSocketConnectionClosedException:
                with self.state_lock:
                    if not self.is_closed_by_user:
                        self.logger.error("WebSocket connection closed unexpectedly")
                        self._handle_disconnect()
                break
            except Exception as e:
                self.logger.error(f"Error while listening for messages: {e}")
                with self.state_lock:
                    if not self.is_closed_by_user:
                        self._handle_disconnect()
                break
    
    def _ping_forever(self):
        """Background thread that sends ping frames"""
        while True:
            try:
                with self.state_lock:
                    if not self._running or not self.ws_connected:
                        break
                    
                # Wait for ping interval
                time.sleep(1)
                
                # Check if it's time to send a ping
                current_time = time.time()
                with self.state_lock:
                    should_ping = (current_time - self.last_ping_time >= self.ping_interval)
                    connection_timeout = (current_time - self.last_received_time > 60)
                
                # Send ping if needed
                if should_ping:
                    try:
                        with self.state_lock:
                            if self.ws and self.ws_connected:
                                self.ws.ping()
                                self.last_ping_time = current_time
                                self.logger.debug("Sent ping frame")
                    except Exception as e:
                        self.logger.error(f"Error sending ping frame: {e}")
                        
                # Check for connection timeout
                if connection_timeout:
                    self.logger.warning("No messages received for 60 seconds, reconnecting...")
                    with self.state_lock:
                        self._handle_disconnect()
                    break
                    
            except Exception as e:
                self.logger.error(f"Error in ping thread: {e}")
    
    def _handle_disconnect(self):
        """Handle unexpected disconnections"""
        self.logger.warning("Connection lost, cleaning up...")
        
        with self.state_lock:
            self.ws_connected = False
            self.session_authenticated = False
            
            if self.ws:
                try:
                    self.ws.close()
                except:
                    pass
                self.ws = None
        
        # Auto-reconnect if enabled
        should_reconnect = False
        with self.state_lock:
            should_reconnect = self.auto_reconnect and not self.is_closed_by_user and self._running
            
        if should_reconnect:
            self.logger.info("Attempting to reconnect...")
            
            # Exponential backoff for reconnection attempts
            attempts = 0
            while attempts < self.max_reconnect_attempts:
                wait_time = min(60, (self.exponential_backoff_base ** attempts))
                self.logger.info(f"Reconnecting in {wait_time} seconds (attempt {attempts+1}/{self.max_reconnect_attempts})...")
                time.sleep(wait_time)
                
                if self.connect():
                    self.logger.info("Reconnected successfully")
                    return
                
                attempts += 1
                
            self.logger.error(f"Failed to reconnect after {self.max_reconnect_attempts} attempts")
    
    def close(self):
        """Close the WebSocket connection gracefully"""
        self.logger.info("Closing WebSocket connection...")
        
        with self.state_lock:
            self.is_closed_by_user = True
            self._running = False
        
        # Clear all pending requests
        with self.lock:
            self.request_callbacks = {}
        
        with self.state_lock:
            if self.ws:
                try:
                    self.ws.close()
                except:
                    pass
                self.ws = None
            
            self.ws_connected = False
            self.session_authenticated = False
            
        self.logger.info("WebSocket connection closed")
    
    def _generate_request_id(self) -> str:
        """
        Generate a unique request ID
        
        Returns:
            str: Unique UUID for request identification
        """
        return str(uuid.uuid4())
    
    def _handle_message(self, message: str):
        """
        Process incoming message from WebSocket
        
        Args:
            message: JSON message received from server
        """
        try:
            # Parse message
            data = json.loads(message)
            request_id = data.get('id')
            
            # Debug raw message
            self.logger.debug(f"Received message: {message[:200]}...")
            
            # Log error responses
            if 'error' in data:
                error_code = data['error'].get('code')
                error_msg = data['error'].get('msg')
                
                # Handle specific error codes
                if error_code == 429:
                    # Rate limit exceeded
                    retry_after = data['error'].get('data', {}).get('retryAfter', 30)
                    self.logger.warning(f"Rate limit exceeded, retry after {retry_after}s")
                elif error_code == 418:
                    # IP ban
                    ban_until = data['error'].get('data', {}).get('banUntil', '')
                    self.logger.error(f"IP banned until {ban_until}")
                else:
                    self.logger.error(f"Error {error_code}: {error_msg}")
            
            # Handle responses to requests with thread safety
            if request_id:
                callback = None
                with self.lock:
                    if request_id in self.request_callbacks:
                        callback = self.request_callbacks.pop(request_id)
                        
                if callback:
                    callback(data)
                    
                self.response_queue.put(data)
                
            # Handle event notifications
            elif 'event' in data:
                self.logger.debug(f"Received event: {data['event']}")
            else:
                self.logger.debug(f"Unhandled message: {message[:200]}...")
                
        except json.JSONDecodeError:
            self.logger.error(f"Failed to parse message as JSON: {message[:200]}...")
        except Exception as e:
            self.logger.error(f"Error handling message: {e}, message: {message[:200]}...")
    
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
            callback: Optional callback for response
            
        Returns:
            str: Request ID
            
        Raises:
            ConnectionError: If not connected to WebSocket API
        """
        is_connected = False
        with self.state_lock:
            is_connected = self.ws_connected
            
        if not is_connected:
            if not self.connect():
                raise ConnectionError("Failed to connect to WebSocket API")
        
        request_id = self._generate_request_id()
        request = {
            "id": request_id,
            "method": method
        }
        
        if params:
            request["params"] = params
        
        # Register callback for response (with thread safety)
        if callback:
            with self.lock:
                self.request_callbacks[request_id] = callback
        
        # Send request
        request_json = json.dumps(request)
        self.logger.debug(f"Sending request: {request_json[:200]}...")
        
        send_success = False
        try:
            with self.state_lock:
                if self.ws and self.ws_connected:
                    self.ws.send(request_json)
                    send_success = True
                else:
                    raise ConnectionError("WebSocket is not connected")
        except Exception as e:
            self.logger.error(f"Error sending request: {e}")
            with self.lock:
                if request_id in self.request_callbacks:
                    self.request_callbacks.pop(request_id)
            raise
        
        if not send_success:
            with self.lock:
                if request_id in self.request_callbacks:
                    self.request_callbacks.pop(request_id)
            raise ConnectionError("Failed to send message")
            
        return request_id
    
    def _send_signed_request(
        self, 
        method: str, 
        params: Optional[Dict] = None, 
        callback: Optional[Callable] = None
    ) -> str:
        """
        Send a signed request to the WebSocket API using Ed25519
        
        If the session is authenticated, only send timestamp
        Otherwise, send full apiKey and signature
        
        Args:
            method: API method name
            params: Request parameters
            callback: Optional callback for response
            
        Returns:
            str: Request ID
        """
        if not params:
            params = {}
        
        # Add timestamp for signature
        params['timestamp'] = int(time.time() * 1000)
        
        # Check if session is authenticated
        session_auth = False
        with self.state_lock:
            session_auth = self.session_authenticated
            
        # If session is authenticated, we don't need to send apiKey and signature
        if session_auth:
            return self._send_request(method, params, callback)
        
        # Otherwise, sign the request
        if not self.api_key or not self.private_key:
            raise ValueError("API key and Ed25519 private key are required for signed requests")
            
        params['apiKey'] = self.api_key
        
        try:
            # Sign the request
            params['signature'] = self._generate_signature(params)
            return self._send_request(method, params, callback)
        except Exception as e:
            self.logger.error(f"Error signing request: {e}")
            raise
    
    def _generate_signature(self, params: Dict) -> str:
        """
        Generate Ed25519 signature for API request
        
        Args:
            params: Request parameters
            
        Returns:
            str: Base64 encoded Ed25519 signature
            
        Raises:
            ValueError: If Ed25519 private key is not available
        """
        if not self.private_key:
            raise ValueError("Ed25519 private key is required for signing")
            
        # Sort parameters by key name
        sorted_params = sorted(params.items())
        
        # Convert to query string format
        query_string = '&'.join([f"{k}={v}" for k, v in sorted_params if k != 'signature'])
        
        # Sign with Ed25519 key
        try:
            signature = self.private_key.sign(query_string.encode('utf-8'))
            return base64.b64encode(signature).decode('utf-8')
        except Exception as e:
            self.logger.error(f"Ed25519 signature failed: {e}")
            raise
    
    def _wait_for_response(self, request_id: str, timeout: int = None) -> Dict:
        """
        Wait for response to a specific request
        
        Args:
            request_id: ID of the request to wait for
            timeout: How long to wait (seconds)
            
        Returns:
            Dict: Response data
            
        Raises:
            TimeoutError: If response is not received within timeout
        """
        if timeout is None:
            timeout = self.timeout
        
        start_time = time.time()
        responses_to_requeue = []
        
        try:
            while time.time() - start_time < timeout:
                try:
                    # Use a shorter timeout for queue.get() to allow checking the loop condition more frequently
                    response = self.response_queue.get(timeout=0.5)
                    if response.get('id') == request_id:
                        # Re-queue any responses we set aside
                        for r in responses_to_requeue:
                            self.response_queue.put(r)
                        return response
                    else:
                        # Save this response to put back later
                        responses_to_requeue.append(response)
                except Empty:
                    # Queue.get timeout - this is normal, continue
                    pass
            
            # Clean up the callback entry if it exists
            with self.lock:
                if request_id in self.request_callbacks:
                    self.request_callbacks.pop(request_id)
            
            # Re-queue any responses we set aside
            for r in responses_to_requeue:
                self.response_queue.put(r)
                
            raise TimeoutError(f"Timed out waiting for response to request {request_id}")
            
        except Exception as e:
            # Re-queue any responses we set aside
            for r in responses_to_requeue:
                self.response_queue.put(r)
            
            if isinstance(e, TimeoutError):
                raise
            else:
                raise RuntimeError(f"Error waiting for response: {e}")
    
    # ===== Basic API Methods =====
    
    def ping_server(self) -> Dict:
        """
        Test connectivity to the WebSocket API
        
        Returns:
            Dict: Server response
        """
        request_id = self._send_request("ping")
        return self._wait_for_response(request_id)
    
    def get_server_time(self) -> Dict:
        """
        Get current server time
        
        Returns:
            Dict: Server response with time
        """
        request_id = self._send_request("time")
        return self._wait_for_response(request_id)
    
    # ===== Account Methods =====
    
    def get_account_info(self) -> Dict:
        """
        Get account information
        
        Returns:
            Dict: Account info including balances
        """
        request_id = self._send_signed_request("account.status")
        return self._wait_for_response(request_id)
    
    # ===== User Stream Methods =====
    
    def start_user_data_stream(self) -> Dict:
        """
        Start a user data stream
        
        Returns:
            Dict: Response containing listenKey
        """
        request_id = self._send_request("userDataStream.start", {"apiKey": self.api_key})
        return self._wait_for_response(request_id)
    
    def ping_user_data_stream(self, listen_key: str) -> Dict:
        """
        Ping a user data stream to keep it alive
        
        Args:
            listen_key: Listen key to ping
            
        Returns:
            Dict: Server response
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
            listen_key: Listen key to stop
            
        Returns:
            Dict: Server response
        """
        params = {
            "apiKey": self.api_key,
            "listenKey": listen_key
        }
        
        request_id = self._send_request("userDataStream.stop", params)
        return self._wait_for_response(request_id)

    def verify_private_key(self):
        """
        Verify that the loaded private key is valid and of the correct type
        
        Returns:
            dict: Status of the private key
        """
        if not self.private_key:
            return {
                "valid": False,
                "error": "No private key loaded"
            }
            
        if not isinstance(self.private_key, ed25519.Ed25519PrivateKey):
            return {
                "valid": False,
                "error": "Loaded key is not an Ed25519 private key",
                "actual_type": str(type(self.private_key))
            }
            
        try:
            # Try to get the public key as a validation test
            public_key = self.private_key.public_key()
            return {
                "valid": True,
                "type": "Ed25519"
            }
        except Exception as e:
            return {
                "valid": False,
                "error": f"Key validation failed: {str(e)}"
            }


# Compatibility layer with client.py
class BinanceWSClient:
    """
    Compatibility layer that provides the same interface as the REST client
    but uses WebSocket API under the hood.
    """
    
    def __init__(self, api_key=None, private_key_path=None, 
                 private_key_pass=None, use_testnet=False):
        self.client = BinanceWebSocketAPIClient(
            api_key=api_key,
            private_key_path=private_key_path,
            private_key_pass=private_key_pass,
            use_testnet=use_testnet,
            timeout=30
        )
        self.logger = logging.getLogger(__name__)
    
    def check_connectivity(self, timeout=20):
        """Test WebSocket API connectivity"""
        try:
            response = self.client.ping_server()
            return response and response.get("status") == 200
        except Exception as e:
            self.logger.error(f"Connectivity check failed: {e}")
            return False