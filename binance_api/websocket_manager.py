import time
import logging
from binance.websocket.spot.websocket_stream import SpotWebsocketStreamClient
from msgspec import Struct, json as msgspec_json

# Define message schemas for fast parsing
class KlineData(Struct):
    t: int          # Kline start time
    T: int          # Kline close time
    s: str          # Symbol
    i: str          # Interval
    f: int          # First trade ID
    L: int          # Last trade ID
    o: str          # Open price
    c: str          # Close price
    h: str          # High price
    l: str          # Low price
    v: str          # Volume
    n: int          # Number of trades
    x: bool         # Is closed
    q: str          # Quote volume
    V: str          # Taker buy volume
    Q: str          # Taker buy quote volume
    B: str          # Ignore

class KlineMessage(Struct):
    e: str          # Event type
    E: int          # Event time
    s: str          # Symbol
    k: KlineData    # Kline data

class TradeMessage(Struct):
    e: str          # Event type
    E: int          # Event time
    s: str          # Symbol
    t: int          # Trade ID
    p: str          # Price
    q: str          # Quantity
    b: int          # Buyer order ID
    a: int          # Seller order ID
    T: int          # Trade time
    m: bool         # Is buyer market maker
    M: bool         # Ignore

class ExecutionReportMessage(Struct):
    e: str          # Event type
    E: int          # Event time
    s: str          # Symbol
    c: str          # Client order ID
    S: str          # Side
    o: str          # Order type
    f: str          # Time in force
    q: str          # Order quantity
    p: str          # Order price
    P: str          # Stop price
    F: str          # Iceberg quantity
    g: int          # Order listen key
    C: str          # Original client order ID
    x: str          # Current execution type
    X: str          # Current order status
    r: str          # Order reject reason
    i: int          # Order ID
    l: str          # Last executed quantity
    z: str          # Cumulative filled quantity
    L: str          # Last executed price
    n: str          # Commission amount
    N: str          # Commission asset
    T: int          # Transaction time
    t: int          # Trade ID
    I: int          # Ignore
    w: bool         # Is working
    m: bool         # Is maker
    M: bool         # Ignore
    O: int          # Order creation time
    Z: str          # Cumulative quote asset transacted quantity
    Y: str          # Last quote asset transacted quantity
    Q: str          # Quote order quantity

class ListStatusMessage(Struct):
    e: str          # Event type
    E: int          # Event time
    s: str          # Symbol
    g: int          # OrderListId
    c: str          # Contingency type
    l: str          # List status type
    L: str          # List order status
    r: str          # List reject reason
    C: str          # List client order ID
    T: int          # Transaction time
    O: list         # Orders

class BookTickerMessage(Struct):
    s: str                # Symbol
    b: str                # Best bid price
    B: str                # Best bid quantity
    a: str                # Best ask price
    A: str                # Best ask quantity
    u: int | None = None  # Update ID, now optional with default None

class CombinedStreamMessage(Struct):
    stream: str     # Stream name
    data: object    # Data payload


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
            # Process message based on format and type
            if isinstance(message, str):
                # Always try generic parsing first to check message format
                try:
                    generic_msg = msgspec_json.decode(message)
                    
                    # Handle special message types that don't follow event format
                    # Connection messages, status updates, and heartbeats
                    if any(key in generic_msg for key in ['ping', 'pong', 'welcome', 'action', 'result', 'id', 'status']):
                        self.on_message_callback(generic_msg)
                        return
                        
                    # Detect combined stream format
                    if 'stream' in generic_msg and 'data' in generic_msg:
                        # Process the combined stream format
                        self.on_message_callback(CombinedStreamMessage(
                            stream=generic_msg['stream'], 
                            data=generic_msg['data']
                        ))
                        # Also process the inner data
                        self._message_handler(_, msgspec_json.encode(generic_msg['data']))
                        return
                        
                    # Only continue with schema validation if we have an event type
                    if 'e' in generic_msg:
                        # Try using the appropriate schema based on event type
                        event_type = generic_msg['e']
                        if event_type == 'kline':
                            parsed = msgspec_json.decode(message, type=KlineMessage)
                        elif event_type == 'trade':
                            parsed = msgspec_json.decode(message, type=TradeMessage)
                        elif event_type == 'executionReport':
                            parsed = msgspec_json.decode(message, type=ExecutionReportMessage)
                        elif event_type == 'listStatus':
                            parsed = msgspec_json.decode(message, type=ListStatusMessage)
                        else:
                            # Unknown event type, use generic parsing
                            parsed = generic_msg
                            
                        self.on_message_callback(parsed)
                        return
                        
                    # Handle bookTicker format which doesn't have 'e' field
                    if all(key in generic_msg for key in ['s', 'b', 'B', 'a', 'A']):
                        parsed = msgspec_json.decode(message, type=BookTickerMessage)
                        self.on_message_callback(parsed)
                        return
                        
                    # If we got here, it's a message format we don't have a specific schema for
                    self.on_message_callback(generic_msg)
                    
                except Exception as decode_error:
                    self.logger.debug(f"Schema parsing failed: {decode_error}, using generic parsing")
                    # Fallback to completely generic parsing
                    parsed = msgspec_json.decode(message)
                    self.on_message_callback(parsed)
            else:
                # Already parsed message or non-string input
                self.on_message_callback(message)
                
        except Exception as e:
            self.logger.error(f"Error handling message: {e}")
            if isinstance(message, str) and len(message) < 1000:  # Only log if message is reasonably sized
                self.logger.debug(f"Raw message that caused error: {message}")
    
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