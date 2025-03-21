import json
import time
import logging
from binance.websocket.spot.websocket_stream import SpotWebsocketStreamClient

class WebsocketManager:
    def __init__(self, on_message_callback, on_error_callback=None):
        self.ws_client = SpotWebsocketStreamClient(on_message=self._message_handler, on_error=self._error_handler)
        self.on_message_callback = on_message_callback
        self.on_error_callback = on_error_callback
        self.logger = logging.getLogger(__name__)
        self.reconnect_delay = 5
        self.is_running = False
        self.symbol = None
        self.listen_key = None
        self.max_reconnect_attempts = 10
        self.current_reconnect_attempt = 0
    
    def _message_handler(self, _, message):
        try:
            # If message is a string, parse it as JSON
            if isinstance(message, str):
                message = json.loads(message)
                
            # Process different types of messages based on their format
            if isinstance(message, dict):
                # Handle user data events
                if "e" in message:
                    self.logger.debug(f"Received user data event: {message['e']}")
                    if message["e"] == "listenKeyExpired":
                        self.logger.warning("Listen key expired notification received")
                        # Pass this to callback so client code can handle refreshing the listen key
                # Handle combined stream messages
                elif "stream" in message and "data" in message:
                    self.logger.debug(f"Received stream data: {message['stream']}")
                # Keep message format as-is for backwards compatibility
            
            # Forward the message to the callback
            self.on_message_callback(message)
            
        except Exception as e:
            self.logger.error(f"Error handling message: {e}")
            self.logger.debug(f"Raw message: {message}")
    
    def _error_handler(self, _, error):
        self.logger.error(f"WebSocket error: {error}")
        
        # Handle specific Binance error codes
        error_code = None
        if hasattr(error, 'code'):
            error_code = error.code
        elif isinstance(error, dict) and 'code' in error:
            error_code = error['code']
        
        if error_code:
            if error_code == -2021:
                self.logger.warning("Received partial failure error (code -2021)")
            elif error_code == -2022:
                self.logger.error("Received complete failure error (code -2022)")
            elif error_code == 429:
                self.logger.warning("Rate limit exceeded (code 429), backing off before reconnect")
                time.sleep(10)  # Add extra delay for rate limit errors
        
        if self.on_error_callback:
            self.on_error_callback(error)
        
        # Try to reconnect
        self._try_reconnect()
    
    def _try_reconnect(self):
        """Attempt to reconnect WebSocket with exponential backoff"""
        if not self.is_running:
            return
            
        if self.current_reconnect_attempt >= self.max_reconnect_attempts:
            self.logger.error(f"Maximum reconnection attempts ({self.max_reconnect_attempts}) reached. Giving up.")
            return
            
        self.current_reconnect_attempt += 1
        backoff_time = min(60, self.reconnect_delay * (2 ** (self.current_reconnect_attempt - 1)))
        self.logger.info(f"Attempting to reconnect ({self.current_reconnect_attempt}/{self.max_reconnect_attempts}) in {backoff_time} seconds...")
        time.sleep(backoff_time)
        
        try:
            self.stop()
            
            # Recreate necessary WebSocket streams
            if self.symbol:
                self.start_market_stream(self.symbol)
                
            if self.listen_key:
                self.start_user_stream(self.listen_key)
                
            self.logger.info("WebSocket reconnected successfully")
            self.current_reconnect_attempt = 0  # Reset counter on successful reconnect
        except Exception as e:
            self.logger.error(f"Failed to reconnect: {e}")
            self._try_reconnect()  # Try again
    
    def start_market_stream(self, symbol):
        """Start market data streams"""
        self.symbol = symbol
        self.is_running = True
        
        if self.ws_client:
            self.ws_client.stop()
            
        self.ws_client = SpotWebsocketStreamClient(
            on_message=self._message_handler, 
            on_error=self._error_handler,
            is_combined=True  # Use combined streams to save connections
        )
        
        # Connect to needed market streams
        try:
            # Kline/candlestick stream
            self.ws_client.kline(symbol=symbol.lower(), id=1, interval='1m')
            
            # Order book stream - use depth instead of diff_book_depth for full snapshots
            # Speed options: 100ms or 1000ms (1s)
            self.ws_client.diff_book_depth(symbol=symbol.lower(), id=2, speed=100)
            
            # Trade stream for real-time trades
            self.ws_client.trade(symbol=symbol.lower(), id=3)
            
            # Optional: Add bookTicker for best bid/ask price tracking
            self.ws_client.book_ticker(symbol=symbol.lower(), id=4)
            
            self.logger.info(f"WebSocket market streams started for {symbol}")
        except Exception as e:
            self.logger.error(f"Error starting market streams: {e}")
            raise
    
    def start_user_stream(self, listen_key):
        """Start user data stream"""
        if not listen_key:
            self.logger.error("Cannot start user stream: listen_key is required")
            return
            
        self.listen_key = listen_key
        self.is_running = True
        
        try:
            if not self.ws_client:
                self.ws_client = SpotWebsocketStreamClient(
                    on_message=self._message_handler, 
                    on_error=self._error_handler
                )
            
            # Connect to user data stream
            self.ws_client.user_data(listen_key=listen_key, id=100)
            self.logger.info("User data stream started with listen key")
        except Exception as e:
            self.logger.error(f"Error starting user data stream: {e}")
            raise
    
    def stop(self):
        """Stop all WebSocket connections"""
        self.is_running = False
        if self.ws_client:
            try:
                self.ws_client.stop()
                self.logger.info("WebSocket streams stopped")
            except Exception as e:
                self.logger.error(f"Error stopping WebSocket streams: {e}")
            finally:
                self.ws_client = None