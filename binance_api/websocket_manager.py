import time
import logging
from binance.websocket.spot.websocket_stream import SpotWebsocketStreamClient
from msgspec import Struct, json as msgspec_json

# Define message schemas for fast parsing of market data streams
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

class AggTradeMessage(Struct):
    e: str          # Event type
    E: int          # Event time
    s: str          # Symbol
    a: int          # Aggregate trade ID
    p: str          # Price
    q: str          # Quantity
    f: int          # First trade ID
    l: int          # Last trade ID
    T: int          # Trade time
    m: bool         # Is buyer market maker
    M: bool         # Ignore

class BookTickerMessage(Struct):
    u: int | None = None  # Update ID, optional with default None
    s: str                # Symbol
    b: str                # Best bid price
    B: str                # Best bid quantity
    a: str                # Best ask price
    A: str                # Best ask quantity

class DepthUpdateMessage(Struct):
    e: str          # Event type
    E: int          # Event time
    s: str          # Symbol
    U: int          # First update ID
    u: int          # Final update ID
    b: list         # Bids to be updated
    a: list         # Asks to be updated

class CombinedStreamMessage(Struct):
    stream: str     # Stream name
    data: object    # Data payload

class MarketDataWebsocketManager:
    """
    WebSocket manager for Binance market data streams
    
    This class manages WebSocket connections to Binance market data streams only.
    It does NOT handle WebSocket API functionality, which is managed separately
    by the WebSocketAPIClient class.
    """
    
    def __init__(self, on_message_callback, on_error_callback=None):
        """
        Initialize the WebSocket manager for market data streams
        
        Args:
            on_message_callback: Callback function for received messages
            on_error_callback: Callback function for error handling
        """
        self.ws_client = None
        self.on_message_callback = on_message_callback
        self.on_error_callback = on_error_callback
        self.logger = logging.getLogger(__name__)
        self.reconnect_delay = 5
        self.is_running = False
        self.symbols = set()  # Track subscribed symbols
        self.stream_types = {}  # Track types of streams for each symbol
        self.listen_key = None
        self.max_reconnect_attempts = 10
        self.current_reconnect_attempt = 0
    
    def _message_handler(self, _, message):
        """
        Process incoming WebSocket messages
        
        Args:
            _: WebSocket client instance (unused)
            message: The received message
        """
        try:
            # Process message based on format and type
            if isinstance(message, str):
                # First try generic parsing to determine message type
                try:
                    generic_msg = msgspec_json.decode(message)
                    
                    # Handle combined stream format
                    if 'stream' in generic_msg and 'data' in generic_msg:
                        # Process the combined stream format
                        combined_msg = CombinedStreamMessage(
                            stream=generic_msg['stream'], 
                            data=generic_msg['data']
                        )
                        self.on_message_callback(combined_msg)
                        
                        # Also process the inner data with proper schema
                        inner_data = generic_msg['data']
                        self._process_market_data_message(inner_data)
                        return
                    
                    # Try to determine message type from structure
                    self._process_market_data_message(generic_msg)
                    
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
    
    def _process_market_data_message(self, msg_data):
        """
        Process market data messages using appropriate schema
        
        Args:
            msg_data: The message data to process
        """
        try:
            # If it's an event type message
            if isinstance(msg_data, dict) and 'e' in msg_data:
                event_type = msg_data['e']
                
                # Use specific schema based on event type
                if event_type == 'kline':
                    parsed = KlineMessage(**msg_data)
                elif event_type == 'trade':
                    parsed = TradeMessage(**msg_data)
                elif event_type == 'aggTrade':
                    parsed = AggTradeMessage(**msg_data)
                elif event_type == 'depthUpdate':
                    parsed = DepthUpdateMessage(**msg_data)
                else:
                    # Unknown event type, use as is
                    parsed = msg_data
                
                self.on_message_callback(parsed)
                return
            
            # Handle bookTicker format which doesn't have 'e' field
            if isinstance(msg_data, dict) and all(key in msg_data for key in ['s', 'b', 'B', 'a', 'A']):
                parsed = BookTickerMessage(**msg_data)
                self.on_message_callback(parsed)
                return
            
            # Default: pass through the message as is
            self.on_message_callback(msg_data)
            
        except Exception as e:
            self.logger.error(f"Error processing market data message: {e}")
            # Pass the original message to ensure callback receives something
            self.on_message_callback(msg_data)
    
    def _error_handler(self, _, error):
        """
        Handle WebSocket errors
        
        Args:
            _: WebSocket client instance (unused)
            error: The error that occurred
        """
        self.logger.error(f"Market Data WebSocket error: {error}")
        
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
            self._reconnect_streams()
            
            self.logger.info("Market Data WebSocket reconnected successfully")
            self.current_reconnect_attempt = 0  # Reset counter on successful reconnect
        except Exception as e:
            self.logger.error(f"Failed to reconnect: {e}")
            self._try_reconnect()  # Try again
    
    def _reconnect_streams(self):
        """Reconnect all previously subscribed streams"""
        if not self.symbols:
            return  # No streams to reconnect
        
        # Create a new client
        self.ws_client = SpotWebsocketStreamClient(
            on_message=self._message_handler, 
            on_error=self._error_handler,
            is_combined=True  # Use combined streams
        )
        
        # Reconnect all previously subscribed streams
        for symbol in self.symbols:
            streams = self.stream_types.get(symbol, {})
            
            if 'kline' in streams:
                interval = streams['kline']
                self.ws_client.kline(symbol=symbol.lower(), interval=interval)
                
            if 'depth' in streams:
                speed = streams['depth']
                self.ws_client.diff_book_depth(symbol=symbol.lower(), speed=speed)
                
            if 'trade' in streams:
                self.ws_client.trade(symbol=symbol.lower())
                
            if 'bookticker' in streams:
                self.ws_client.book_ticker(symbol=symbol.lower())
                
            if 'aggtrade' in streams:
                self.ws_client.agg_trade(symbol=symbol.lower())
        
        # Reconnect user data stream if needed
        if self.listen_key:
            self.ws_client.user_data(listen_key=self.listen_key)
    
    def start_kline_stream(self, symbol, interval='1m'):
        """
        Start kline/candlestick stream for a symbol
        
        Args:
            symbol: Symbol to subscribe to
            interval: Kline interval (default: 1m)
        """
        self._ensure_client_initialized()
        self.symbols.add(symbol)
        
        if symbol not in self.stream_types:
            self.stream_types[symbol] = {}
        
        self.stream_types[symbol]['kline'] = interval
        self.ws_client.kline(symbol=symbol.lower(), interval=interval)
        self.logger.info(f"Started kline stream for {symbol} with {interval} interval")
    
    def start_depth_stream(self, symbol, speed=100):
        """
        Start order book depth stream for a symbol
        
        Args:
            symbol: Symbol to subscribe to
            speed: Update speed in ms (100 or 1000)
        """
        self._ensure_client_initialized()
        self.symbols.add(symbol)
        
        if symbol not in self.stream_types:
            self.stream_types[symbol] = {}
        
        self.stream_types[symbol]['depth'] = speed
        self.ws_client.diff_book_depth(symbol=symbol.lower(), speed=speed)
        self.logger.info(f"Started depth stream for {symbol} with {speed}ms updates")
    
    def start_trade_stream(self, symbol):
        """
        Start trade stream for a symbol
        
        Args:
            symbol: Symbol to subscribe to
        """
        self._ensure_client_initialized()
        self.symbols.add(symbol)
        
        if symbol not in self.stream_types:
            self.stream_types[symbol] = {}
        
        self.stream_types[symbol]['trade'] = True
        self.ws_client.trade(symbol=symbol.lower())
        self.logger.info(f"Started trade stream for {symbol}")
    
    def start_bookticker_stream(self, symbol):
        """
        Start book ticker stream for a symbol
        
        Args:
            symbol: Symbol to subscribe to
        """
        self._ensure_client_initialized()
        self.symbols.add(symbol)
        
        if symbol not in self.stream_types:
            self.stream_types[symbol] = {}
        
        self.stream_types[symbol]['bookticker'] = True
        self.ws_client.book_ticker(symbol=symbol.lower())
        self.logger.info(f"Started book ticker stream for {symbol}")
    
    def start_aggtrade_stream(self, symbol):
        """
        Start aggregate trade stream for a symbol
        
        Args:
            symbol: Symbol to subscribe to
        """
        self._ensure_client_initialized()
        self.symbols.add(symbol)
        
        if symbol not in self.stream_types:
            self.stream_types[symbol] = {}
        
        self.stream_types[symbol]['aggtrade'] = True
        self.ws_client.agg_trade(symbol=symbol.lower())
        self.logger.info(f"Started aggregate trade stream for {symbol}")
    
    def start_user_data_stream(self, listen_key):
        """
        Start user data stream using a listen key
        
        This connects to the user data stream for account updates, order updates, etc.
        Note: The listen key must be obtained separately using REST API or WebSocket API.
        
        Args:
            listen_key: Listen key for user data stream
        """
        if not listen_key:
            self.logger.error("Cannot start user data stream: listen key is required")
            return
            
        self._ensure_client_initialized()
        self.listen_key = listen_key
        
        self.ws_client.user_data(listen_key=listen_key)
        self.logger.info("Started user data stream")
    
    def start_multiple_streams(self, symbol, streams=None):
        """
        Start multiple streams for a symbol at once
        
        Args:
            symbol: Symbol to subscribe to
            streams: List of streams to start, e.g., ['kline_1m', 'depth', 'trade', 'bookticker']
        """
        if not streams:
            streams = ['kline_1m', 'depth', 'trade', 'bookticker']
        
        self._ensure_client_initialized()
        self.symbols.add(symbol)
        
        if symbol not in self.stream_types:
            self.stream_types[symbol] = {}
        
        for stream in streams:
            if stream.startswith('kline_'):
                interval = stream.split('_')[1]
                self.stream_types[symbol]['kline'] = interval
                self.ws_client.kline(symbol=symbol.lower(), interval=interval)
            elif stream == 'depth':
                self.stream_types[symbol]['depth'] = 100  # Default to 100ms
                self.ws_client.diff_book_depth(symbol=symbol.lower(), speed=100)
            elif stream == 'trade':
                self.stream_types[symbol]['trade'] = True
                self.ws_client.trade(symbol=symbol.lower())
            elif stream == 'bookticker':
                self.stream_types[symbol]['bookticker'] = True
                self.ws_client.book_ticker(symbol=symbol.lower())
            elif stream == 'aggtrade':
                self.stream_types[symbol]['aggtrade'] = True
                self.ws_client.agg_trade(symbol=symbol.lower())
        
        self.logger.info(f"Started multiple streams for {symbol}: {streams}")
    
    def _ensure_client_initialized(self):
        """Ensure WebSocket client is initialized"""
        if not self.ws_client:
            self.ws_client = SpotWebsocketStreamClient(
                on_message=self._message_handler, 
                on_error=self._error_handler,
                is_combined=True  # Use combined streams to save connections
            )
            self.is_running = True
    
    def stop(self):
        """Stop all WebSocket connections"""
        self.is_running = False
        if self.ws_client:
            try:
                self.ws_client.stop()
                self.logger.info("Market Data WebSocket streams stopped")
            except Exception as e:
                self.logger.error(f"Error stopping WebSocket streams: {e}")
            finally:
                self.ws_client = None