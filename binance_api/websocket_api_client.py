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
    
    Implements the Binance WebSocket API for trading operations with proper
    protocol handling, authentication, and session management.
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
        timeout: int = 20,  # Increased default timeout to 20 seconds
        event_callback: Optional[Callable[[Dict[str, Any], Optional[int]], None]] = None
    ):
        """
        Initialize the WebSocket API client
        
        Args:
            api_key: Binance API key
            api_secret: Binance API secret (HMAC authentication)
            private_key_path: Path to Ed25519 private key file (preferred for WebSocket API)
            private_key_pass: Password for private key if encrypted
            use_testnet: Whether to use testnet instead of production
            auto_reconnect: Whether to automatically reconnect on disconnection
            ping_interval: How often to send ping frames (seconds)
            timeout: Request timeout (seconds)
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
                    
                # Correctly handle private_key_pass, especially when it's "None"
                if private_key_pass and private_key_pass.lower() != 'none':
                    password = private_key_pass.encode('utf-8')
                else:
                    password = None
                    
                self.private_key = load_pem_private_key(
                    private_key_data, 
                    password=password
                )
                
                # Verify the key type is Ed25519
                if not isinstance(self.private_key, ed25519.Ed25519PrivateKey):
                    self.logger.warning("Loaded key is not an Ed25519 private key. WebSocket API requires Ed25519 keys.")
                    self.logger.warning("Will fall back to HMAC authentication if API secret is provided.")
                    self.private_key = None
            except Exception as e:
                self.logger.error(f"Failed to load private key: {e}")
                self.private_key = None
                self.logger.warning("Will fall back to HMAC authentication if API secret is provided.")
        
        # Connection details (align with official WS-API endpoints)
        self.ws_base_url = (
            "wss://ws-api.testnet.binance.vision/ws-api/v3"
            if use_testnet
            else "wss://ws-api.binance.com/ws-api/v3"
        )
        self.timeout = timeout
        self.ping_interval = ping_interval
        self.auto_reconnect = auto_reconnect
        
        # WebSocket state
        self.ws = None
        self.ws_connected = False
        self.last_received_time = 0
        self.last_ping_time = 0
        self.max_request_attempts = 3  # Added retry attempts
        
        # Request-response management
        self.request_callbacks = {}
        self.response_queue = Queue()
        self.lock = threading.Lock()
        
        # Background threads
        self.listen_thread = None
        self.ping_thread = None
        self._running = False
        self.is_closed_by_user = False
        
        # Session status
        self.session_authenticated = False
        # Optional callback for push events (e.g., user data stream)
        self.event_callback = event_callback
        # Track user data stream subscription for reconnection recovery
        self.user_stream_active = False
        self.user_stream_subscription_id: Optional[int] = None
        
        # Time synchronization variables
        self.time_offset = 0  # Time difference between local and server time in ms
        self.last_sync_time = 0  # Last time we synchronized with server time
        self.sync_interval = 60 * 60  # Re-sync time every hour (seconds)
        self.sync_retry_count = 0
        self.sync_max_retries = 5
        self.time_sync_lock = threading.Lock()
        
        # Connect to WebSocket and synchronize time
        self.connect()
        
        # Initial time synchronization
        self.sync_server_time()
    
    def connect(self) -> bool:
        """
        Establish connection to WebSocket API and authenticate session
        
        Returns:
            bool: True if connection was successful, False otherwise
        """
        if self.ws_connected:
            return True
        
        # Reset state
        self.is_closed_by_user = False
        self.session_authenticated = False
        
        try:
            self.logger.info(f"Connecting to {self.ws_base_url}...")
            
            # Enable trace for debugging if needed
            # websocket.enableTrace(True)
            
            # Create WebSocket connection with proper settings
            self.ws = websocket.create_connection(
                self.ws_base_url,
                timeout=30,  # Increased timeout for initial connection
                sslopt={"cert_reqs": ssl.CERT_REQUIRED},  # Enable certificate verification
                enable_multithread=True,
                skip_utf8_validation=True  # For performance
            )
            
            self.ws_connected = True
            self.last_received_time = time.time()
            self.last_ping_time = time.time()
            
            self._running = True
            
            # Start listener thread
            self.listen_thread = threading.Thread(target=self._listen_forever)
            self.listen_thread.daemon = True
            self.listen_thread.start()
            
            # Start ping thread
            self.ping_thread = threading.Thread(target=self._ping_forever)
            self.ping_thread.daemon = True
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
                        time.sleep(1)  # Short delay between attempts
                except Exception as e:
                    self.logger.warning(f"Ping server attempt {attempt} failed: {e}")
                    if attempt < self.max_request_attempts:
                        time.sleep(2)  # Longer delay before retry
            
            if not ping_success:
                self.logger.error("All ping attempts failed")
                self.close()
                return False
            
            # Synchronize time with server before authentication
            self.sync_server_time()
            
            # Authenticate session if we have an Ed25519 key
            if self.private_key and self.api_key and isinstance(self.private_key, ed25519.Ed25519PrivateKey):
                if not self.authenticate_session():
                    self.logger.warning("Session authentication failed, will sign each request individually")
                
            self.logger.info("Connected to Binance WebSocket API")
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to connect to WebSocket API: {e}")
            self.ws_connected = False
            return False
    
    def sync_server_time(self) -> bool:
        """
        Synchronize local time with Binance server time
        
        Returns:
            bool: True if synchronization was successful, False otherwise
        """
        with self.time_sync_lock:
            try:
                # Get server time
                time_response = self._send_request_raw("time")
                response = self._wait_for_response(time_response)
                
                if response and response.get("status") == 200 and 'result' in response:
                    # Calculate offset (server_time - local_time)
                    server_time = response['result']['serverTime']
                    local_time = int(time.time() * 1000)
                    
                    # Calculate the time offset in milliseconds
                    new_offset = server_time - local_time
                    
                    # Update the time offset
                    self.time_offset = new_offset
                    self.last_sync_time = time.time()
                    
                    # Log time sync results
                    if abs(new_offset) > 1000:
                        # If offset is > 1 second, log as warning
                        self.logger.warning(f"Local time is {'ahead of' if new_offset < 0 else 'behind'} server by "
                                           f"{abs(new_offset)/1000:.2f} seconds. Offset applied: {new_offset}ms")
                    else:
                        # Normal offset within threshold
                        self.logger.debug(f"Time synchronized with server. Offset: {new_offset}ms")
                    
                    # Reset retry count on successful sync
                    self.sync_retry_count = 0
                    return True
                else:
                    self.logger.error(f"Failed to get server time: {response}")
                    return False
            except Exception as e:
                self.logger.error(f"Error synchronizing time: {e}")
                
                # Increment retry count
                self.sync_retry_count += 1
                
                # If we've reached max retries, stop retrying
                if self.sync_retry_count > self.sync_max_retries:
                    self.logger.error(f"Failed to sync time after {self.sync_retry_count} attempts, giving up")
                    return False
                    
                # Try again after a delay (exponential backoff)
                retry_delay = min(30, 2 ** self.sync_retry_count)
                self.logger.warning(f"Retrying time sync in {retry_delay} seconds...")
                time.sleep(retry_delay)
                
                # Recursive retry
                return self.sync_server_time()
    
    def get_adjusted_timestamp(self) -> int:
        """
        Get current timestamp adjusted with server time offset
        
        Returns:
            int: Current timestamp in milliseconds, adjusted to match server time
        """
        # Check if we need to re-sync time
        current_time = time.time()
        if current_time - self.last_sync_time > self.sync_interval:
            # Time for periodic re-sync
            try:
                # Don't block - start a background thread to sync time
                threading.Thread(target=self.sync_server_time, daemon=True).start()
            except Exception as e:
                self.logger.error(f"Failed to start time sync thread: {e}")
        
        # Return current timestamp adjusted with the offset
        return int(time.time() * 1000) + self.time_offset
    
    def authenticate_session(self) -> bool:
        """
        Authenticate the WebSocket session using Ed25519 key
        This is more efficient than signing each request individually
        
        Returns:
            bool: True if authentication was successful, False otherwise
        """
        if not self.api_key or not self.private_key or not isinstance(self.private_key, ed25519.Ed25519PrivateKey):
            self.logger.warning("Cannot authenticate session: API key or valid Ed25519 private key is missing")
            return False
        
        # Use adjusted timestamp
        timestamp = self.get_adjusted_timestamp()
        
        params = {
            "apiKey": self.api_key,
            "timestamp": timestamp
        }
        
        # Generate signature
        sorted_params = sorted(params.items())
        query_string = '&'.join([f"{k}={v}" for k, v in sorted_params])
        
        # Ed25519 signature
        try:
            signature = self.private_key.sign(query_string.encode('utf-8'))
            params["signature"] = base64.b64encode(signature).decode('utf-8')
            
            # Use the correct method name: session.logon
            self.logger.debug(f"Auth query string: {query_string}")
            self.logger.debug(f"Auth signature: {params['signature'][:10]}...{params['signature'][-10:]}")
            
            request_id = self._send_request("session.logon", params)
            response = self._wait_for_response(request_id, timeout=20)  # Increased timeout
            
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
        while self._running and self.ws_connected:
            try:
                # Set a specific receive timeout to prevent blocking indefinitely
                self.ws.settimeout(5.0)
                
                # Use recv_data_frame instead of recv to properly handle all frame types
                op_code, frame = self.ws.recv_data_frame(True)
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
                        self.ws.pong(frame.data)
                    except Exception as e:
                        self.logger.error(f"Failed to send pong: {e}")
                elif op_code == ABNF.OPCODE_PONG:
                    self.logger.debug("Received pong frame")
                elif op_code == ABNF.OPCODE_CLOSE:
                    self.logger.info("Received close frame")
                    if not self.is_closed_by_user:
                        self._handle_disconnect()
                    break
                
            except websocket.WebSocketTimeoutException:
                # This is normal - just a timeout on receive, continue
                continue
            except WebSocketConnectionClosedException:
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
        """Background thread that sends ping frames"""
        while self._running and self.ws_connected:
            try:
                # Wait for ping interval
                time.sleep(1)
                
                # Check if it's time to send a ping
                current_time = time.time()
                if current_time - self.last_ping_time >= self.ping_interval:
                    if self.ws_connected:
                        try:
                            # Send a websocket ping frame
                            self.ws.ping()
                            self.last_ping_time = current_time
                            self.logger.debug("Sent ping frame")
                        except Exception as e:
                            self.logger.error(f"Error sending ping frame: {e}")
                            
                # Check for connection timeout
                if current_time - self.last_received_time > 60:  # 60 seconds without any response
                    self.logger.warning("No messages received for 60 seconds, reconnecting...")
                    self._handle_disconnect()
                    break
                    
            except Exception as e:
                self.logger.error(f"Error in ping thread: {e}")
    
    def _handle_disconnect(self):
        """Handle unexpected disconnections"""
        self.logger.warning("Connection lost, cleaning up...")
        self.ws_connected = False
        self.session_authenticated = False
        
        if self.ws:
            try:
                self.ws.close()
            except:
                pass
            self.ws = None
        
        # Auto-reconnect if enabled
        if self.auto_reconnect and not self.is_closed_by_user:
            self.logger.info("Attempting to reconnect...")
            
            # Exponential backoff for reconnection attempts
            attempts = 0
            max_attempts = 5
            while attempts < max_attempts and not self.ws_connected and self._running:
                wait_time = min(60, (2 ** attempts))
                self.logger.info(f"Reconnecting in {wait_time} seconds (attempt {attempts+1}/{max_attempts})...")
                time.sleep(wait_time)
                
                if self.connect():
                    self.logger.info("Reconnected successfully")
                    # Re-subscribe to user data stream if previously active
                    if self.user_stream_active and self.event_callback:
                        try:
                            resp = self.subscribe_user_data_stream()
                            self.logger.info(f"Recovered user data stream subscription: {resp}")
                        except Exception as sub_err:
                            self.logger.error(f"Failed to recover user data stream after reconnect: {sub_err}")
                    break
                
                attempts += 1
                
            if not self.ws_connected:
                self.logger.error(f"Failed to reconnect after {max_attempts} attempts")
    
    def close(self):
        """Close the WebSocket connection gracefully"""
        self.logger.info("Closing WebSocket connection...")
        self.is_closed_by_user = True
        self._running = False
        
        # Clear all pending requests
        with self.lock:
            self.request_callbacks = {}
        
        if self.ws:
            try:
                self.ws.close()
            except:
                pass
        
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
                elif error_code == -1021:
                    # Timestamp out of sync with server
                    self.logger.warning("Timestamp error detected, re-syncing time with server...")
                    # Force time resync immediately
                    self.sync_server_time()
                else:
                    self.logger.error(f"Error {error_code}: {error_msg}")
            
            # Handle responses to requests with thread safety
            if request_id:
                with self.lock:
                    if request_id in self.request_callbacks:
                        callback = self.request_callbacks.pop(request_id)
                        if callback:
                            callback(data)
                self.response_queue.put(data)
            # Handle push events (e.g., user data stream events)
            elif 'event' in data:
                event_payload = data.get('event')
                subscription_id = data.get('subscriptionId')
                if self.event_callback:
                    try:
                        self.event_callback(event_payload, subscription_id)
                    except Exception as cb_err:
                        self.logger.error(f"Event callback failed: {cb_err}")
                else:
                    self.logger.debug(f"Received event without handler: {data}")
            else:
                self.logger.debug(f"Unhandled message: {message[:200]}...")
                
        except json.JSONDecodeError:
            self.logger.error(f"Failed to parse message as JSON: {message[:200]}...")
        except Exception as e:
            self.logger.error(f"Error handling message: {e}, message: {message[:200]}...")
    
    def _send_request_raw(self, method: str, params: Optional[Dict] = None) -> str:
        """
        Send a request to the WebSocket API without automatic time adjustment
        Used internally for time synchronization to avoid circular dependencies
        
        Args:
            method: API method name
            params: Request parameters
            
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
        
        # Send request
        request_json = json.dumps(request)
        self.logger.debug(f"Sending raw request: {request_json[:200]}...")
        
        try:
            with self.lock:
                if self.ws and self.ws_connected:
                    self.ws.send(request_json)
                else:
                    raise ConnectionError("WebSocket is not connected")
        except Exception as e:
            self.logger.error(f"Error sending raw request: {e}")
            raise
        
        return request_id
    
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
        
        # Register callback for response (with thread safety)
        if callback:
            with self.lock:
                self.request_callbacks[request_id] = callback
        
        # Send request
        request_json = json.dumps(request)
        self.logger.debug(f"Sending request: {request_json[:200]}...")
        try:
            with self.lock:
                if self.ws and self.ws_connected:
                    self.ws.send(request_json)
                else:
                    raise ConnectionError("WebSocket is not connected")
        except Exception as e:
            self.logger.error(f"Error sending request: {e}")
            with self.lock:
                if request_id in self.request_callbacks:
                    self.request_callbacks.pop(request_id)
            raise
        
        return request_id
    
    def _send_signed_request(
        self, 
        method: str, 
        params: Optional[Dict] = None, 
        callback: Optional[Callable] = None
    ) -> str:
        """
        Send a signed request to the WebSocket API
        
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
        
        # Add timestamp for signature with server time adjustment
        params['timestamp'] = self.get_adjusted_timestamp()
        
        # If session is authenticated, we don't need to send apiKey and signature
        if self.session_authenticated:
            return self._send_request(method, params, callback)
        
        # Otherwise, sign the request
        params['apiKey'] = self.api_key
        
        try:
            # Import needed for signature generation - import here to avoid circular imports
            try:
                from binance.lib.utils import websocket_api_signature
                
                # Use the official Binance signature method if available
                params = websocket_api_signature(self.api_key, self.api_secret, params)
            except ImportError:
                # Fallback to our custom signature method
                params['signature'] = self._generate_signature(params)
            
            return self._send_request(method, params, callback)
        except Exception as e:
            self.logger.error(f"Error signing request: {e}")
            raise
    
    def _generate_signature(self, params: Dict) -> str:
        """
        Generate signature for API request
        
        Args:
            params: Request parameters
            
        Returns:
            str: Signature for the request
            
        Raises:
            ValueError: If no authentication method is available
        """
        # Sort parameters by key name
        sorted_params = sorted(params.items())
        
        # Convert to query string format
        query_string = '&'.join([f"{k}={v}" for k, v in sorted_params if k != 'signature'])
        
        # Sign with Ed25519 key (recommended for WebSocket API)
        if self.private_key and isinstance(self.private_key, ed25519.Ed25519PrivateKey):
            try:
                signature = self.private_key.sign(query_string.encode('utf-8'))
                return base64.b64encode(signature).decode('utf-8')
            except Exception as e:
                self.logger.error(f"Ed25519 signature failed: {e}")
                # Try HMAC fallback if API secret is available
                if self.api_secret:
                    self.logger.info("Falling back to HMAC authentication")
                else:
                    raise
        
        # HMAC signature - less preferred for WebSocket API
        if self.api_secret:
            import hmac
            from hashlib import sha256
            signature = hmac.new(
                self.api_secret.encode('utf-8'),
                query_string.encode('utf-8'),
                sha256
            ).hexdigest()
            return signature
        
        raise ValueError("No authentication method available")
    
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
                        
                        # Check for timestamp error and auto-resync
                        if 'error' in response and response['error'].get('code') == -1021:
                            self.logger.warning("Timestamp error in response, re-syncing time...")
                            self.sync_server_time()
                            
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
            raise
    
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
        response = self._wait_for_response(request_id)
        
        # Auto-update time offset when we get server time
        if response and response.get("status") == 200 and 'result' in response:
            server_time = response['result']['serverTime']
            local_time = int(time.time() * 1000)
            new_offset = server_time - local_time
            
            # Update offset with thread safety
            with self.time_sync_lock:
                self.time_offset = new_offset
                self.last_sync_time = time.time()
                
                if abs(new_offset) > 1000:
                    self.logger.info(f"Time offset updated: {new_offset}ms")
        
        return response
    
    # ===== Account Methods =====
    
    def get_account_info(self) -> Dict:
        """
        Get account information
        
        Returns:
            Dict: Account info including balances
        """
        request_id = self._send_signed_request("account.status")
        return self._wait_for_response(request_id)
    
    # ===== User Stream Methods (WebSocket API per userDataStream.subscribe) =====
    
    def subscribe_user_data_stream(self) -> Dict:
        """
        Subscribe to the User Data Stream on the current WS-API connection.
        
        Returns:
            Dict: Response containing subscriptionId
        """
        method = "userDataStream.subscribe"
        if self.session_authenticated:
            request_id = self._send_request(method)
        else:
            # Use signature-based subscription when not authenticated via session.logon
            request_id = self._send_signed_request(f"{method}.signature")
        response = self._wait_for_response(request_id)
        if response and response.get("status") == 200:
            sub_id = response.get("result", {}).get("subscriptionId")
            self.user_stream_subscription_id = sub_id
            self.user_stream_active = True
        return response

    def unsubscribe_user_data_stream(self, subscription_id: Optional[int] = None) -> Dict:
        """
        Unsubscribe from User Data Stream.
        
        Args:
            subscription_id: Optional specific subscriptionId; if None, all subscriptions are closed.
        """
        params = {}
        if subscription_id is not None:
            params["subscriptionId"] = subscription_id
        request_id = self._send_request("userDataStream.unsubscribe", params or None)
        response = self._wait_for_response(request_id)
        self.user_stream_active = False
        self.user_stream_subscription_id = None
        return response

    # Helper method to verify private key
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
    
    def __init__(self, api_key=None, api_secret=None, private_key_path=None, 
                 private_key_pass=None, use_testnet=False, event_callback=None):
        self.client = BinanceWebSocketAPIClient(
            api_key=api_key,
            api_secret=api_secret,
            private_key_path=private_key_path,
            private_key_pass=private_key_pass,
            use_testnet=use_testnet,
            timeout=30,  # Increased timeout
            event_callback=event_callback
        )
        self.logger = logging.getLogger(__name__)
    
    def check_connectivity(self, timeout=20):
        """Test WebSocket API connectivity"""
        try:
            # Also ensure time is synchronized
            self.client.sync_server_time()
            
            # Test general connectivity
            response = self.client.ping_server()
            return response and response.get("status") == 200
        except Exception as e:
            self.logger.error(f"Connectivity check failed: {e}")
            return False

    # ---- User data stream (WS-API) helpers ----
    def start_user_stream(self, on_event=None):
        """
        Subscribe to user data stream; pass-through of subscriptionId.
        on_event: callable(event_payload, subscription_id)
        """
        if on_event:
            self.client.event_callback = on_event
        response = self.client.subscribe_user_data_stream()
        return response.get("result", {}).get("subscriptionId")

    def stop_user_stream(self, subscription_id=None):
        return self.client.unsubscribe_user_data_stream(subscription_id)

    def new_oco_order(self, symbol, side, quantity, price, stopPrice, stopLimitPrice=None, 
                     stopLimitTimeInForce="GTC", aboveType=None, belowType=None, **kwargs):
        """
        Create an OCO order via WebSocket API
        
        For SELL orders (risk management):
        - above leg: LIMIT_MAKER (take profit - executed when price rises)
        - below leg: STOP_LOSS (stop loss - triggered when price drops)
        
        For BUY orders:
        - above leg: STOP_LOSS (stop loss - triggered when price rises)
        - below leg: LIMIT_MAKER (take profit - executed when price drops)
        """
        try:
            params = {
                "symbol": symbol,
                "side": side,
                "quantity": str(quantity)  # Ensure string format
            }

            # Map legs based on side, allow limit stop legs when stopLimitPrice is provided
            if side == "SELL":
                params["aboveType"] = aboveType or "LIMIT_MAKER"
                params["abovePrice"] = str(price)

                if stopLimitPrice:
                    params["belowType"] = belowType or "STOP_LOSS_LIMIT"
                    params["belowPrice"] = str(stopLimitPrice)
                    params["belowStopPrice"] = str(stopPrice)
                    params["belowTimeInForce"] = stopLimitTimeInForce
                else:
                    params["belowType"] = belowType or "STOP_LOSS"
                    params["belowStopPrice"] = str(stopPrice)
            else:
                # BUY: invert legs
                if stopLimitPrice:
                    params["aboveType"] = aboveType or "STOP_LOSS_LIMIT"
                    params["abovePrice"] = str(stopLimitPrice)
                    params["aboveStopPrice"] = str(stopPrice)
                    params["aboveTimeInForce"] = stopLimitTimeInForce
                else:
                    params["aboveType"] = aboveType or "STOP_LOSS"
                    params["aboveStopPrice"] = str(stopPrice)

                params["belowType"] = belowType or "LIMIT_MAKER"
                params["belowPrice"] = str(price)
            
            # Log the parameters for debugging
            self.logger.debug(f"Sending OCO order via WebSocket: {params}")
            
            # Send the request using the orderList.place.oco endpoint
            request_id = self.client._send_signed_request("orderList.place.oco", params)
            return self.client._wait_for_response(request_id)
            
        except Exception as e:
            self.logger.error(f"Error creating OCO order via WebSocket API: {e}")
            raise

    def exchange_info(self, symbol=None):
        """Fetch exchange information via WS API."""
        params = {}
        if symbol:
            params["symbol"] = symbol
        request_id = self.client._send_request("exchangeInfo", params)
        return self.client._wait_for_response(request_id)

    def ticker_price(self, symbol):
        """Get latest price for a symbol via WS API."""
        params = {"symbol": symbol}
        request_id = self.client._send_request("ticker.price", params)
        return self.client._wait_for_response(request_id)

    def depth(self, symbol, limit=20):
        """Get partial order book depth via WS API."""
        params = {"symbol": symbol, "limit": limit}
        request_id = self.client._send_request("depth", params)
        return self.client._wait_for_response(request_id)

    def klines(self, symbol, interval, startTime=None, endTime=None, limit=500):
        """Get kline data via WS API."""
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        if startTime is not None:
            params["startTime"] = startTime
        if endTime is not None:
            params["endTime"] = endTime
        request_id = self.client._send_request("klines", params)
        return self.client._wait_for_response(request_id)

    def account(self):
        """Get account status (balances) via WS API."""
        request_id = self.client._send_signed_request("account.status")
        return self.client._wait_for_response(request_id)

    def new_order(self, **params):
        """Place a new order via WS API."""
        request_id = self.client._send_signed_request("order.place", params)
        return self.client._wait_for_response(request_id)

    def cancel_order(self, **params):
        """Cancel an order via WS API."""
        request_id = self.client._send_signed_request("order.cancel", params)
        return self.client._wait_for_response(request_id)

    def get_open_orders(self, symbol=None):
        """Get open orders via WS API."""
        params = {}
        if symbol:
            params["symbol"] = symbol
        request_id = self.client._send_signed_request("openOrders.status", params)
        return self.client._wait_for_response(request_id)

    def cancel_oco_order(self, **params):
        """Cancel an order list (OCO) via WS API."""
        request_id = self.client._send_signed_request("orderList.cancel", params)
        return self.client._wait_for_response(request_id)
