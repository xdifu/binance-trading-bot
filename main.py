import logging
import time
from threading import Thread
from datetime import datetime, timedelta
import sys
import os

# Add project root directory to system path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import custom modules
from binance_api.client import BinanceClient
from binance_api.websocket_manager import MarketDataWebsocketManager  # Updated import
from core.grid_trader import GridTrader
from core.risk_manager import RiskManager
from tg_bot.bot import TelegramBot
import config

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('grid_bot.log')
    ]
)

logger = logging.getLogger(__name__)

class GridTradingBot:
    def __init__(self):
        self.binance_client = BinanceClient()
        self.telegram_bot = None
        self.grid_trader = None
        self.risk_manager = None
        self.ws_manager = None
        self.is_running = False
        self.listen_key = None
        self.keep_alive_thread = None
        
        # Initialize submodules
        self._init_modules()
    
    def _init_modules(self):
        """Initialize each module"""
        # Initialize Telegram bot
        try:
            if config.TELEGRAM_TOKEN and config.ALLOWED_TELEGRAM_USERS:
                self.telegram_bot = TelegramBot(
                    token=config.TELEGRAM_TOKEN,
                    allowed_users=config.ALLOWED_TELEGRAM_USERS
                )
                logger.info("Telegram bot initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize Telegram bot: {e}")
        
        # Initialize grid trading strategy
        self.grid_trader = GridTrader(
            binance_client=self.binance_client,
            telegram_bot=self.telegram_bot
        )
        logger.info("Grid trading strategy initialized successfully")
        
        # Initialize risk management
        self.risk_manager = RiskManager(
            binance_client=self.binance_client,
            telegram_bot=self.telegram_bot
        )
        logger.info("Risk management module initialized successfully")
        
        # Update Telegram bot references
        if self.telegram_bot:
            self.telegram_bot.grid_trader = self.grid_trader
            self.telegram_bot.risk_manager = self.risk_manager

    def _handle_websocket_message(self, message):
        """Process WebSocket messages"""
        try:
            # Handle different types of messages with msgspec structured objects
            
            # Handle kline events
            if hasattr(message, 'e') and message.e == 'kline':
                symbol = message.s
                # Access close price through the kline data structure
                price = float(message.k.c)
                
                if symbol == config.SYMBOL:
                    # Check risk management, only update trailing logic
                    if self.risk_manager and self.risk_manager.is_active:
                        self.risk_manager.check_price(price)
            
            # Handle user data events
            elif hasattr(message, 'e') and message.e == 'executionReport':
                # Convert structured object to dict for compatibility with grid_trader
                order_data = {
                    'X': message.X,  # Order status
                    'i': message.i,  # Order ID
                    's': message.s,  # Symbol
                    'S': message.S,  # Side
                    'p': message.p,  # Price
                    'q': message.q,  # Quantity
                }
                self.grid_trader.handle_order_update(order_data)
                
            elif hasattr(message, 'e') and message.e == 'listStatus':
                # OCO order status update
                list_data = {
                    'L': message.L,  # List order status
                    's': message.s,  # Symbol
                    'i': message.g,  # Order list ID
                    'l': message.l,  # List status type
                }
                self._handle_oco_update(list_data)
            
            # Handle combined stream messages
            elif hasattr(message, 'stream') and hasattr(message, 'data'):
                # Process the data payload from combined stream
                nested_data = message.data
                self._handle_websocket_message(nested_data)
            
            # Handle book ticker messages (which don't have event type field)
            elif hasattr(message, 's') and hasattr(message, 'b') and hasattr(message, 'a'):
                # This is a book ticker message
                symbol = message.s
                if symbol == config.SYMBOL:
                    # Could process best bid/ask data here if needed
                    pass
                
            # Handle dict messages (fallback for compatibility)
            elif isinstance(message, dict):
                if 'e' in message:
                    if message['e'] == 'kline':
                        symbol = message['s']
                        price = float(message['k']['c'])
                        
                        if symbol == config.SYMBOL:
                            if self.risk_manager and self.risk_manager.is_active:
                                self.risk_manager.check_price(price)
                    
                    elif message['e'] == 'executionReport':
                        self.grid_trader.handle_order_update(message)
                        
                    elif message['e'] == 'listStatus':
                        self._handle_oco_update(message)
                
        except Exception as e:
            logger.error(f"Failed to process WebSocket message: {e}")
            if hasattr(message, '__dict__'):
                logger.debug(f"Message attributes: {message.__dict__}")
            else:
                logger.debug(f"Message type: {type(message)}")
        
    def _handle_oco_update(self, message):
        """Handle OCO order updates"""
        try:
            # Check if using dict or object access
            if isinstance(message, dict):
                status = message.get('L')
                symbol = message.get('s')
                order_list_id = message.get('i')
                list_type = message.get('l')
            else:
                status = message.L
                symbol = message.s
                order_list_id = message.i
                list_type = message.l
                
            if status == 'EXECUTING' or status == 'ALL_DONE':
                # Check if this is our risk management OCO order
                if (self.risk_manager and self.risk_manager.is_active and 
                    self.risk_manager.oco_order_id == order_list_id):
                    
                    if status == 'ALL_DONE':
                        logger.info(f"Risk management OCO order executed: {order_list_id}")
                        
                        # Check which side was executed
                        if list_type == 'STOP_LOSS_LIMIT':
                            logger.info("Stop loss order executed")
                            if self.telegram_bot:
                                self.telegram_bot.send_message("Stop loss order executed, grid trading stopped")
                            # Stop grid trading
                            self.grid_trader.stop()
                        elif list_type == 'LIMIT_MAKER':
                            logger.info("Take profit order executed")
                            if self.telegram_bot:
                                self.telegram_bot.send_message("Take profit order executed, grid trading stopped")
                            # Stop grid trading
                            self.grid_trader.stop()
                        
                        # Reset OCO order ID
                        self.risk_manager.oco_order_id = None
        except Exception as e:
            logger.error(f"Failed to process OCO order update: {e}")

    def _websocket_error_handler(self, error):
        """WebSocket error handler"""
        logger.error(f"WebSocket error: {error}")
        
        # The MarketDataWebsocketManager now handles reconnection logic internally,
        # so we don't need to handle it here.
        pass
    
    def _setup_websocket(self):
        """Set up WebSocket connection"""
        try:
            # Stop existing connection
            if self.ws_manager:
                self.ws_manager.stop()
            
            # Create new MarketDataWebsocketManager
            self.ws_manager = MarketDataWebsocketManager(
                on_message_callback=self._handle_websocket_message,
                on_error_callback=self._websocket_error_handler
            )
            
            # Start kline stream for real-time price updates
            self.ws_manager.start_kline_stream(symbol=config.SYMBOL, interval='1m')
            
            # Optional: Start additional streams as needed
            self.ws_manager.start_bookticker_stream(symbol=config.SYMBOL)
            
            # Start user data stream
            self._setup_user_data_stream()
            
        except Exception as e:
            logger.error(f"Failed to set up WebSocket: {e}")
    
    def _setup_user_data_stream(self):
        """Set up user data stream for order updates"""
        try:
            # Only proceed if we have a WebSocket manager
            if not self.ws_manager:
                logger.error("Cannot set up user data stream: WebSocket manager not initialized")
                return
                
            # Get a listen key using the WebSocket API client if available, otherwise use REST
            client_status = self.binance_client.get_client_status()
            
            if client_status["websocket_available"]:
                # WebSocket API is available
                listen_key_response = self.binance_client.ws_client.client.start_user_data_stream()
                if listen_key_response and listen_key_response.get('status') == 200:
                    self.listen_key = listen_key_response['result']['listenKey']
                else:
                    logger.error("Failed to get listen key from WebSocket API")
                    return
            else:
                # Fallback to REST API
                listen_key_response = self.binance_client.rest_client.new_listen_key()
                if listen_key_response and 'listenKey' in listen_key_response:
                    self.listen_key = listen_key_response['listenKey']
                else:
                    logger.error("Failed to get listen key from REST API")
                    return
            
            # Start user data stream with the listen key
            self.ws_manager.start_user_data_stream(self.listen_key)
            logger.info("User data stream started successfully")
            
            # Start keep-alive thread
            self._start_listen_key_keep_alive()
            
        except Exception as e:
            logger.error(f"Failed to set up user data stream: {e}")
    
    def _start_listen_key_keep_alive(self):
        """Start a thread to keep the listen key alive"""
        if self.keep_alive_thread and self.keep_alive_thread.is_alive():
            return  # Thread already running
            
        def keep_alive_listen_key():
            """Thread function to extend listen key validity periodically"""
            while self.is_running and self.listen_key:
                try:
                    time.sleep(30 * 60)  # Wait 30 minutes
                    
                    if not self.is_running or not self.listen_key:
                        break
                        
                    # Extend listen key validity
                    client_status = self.binance_client.get_client_status()
                    
                    if client_status["websocket_available"]:
                        # Use WebSocket API
                        self.binance_client.ws_client.client.ping_user_data_stream(self.listen_key)
                    else:
                        # Fallback to REST API
                        self.binance_client.rest_client.renew_listen_key(self.listen_key)
                        
                    logger.debug(f"Extended listenKey validity: {self.listen_key[:5]}...")
                    
                except Exception as e:
                    logger.error(f"Failed to extend listenKey: {e}")
                    # Try to get a new listen key if the current one is invalid
                    try:
                        self._setup_user_data_stream()
                        break  # Exit this thread as a new one will be started
                    except:
                        pass
        
        # Start keep-alive thread
        self.keep_alive_thread = Thread(target=keep_alive_listen_key, daemon=True)
        self.keep_alive_thread.start()
    
    def _grid_maintenance_thread(self):
        """Grid maintenance thread"""
        last_check = datetime.now()
        
        while self.is_running:
            try:
                now = datetime.now()
                
                # Check grid recalculation once per day
                if (now - last_check).total_seconds() > 24 * 60 * 60:
                    self.grid_trader.check_grid_recalculation()
                    last_check = now
                
                # Short sleep
                time.sleep(60)
            except Exception as e:
                logger.error(f"Grid maintenance failed: {e}")
                time.sleep(60)
    
    def start(self):
        """Start the bot"""
        if self.is_running:
            logger.info("Bot is already running")
            return
        
        self.is_running = True
        logger.info("Starting Grid Trading Bot")
        
        # Start Telegram bot
        if self.telegram_bot:
            self.telegram_bot.start()
            self.telegram_bot.send_message("Grid trading bot started. Use /help to see commands")
        
        # Set up WebSocket connection
        self._setup_websocket()
        
        # Start grid maintenance thread
        Thread(target=self._grid_maintenance_thread, daemon=True).start()
        
        # Main loop
        try:
            while self.is_running:
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Received interrupt signal, stopping...")
            self.stop()
    
    def stop(self):
        """Stop the bot"""
        if not self.is_running:
            return
        
        self.is_running = False
        logger.info("Stopping Grid Trading Bot")
        
        # Stop grid trading
        if self.grid_trader and self.grid_trader.is_running:
            self.grid_trader.stop()
        
        # Stop risk management
        if self.risk_manager and self.risk_manager.is_active:
            self.risk_manager.deactivate()
        
        # Stop WebSocket connection
        if self.ws_manager:
            self.ws_manager.stop()
        
        # Stop user data stream if needed
        if self.listen_key:
            try:
                client_status = self.binance_client.get_client_status()
                if client_status["websocket_available"]:
                    self.binance_client.ws_client.client.stop_user_data_stream(self.listen_key)
                else:
                    self.binance_client.rest_client.close_listen_key(self.listen_key)
            except Exception as e:
                logger.error(f"Failed to close listen key: {e}")
        
        # Stop Telegram bot
        if self.telegram_bot:
            self.telegram_bot.send_message("Grid trading bot stopped")
            self.telegram_bot.stop()
        
        logger.info("Grid Trading Bot stopped")

if __name__ == "__main__":
    bot = GridTradingBot()
    bot.start()