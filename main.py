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
from binance_api.websocket_manager import WebsocketManager
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
            # Handle different types of messages
            event_type = getattr(message, 'e', None)
            
            if event_type:
                if event_type == 'kline':
                    # Kline update
                    symbol = message.s
                    # Access close price through the kline data structure
                    price = float(message.k.c)
                    
                    if symbol == config.SYMBOL:
                        # Check risk management, only update trailing logic
                        if self.risk_manager and self.risk_manager.is_active:
                            self.risk_manager.check_price(price)
                
                elif event_type == 'executionReport':
                    # Order execution report
                    # Convert schema object to dict for compatibility
                    order_data = {
                        'X': message.X,  # Order status
                        'i': message.i,  # Order ID
                        's': message.s,  # Symbol
                        'S': message.S,  # Side
                        'p': message.p,  # Price
                        'q': message.q,  # Quantity
                    }
                    self.grid_trader.handle_order_update(order_data)
                    
                elif event_type == 'listStatus':
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
                
            # Handle raw dict messages (fallback)
            elif isinstance(message, dict):
                # Handle the original dict format for backward compatibility
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
            logger.debug(f"Message: {message}")
        
    def _handle_oco_update(self, message):
        """Handle OCO order updates"""
        try:
            if message['L'] == 'EXECUTING' or message['L'] == 'ALL_DONE':
                symbol = message['s']
                order_list_id = message['i']
                
                # Check if this is our risk management OCO order
                if (self.risk_manager and self.risk_manager.is_active and 
                    self.risk_manager.oco_order_id == order_list_id):
                    
                    if message['L'] == 'ALL_DONE':
                        logger.info(f"Risk management OCO order executed: {order_list_id}")
                        
                        # Check which side was executed
                        if message['l'] == 'STOP_LOSS_LIMIT':
                            logger.info("Stop loss order executed")
                            if self.telegram_bot:
                                self.telegram_bot.send_message("Stop loss order executed, grid trading stopped")
                            # Stop grid trading
                            self.grid_trader.stop()
                        elif message['l'] == 'LIMIT_MAKER':
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
        
        # Try to reconnect
        if self.is_running:
            logger.info("Attempting to reconnect WebSocket...")
            time.sleep(5)
            self._setup_websocket()
    
    def _setup_websocket(self):
        """Set up WebSocket connection"""
        try:
            # Stop existing connection
            if self.ws_manager:
                self.ws_manager.stop()
            
            # Create new connection
            self.ws_manager = WebsocketManager(
                on_message_callback=self._handle_websocket_message,
                on_error_callback=self._websocket_error_handler
            )
            
            # Start market data stream
            self.ws_manager.start_market_stream(config.SYMBOL)
            
            # Start user data stream
            try:
                # Create listenKey
                response = self.binance_client.client.new_listen_key()
                listen_key = response['listenKey']
                
                # Start user data stream
                self.ws_manager.start_user_stream(listen_key)
                
                # Periodically extend listenKey validity
                def keep_alive_listen_key():
                    while self.is_running:
                        try:
                            self.binance_client.client.renew_listen_key(listen_key)
                            logger.debug(f"Extended listenKey validity: {listen_key}")
                        except Exception as e:
                            logger.error(f"Failed to extend listenKey: {e}")
                        
                        # Update every 30 minutes
                        time.sleep(30 * 60)
                
                # Start keep-alive thread
                Thread(target=keep_alive_listen_key, daemon=True).start()
                
            except Exception as e:
                logger.error(f"Failed to start user data stream: {e}")
            
        except Exception as e:
            logger.error(f"Failed to set up WebSocket: {e}")
    
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
        
        # Stop Telegram bot
        if self.telegram_bot:
            self.telegram_bot.send_message("Grid trading bot stopped")
            self.telegram_bot.stop()
        
        logger.info("Grid Trading Bot stopped")

if __name__ == "__main__":
    bot = GridTradingBot()
    bot.start()