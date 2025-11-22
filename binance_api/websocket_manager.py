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

class BookTickerMessage(Struct, kw_only=True):
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

class UserDataMessage(Struct):
    e: str          # Event type (outboundAccountPosition, executionReport, etc.)
    E: int          # Event time
    # All other fields are dynamic and will be passed through

class StandardizedMessage:
    def __init__(self, data):
        for key, value in data.items():
            # Handle nested dictionaries by recursively standardizing
            if isinstance(value, dict):
                value = StandardizedMessage(value)
            setattr(self, key, value)
    
    def get(self, key, default=None):
        """Implement dictionary-like get method for compatibility"""
        return getattr(self, key, default)
    
    # Add additional dictionary-like methods for better compatibility
    def __getitem__(self, key):
        try:
            return getattr(self, key)
        except AttributeError:
            raise KeyError(key)
            
    def __contains__(self, key):
        return hasattr(self, key)

class MarketDataWebsocketManager:
    """
    WebSocket manager for Binance market data streams
    
    This class manages WebSocket connections to Binance market data streams only.
    It does NOT handle WebSocket API functionality, which is managed separately
    by the WebSocketAPIClient class.
    """
    
    def __init__(self, on_message_callback, on_error_callback=None, use_testnet=False):
        """
        Initialize the WebSocket manager for market data streams
        
        Args:
            on_message_callback: Callback function for received messages
            on_error_callback: Callback function for error handling
            use_testnet: Whether to use testnet streams
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
        self.use_testnet = use_testnet
        
        # Set stream URL based on network
        if self.use_testnet:
            self.stream_url = "wss://stream.testnet.binance.vision"
        else:
            self.stream_url = "wss://stream.binance.com:9443"
        
        # Initialize message handlers mapping
        self.message_handlers = {
            'kline': self._handle_kline_message,
            'trade': self._handle_trade_message,
            'aggTrade': self._handle_aggtrade_message,
            'depthUpdate': self._handle_depth_message,
            'outboundAccountPosition': self._handle_account_update,
            'executionReport': self._handle_order_update,
            'listStatus': self._handle_oco_update,
            'balanceUpdate': self._handle_balance_update
        }
    
    def _message_handler(self, _, message):
        """
        Process incoming WebSocket messages
        
        Args:
            _: WebSocket client instance (unused)
            message: The received message
        """
        if not message:
            return
            
        parsed_message = self._parse_message_safely(message)
        if parsed_message:
            self._route_message_to_handler(parsed_message)
    
    def _parse_message_safely(self, message):
        """
        Safely parse a message from the WebSocket
        
        Args:
            message: Raw message to parse
            
        Returns:
            Parsed message object or None if parsing failed
        """
        try:
            # Handle string messages
            if isinstance(message, str):
                # Parse as generic JSON first
                generic_msg = msgspec_json.decode(message)
                
                # Handle combined stream format
                if isinstance(generic_msg, dict) and 'stream' in generic_msg and 'data' in generic_msg:
                    # Process the combined stream format
                    combined_msg = CombinedStreamMessage(
                        stream=generic_msg['stream'], 
                        data=generic_msg['data']
                    )
                    # Return a tuple indicating this is a combined message
                    return ('combined', combined_msg)
                    
                return generic_msg
            else:
                # Already parsed message or non-string input
                return message
                
        except Exception as e:
            self.logger.debug(f"Message parsing failed: {e}")
            if isinstance(message, str) and len(message) < 1000:
                self.logger.debug(f"Raw message: {message}")
            return None
    
    def _route_message_to_handler(self, parsed_message):
        """
        Route parsed message to appropriate handler based on message type
        
        Args:
            parsed_message: Parsed message object
        """
        try:
            # Handle combined stream messages
            if isinstance(parsed_message, tuple) and parsed_message[0] == 'combined':
                combined_msg = parsed_message[1]
                # Pass the combined message to callback
                self.on_message_callback(combined_msg)
                
                # Also process the inner data
                inner_data = combined_msg.data
                self._route_message_to_handler(inner_data)
                return
                
            # Handle regular messages with event types
            if isinstance(parsed_message, dict) and 'e' in parsed_message:
                event_type = parsed_message['e']
                
                # Use mapped handler for the event type
                handler = self.message_handlers.get(event_type)
                if handler:
                    handler(parsed_message)
                else:
                    # Unknown event type, standardize and pass through
                    self.on_message_callback(self._standardize_message(parsed_message))
                return
                
            # Handle bookTicker format which doesn't have 'e' field
            if isinstance(parsed_message, dict) and all(key in parsed_message for key in ['s', 'b', 'B', 'a', 'A']):
                self._handle_bookticker_message(parsed_message)
                return
                
            # Default: pass the message as is
            self.on_message_callback(parsed_message)
            
        except Exception as e:
            self.logger.error(f"Error routing message: {e}")
            # Still try to deliver the message to make sure client receives something
            try:
                self.on_message_callback(parsed_message)
            except:
                pass
    
    def _standardize_message(self, message_dict):
        """
        Standardize message format for consistent handling at higher levels
        
        Args:
            message_dict: Raw dictionary message
            
        Returns:
            Standardized message object with consistent attribute access
        """
        # Convert all dictionary keys to attributes for consistent access
        # This helps main.py to use consistent dot notation regardless of message source
        return StandardizedMessage(message_dict)
    
    # Individual message type handlers
    
    def _handle_kline_message(self, message):
        """Handle kline/candlestick messages"""
        try:
            # Parse with schema for validation and standardization
            parsed = KlineMessage(**message)
            self.on_message_callback(parsed)
        except Exception:
            # Fallback to standardized message
            self.on_message_callback(self._standardize_message(message))
    
    def _handle_trade_message(self, message):
        """Handle trade messages"""
        try:
            parsed = TradeMessage(**message)
            self.on_message_callback(parsed)
        except Exception:
            self.on_message_callback(self._standardize_message(message))
    
    def _handle_aggtrade_message(self, message):
        """Handle aggregate trade messages"""
        try:
            parsed = AggTradeMessage(**message)
            self.on_message_callback(parsed)
        except Exception:
            self.on_message_callback(self._standardize_message(message))
    
    def _handle_depth_message(self, message):
        """Handle order book depth update messages"""
        try:
            parsed = DepthUpdateMessage(**message)
            self.on_message_callback(parsed)
        except Exception:
            self.on_message_callback(self._standardize_message(message))
    
    def _handle_bookticker_message(self, message):
        """Handle book ticker messages"""
        try:
            parsed = BookTickerMessage(**message)
            self.on_message_callback(parsed)
        except Exception:
            self.on_message_callback(self._standardize_message(message))
    
    def _handle_account_update(self, message):
        """Handle account update messages"""
        self.on_message_callback(self._standardize_message(message))
    
    def _handle_order_update(self, message):
        """Handle order update messages"""
        self.on_message_callback(self._standardize_message(message))
    
    def _handle_oco_update(self, message):
        """Handle OCO order update messages"""
        self.on_message_callback(self._standardize_message(message))
    
    def _handle_balance_update(self, message):
        """Handle balance update messages"""
        self.on_message_callback(self._standardize_message(message))
    
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
            stream_url=self.stream_url,
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
                stream_url=self.stream_url,
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
