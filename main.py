import logging
import time
import threading
from threading import Thread, RLock
from datetime import datetime
import sys
import os

# Add project root directory to system path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import custom modules
from binance_api.client import BinanceClient
from binance_api.websocket_manager import MarketDataWebsocketManager
from core.grid_trader import GridTrader
from core.risk_manager import RiskManager
from tg_bot.bot import TelegramBot
import config

# Configuration constants
LISTEN_KEY_RENEWAL_INTERVAL = 30 * 60  # 30 minutes in seconds
GRID_RECALCULATION_INTERVAL = 24 * 60 * 60  # 24 hours in seconds
WEBSOCKET_RECONNECT_INTERVAL = 60  # 60 seconds
MAINTENANCE_THREAD_SLEEP = 60  # 60 seconds

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
        """Initialize the trading bot with all necessary components"""
        self.binance_client = BinanceClient()
        self.telegram_bot = None
        self.grid_trader = None
        self.risk_manager = None
        self.ws_manager = None
        self.listen_key = None
        self.keep_alive_thread = None
        self.logger = logging.getLogger(__name__)
        
        # Initialize state management
        self.state_lock = RLock()
        with self.state_lock:
            self.is_running = False
        
        # Initialize submodules
        self._init_modules()
    
    def _init_modules(self):
        """Initialize each module of the trading system"""
        try:
            # Initialize Telegram bot if credentials are available
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
            telegram_bot=self.telegram_bot,
            grid_trader=self.grid_trader  # Add reference to grid_trader
        )
        logger.info("Risk management module initialized successfully")
        
        # Update Telegram bot references
        if self.telegram_bot:
            self.telegram_bot.grid_trader = self.grid_trader
            self.telegram_bot.risk_manager = self.risk_manager

    def _handle_websocket_message(self, message):
        """Process WebSocket messages with focus on business logic only"""
        try:
            # Handle kline events for price updates
            if (hasattr(message, 'e') and message.e == 'kline' and 
                hasattr(message, 'k') and hasattr(message.k, 'c') and 
                hasattr(message, 's')):  # Added check for 's' attribute
                
                symbol = message.s
                price = float(message.k.c)
                
                if symbol == config.SYMBOL:
                    # Check risk management conditions if active
                    if self.risk_manager and self.risk_manager.is_active:
                        self.risk_manager.check_price(price)
            
            # Handle execution reports for order updates
            elif hasattr(message, 'e') and message.e == 'executionReport':
                self.grid_trader.handle_order_update(message)
                
            # Handle order list status updates (OCO orders)
            elif hasattr(message, 'e') and message.e == 'listStatus':
                self._handle_oco_update(message)
                
        except AttributeError:
            # Handle dict-format messages as fallback
            if isinstance(message, dict):
                if ('e' in message and message['e'] == 'kline' and 
                    'k' in message and 'c' in message.get('k', {}) and
                    's' in message):  # Added check for 's' key
                    
                    symbol = message['s']
                    price = float(message['k']['c'])
                    
                    if symbol == config.SYMBOL:
                        if self.risk_manager and self.risk_manager.is_active:
                            self.risk_manager.check_price(price)
                
                elif 'e' in message and message['e'] == 'executionReport':
                    self.grid_trader.handle_order_update(message)
                    
                elif 'e' in message and message['e'] == 'listStatus':
                    self._handle_oco_update(message)
        except Exception as e:
            self.logger.error(f"Failed to process WebSocket message: {e}")
        
    def _handle_oco_update(self, message):
        """Handle OCO order updates with standardized access pattern"""
        try:
            # Extract properties safely regardless of object or dict format
            status = getattr(message, 'L', None) if hasattr(message, 'L') else message.get('L')
            order_list_id = getattr(message, 'i', None) if hasattr(message, 'i') else message.get('i')
            list_type = getattr(message, 'l', None) if hasattr(message, 'l') else message.get('l')
                
            if status in ('EXECUTING', 'ALL_DONE'):
                # Check if this is our risk management OCO order
                if (self.risk_manager and self.risk_manager.is_active and 
                    self.risk_manager.oco_order_id == order_list_id):
                    
                    if status == 'ALL_DONE':
                        logger.info(f"Risk management OCO order executed: {order_list_id}")
                        
                        # Check which side was executed and take appropriate action
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

    def _handle_account_position_update(self, message):
        """
        Handle account position updates from WebSocket stream.
        Detects when balances exceed thresholds and triggers grid/OCO order checks.
        
        Args:
            message: Account position update message from WebSocket
        """
        try:
            # Extract trading pair assets
            base_asset = self.grid_trader.symbol.replace('USDT', '')
            quote_asset = 'USDT'
            
            # Extract balances from message (handling both object and dict formats)
            balances = []
            if hasattr(message, 'B'):
                balances = message.B
            elif isinstance(message, dict) and 'B' in message:
                balances = message['B']
            
            # Track if relevant assets exceed threshold
            check_grid = False
            check_oco = False
            
            # Process each balance update
            for balance_item in balances:
                # Extract asset and free amount (with object/dict format handling)
                asset = getattr(balance_item, 'a', None) if hasattr(balance_item, 'a') else balance_item.get('a')
                free_amount = 0
                if hasattr(balance_item, 'f'):
                    free_amount = float(balance_item.f)
                elif isinstance(balance_item, dict) and 'f' in balance_item:
                    free_amount = float(balance_item['f'])
                
                # Check USDT for grid orders
                if asset == quote_asset and free_amount >= config.CAPITAL_PER_LEVEL:
                    self.logger.info(f"Balance update: Detected {free_amount} {quote_asset}, checking for unfilled grid slots")
                    check_grid = True
                
                # Check base asset for OCO orders
                elif asset == base_asset and free_amount > 0:
                    self.logger.info(f"Balance update: Detected {free_amount} {base_asset}, checking for missing OCO orders")
                    check_oco = True
            
            # Use separate threads to avoid blocking WebSocket processing
            if check_grid and self.grid_trader and self.grid_trader.is_running:
                threading.Thread(
                    target=self.grid_trader._check_for_unfilled_grid_slots,
                    daemon=True
                ).start()
                
            if check_oco and self.risk_manager and self.risk_manager.is_active:
                threading.Thread(
                    target=self.risk_manager._check_for_missing_oco_orders,
                    daemon=True
                ).start()
                
        except Exception as e:
            self.logger.error(f"Error processing account position update: {e}")

    def _websocket_error_handler(self, error):
        """Handle WebSocket errors"""
        logger.error(f"WebSocket error: {error}")
    
    def _setup_websocket(self):
        """Set up WebSocket connection with reconnection support"""
        try:
            # Stop existing connection
            if self.ws_manager:
                self.ws_manager.stop()
            
            # Create new MarketDataWebsocketManager
            self.ws_manager = MarketDataWebsocketManager(
                on_message_callback=self._handle_websocket_message,
                on_error_callback=self._websocket_error_handler
            )
            
            # Start necessary data streams
            self.ws_manager.start_kline_stream(symbol=config.SYMBOL, interval='1m')
            self.ws_manager.start_bookticker_stream(symbol=config.SYMBOL)
            
            # Start user data stream
            self._setup_user_data_stream()
            
        except Exception as e:
            logger.error(f"Failed to set up WebSocket: {e}")
            
            # Schedule a reconnection attempt
            reconnect_thread = Thread(
                target=self._delayed_websocket_reconnect,
                args=(WEBSOCKET_RECONNECT_INTERVAL,),
                daemon=True
            )
            reconnect_thread.start()
    
    def _delayed_websocket_reconnect(self, delay_seconds):
        """Attempt to reconnect WebSocket after a delay"""
        try:
            time.sleep(delay_seconds)
            if self.is_running:
                logger.info(f"Attempting WebSocket reconnection after {delay_seconds}s delay")
                self._setup_websocket()
        except Exception as e:
            logger.error(f"Failed to reconnect WebSocket: {e}")
    
    def _setup_user_data_stream(self):
        """Set up user data stream for order updates with robust error handling"""
        try:
            # Only proceed if we have a WebSocket manager
            if not self.ws_manager:
                logger.error("Cannot set up user data stream: WebSocket manager not initialized")
                return
                
            # Get client status to determine API availability
            client_status = self.binance_client.get_client_status()
            listen_key_response = None
            
            # Try WebSocket API first, fall back to REST
            if client_status["websocket_available"]:
                try:
                    listen_key_response = self.binance_client.ws_client.client.start_user_data_stream()
                    if listen_key_response and listen_key_response.get('status') == 200:
                        self.listen_key = listen_key_response['result']['listenKey']
                    else:
                        logger.warning("WebSocket API listen key request failed, falling back to REST")
                        listen_key_response = None
                except Exception as e:
                    logger.warning(f"Failed to get listen key from WebSocket API: {e}")
            
            # Fallback to REST API if needed
            if not listen_key_response or not self.listen_key:
                try:
                    listen_key_response = self.binance_client.rest_client.new_listen_key()
                    if listen_key_response and 'listenKey' in listen_key_response:
                        self.listen_key = listen_key_response['listenKey']
                    else:
                        logger.error("Failed to get listen key from REST API")
                        return
                except Exception as e:
                    logger.error(f"Failed to get listen key from REST API: {e}")
                    return
            
            # Start user data stream with the listen key
            self.ws_manager.start_user_data_stream(self.listen_key)
            logger.info("User data stream started successfully")
            
            # Start keep-alive thread with thread safety
            with self.state_lock:
                if not self.keep_alive_thread or not self.keep_alive_thread.is_alive():
                    self.keep_alive_thread = Thread(target=self._keep_alive_listen_key_thread, daemon=True)
                    self.keep_alive_thread.start()
            
        except Exception as e:
            logger.error(f"Failed to set up user data stream: {e}")
    
    def _keep_alive_listen_key_thread(self):
        """Thread function to keep the listen key alive with improved thread safety"""
        while True:
            try:
                # Check run state with thread safety
                with self.state_lock:
                    if not self.is_running or not self.listen_key:
                        break
                
                # Sleep first to avoid immediate ping after getting a new key
                time.sleep(LISTEN_KEY_RENEWAL_INTERVAL)
                
                # Get current listen key with thread safety and check state again
                current_listen_key = None
                with self.state_lock:
                    if not self.is_running:
                        break
                    current_listen_key = self.listen_key
                    
                if not current_listen_key:
                    break
                        
                # Extend listen key validity
                client_status = self.binance_client.get_client_status()
                
                # Final check before network operation
                with self.state_lock:
                    if not self.is_running:
                        break
                
                if client_status["websocket_available"]:
                    # Use WebSocket API
                    self.binance_client.ws_client.client.ping_user_data_stream(current_listen_key)
                else:
                    # Fallback to REST API
                    self.binance_client.rest_client.renew_listen_key(current_listen_key)
                    
                logger.debug(f"Extended listenKey validity: {current_listen_key[:5]}...")
                
            except Exception as e:
                logger.error(f"Failed to extend listenKey: {e}")
                
                # Try to get a new listen key if the current one is invalid
                try:
                    # Check if we should retry
                    with self.state_lock:
                        if not self.is_running:
                            break
                    
                    time.sleep(5)  # Brief delay before retry
                    
                    # Check again after delay
                    with self.state_lock:
                        if self.is_running:  # Only retry if still running
                            self._setup_user_data_stream()
                    break  # Exit this thread as a new one will be started
                except Exception as retry_error:
                    logger.error(f"Failed to recover listen key: {retry_error}")
                    time.sleep(60)  # Wait longer before next attempt
    
    def _grid_maintenance_thread(self):
        """Grid maintenance thread with improved timing precision and unfilled slot checking"""
        last_grid_check = datetime.now()
        last_unfilled_check = datetime.now()
        last_oco_check = datetime.now()
        last_ws_check = datetime.now()  # Add new timestamp for WebSocket checks
        
        while True:
            try:
                # Check run state with thread safety
                with self.state_lock:
                    if not self.is_running:
                        break
            
                now = datetime.now()
                
                # Check grid recalculation using configuration constant
                if (now - last_grid_check).total_seconds() > GRID_RECALCULATION_INTERVAL:
                    self.grid_trader.check_grid_recalculation()
                    last_grid_check = now
                
                # Check for unfilled grid slots every 15 minutes
                if (now - last_unfilled_check).total_seconds() > 15 * 60:  # 15 minutes
                    self.grid_trader._check_for_unfilled_grid_slots()
                    last_unfilled_check = now
                
                # Check for missing OCO orders every 5 minutes
                if (now - last_oco_check).total_seconds() > 5 * 60:  # 5 minutes
                    if self.risk_manager and self.risk_manager.is_active:
                        self.risk_manager._check_for_missing_oco_orders()
                    last_oco_check = now
                
                # NEW: Check WebSocket connection every 2 minutes
                if (now - last_ws_check).total_seconds() > 2 * 60:  # 2 minutes
                    if not ensure_websocket_connection(self.binance_client):
                        logger.error("WebSocket connection check failed, attempting reconnection")
                        self._setup_websocket()
                    last_ws_check = now
            
                # Short sleep to allow for timely shutdown
                time.sleep(MAINTENANCE_THREAD_SLEEP)
            except Exception as e:
                logger.error(f"Grid maintenance failed: {e}")
                time.sleep(MAINTENANCE_THREAD_SLEEP)
    
    def _auto_start_grid_trading(self):
        """
        Automatically start grid trading without requiring Telegram command.
        This ensures the bot can start trading immediately after system startup or restart.
        """
        try:
            if not self.grid_trader:
                logger.error("Cannot auto-start trading: Grid trader not initialized")
                return
            
            # Check if grid trading is already running
            if self.grid_trader.is_running:
                logger.info("Grid trading already running, skipping auto-start")
                return
            
            logger.info("Auto-starting grid trading")
            
            # Start grid trading (same process as when /startgrid is called)
            result = self.grid_trader.start()
            
            # Log and notify about the auto-start
            logger.info(f"Auto-start grid trading result: {result}")
            if self.telegram_bot:
                self.telegram_bot.send_message(f"🤖 Grid trading auto-started on system initialization: {result}")
            
            # If grid trader is running and risk manager exists, activate it
            if self.grid_trader.is_running and self.risk_manager:
                # Ensure grid has prices before activating risk manager
                if hasattr(self.grid_trader, 'grid') and self.grid_trader.grid and len(self.grid_trader.grid) > 0:
                    grid_prices = [level['price'] for level in self.grid_trader.grid]
                    if grid_prices:  # Double-check that we have prices
                        self.risk_manager.activate(min(grid_prices), max(grid_prices))
                        logger.info("Risk manager activated with grid price range")
                    else:
                        logger.warning("Cannot activate risk manager: No grid prices available")
                else:
                    logger.warning("Cannot activate risk manager: Grid is empty")
        except Exception as e:
            # Log any errors but don't crash the bot
            logger.error(f"Error during auto-start of grid trading: {e}")
            if self.telegram_bot:
                self.telegram_bot.send_message(f"⚠️ Error during auto-start of grid trading: {str(e)}")

    def start(self):
        """Start the bot with proper state management"""
        with self.state_lock:
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
        
        # Auto-start grid trading - this will automatically start trading without manual intervention
        self._auto_start_grid_trading()
        
        # Start grid maintenance thread
        maintenance_thread = Thread(target=self._grid_maintenance_thread, daemon=True)
        maintenance_thread.start()
        
        # Main loop
        try:
            while True:
                with self.state_lock:
                    if not self.is_running:
                        break
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Received interrupt signal, stopping...")
            self.stop()
    
    def stop(self):
        """Stop the bot and clean up resources"""
        with self.state_lock:
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
        
        # Clean up user data stream if needed
        listen_key_to_close = None
        with self.state_lock:
            listen_key_to_close = self.listen_key
            self.listen_key = None
            
        if listen_key_to_close:
            try:
                client_status = self.binance_client.get_client_status()
                if client_status["websocket_available"]:
                    self.binance_client.ws_client.client.stop_user_data_stream(listen_key_to_close)
                else:
                    self.binance_client.rest_client.close_listen_key(listen_key_to_close)
            except Exception as e:
                logger.error(f"Failed to close listen key: {e}")
        
        # Stop WebSocket connection
        if self.ws_manager:
            self.ws_manager.stop()
        
        # Stop Telegram bot
        if self.telegram_bot:
            self.telegram_bot.send_message("Grid trading bot stopped")
            self.telegram_bot.stop()
        
        logger.info("Grid Trading Bot stopped")


def ensure_websocket_connection(binance_client):
    """
    Ensure WebSocket connection is available, reconnect if needed
    """
    try:
        status = binance_client.get_client_status()
        logger.info("Checked client status for WebSocket connection")
        if not status.get("websocket_available"):
            logger.warning("WebSocket connection unavailable, attempting reconnection")
            # Reinitialize WebSocket
            binance_client.reconnect_websocket()
            
            # Verify reconnection result
            new_status = binance_client.get_client_status()
            if new_status.get("websocket_available"):
                logger.info("WebSocket reconnection successful")
            else:
                logger.error("WebSocket reconnection failed, switching to REST API mode")
    except Exception as e:
        logger.error(f"Error checking WebSocket connection: {e}")
        return False
    return True


def run_grid_trading():
    """
    Initialize and run the grid trading bot
    """
    global grid_trader, risk_manager, telegram_bot
    
    # Create bot instance
    bot = GridTradingBot()
    
    # Store components in global variables for error handling access
    grid_trader = bot.grid_trader
    risk_manager = bot.risk_manager
    telegram_bot = bot.telegram_bot
    
    # Start the bot
    bot.start()


def run_trading_loop():
    """
    Main trading loop with enhanced error handling and recovery
    """
    restart_count = 0
    max_restarts = 5
    
    while restart_count < max_restarts:
        try:
            # Main trading logic
            run_grid_trading()
        except Exception as e:
            restart_count += 1
            logging.critical(f"System error, preparing to restart ({restart_count}/{max_restarts}): {e}", exc_info=True)
            
            # Try to perform safe shutdown
            try:
                if 'grid_trader' in globals() and grid_trader is not None and grid_trader.is_running:
                    grid_trader.stop()
                if 'risk_manager' in globals() and risk_manager is not None and risk_manager.is_active:
                    risk_manager.deactivate()
            except Exception as cleanup_error:
                logging.error(f"Error during shutdown process: {cleanup_error}")
                
            # Delay restart to prevent rapid crash loops
            time.sleep(60)
        else:
            # Normal exit from loop
            break

    if restart_count >= max_restarts:
        logging.critical(f"Maximum restart attempts ({max_restarts}) reached. System halting.")
        # Send critical alert notification
        if 'telegram_bot' in globals():
            telegram_bot.send_message("🚨 CRITICAL: System halted after multiple restart failures")


# Modify the main execution block at the end of the file
if __name__ == "__main__":
    # Define global variables for error handling
    grid_trader = None
    risk_manager = None
    telegram_bot = None
    
    # Start with enhanced error handling
    run_trading_loop()