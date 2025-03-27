import os
import time
import asyncio
import threading
import logging
import numpy as np
from datetime import datetime, timedelta
from collections import deque
import json
import sys
from pathlib import Path
import redis
import pickle
from functools import lru_cache
from numba import jit
import gc
import psutil
import resource

# Add this function here, outside any class definition
# Move the function from inside the class to the top level
@jit(nopython=True)
def calculate_std_dev(returns):
    """Calculate standard deviation with JIT compilation for performance"""
    if len(returns) < 2:
        return 0.0
    
    # Calculate mean
    mean = 0.0
    for r in returns:
        mean += r
    mean /= len(returns)
    
    # Calculate variance
    variance = 0.0
    for r in returns:
        variance += (r - mean) ** 2
    variance /= (len(returns) - 1)
    
    # Return standard deviation
    return variance ** 0.5

# Add parent directory to path for imports
sys.path.append(str(Path(__file__).parent.parent))

# Import configuration and API clients
import config
from binance_api.client import BinanceClient
from binance_api.websocket_api_client import BinanceWebSocketAPIClient
from utils.format_utils import format_price

class HFTMarketMaker:
    """
    High Frequency Market Making Strategy
    
    This class implements a low-latency market making strategy that uses
    WebSocket API for real-time data and order execution to capture small
    price spreads with microsecond precision.
    """
    
    def __init__(self):
        """Initialize the HFT market maker strategy"""
        self.logger = logging.getLogger(__name__)
        self.logger.setLevel(logging.INFO)
        
        # Configure logging handler if not already configured
        if not self.logger.handlers:
            formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
            handler = logging.StreamHandler()
            handler.setFormatter(formatter)
            self.logger.addHandler(handler)
            
            # Add file handler for detailed logging
            file_handler = logging.FileHandler('hft_market_maker.log')
            file_handler.setFormatter(formatter)
            self.logger.addHandler(file_handler)
        
        # Initialize API clients
        self.binance_client = BinanceClient()
        self.ws_client = None
        
        # Trading parameters from config
        self.symbol = config.SYMBOL
        self.base_asset = self.symbol.replace("USDT", "")
        self.quote_asset = "USDT"
        
        # Strategy parameters with default values
        self.spread_threshold = 0.0008  # 0.08%
        self.price_offset_ratio = 0.0003  # 0.03%
        self.order_timeout_phase1 = 0.3  # 300ms
        self.order_timeout_phase2 = 0.6  # 600ms
        self.single_loss_limit = 0.001  # 0.1%
        self.daily_meltdown_level = 0.02  # 2%
        
        # Order state management
        self.active_orders = {}
        self.order_history = {}
        self.last_prices = deque(maxlen=100)  # Store last 100 price points
        self.order_book = {'bids': [], 'asks': []}
        self.last_trade_price = 0
        self.last_spread_calculation = 0
        self.dynamic_spread_threshold = self.spread_threshold
        
        # Risk management
        self.initial_balance = 0
        self.current_balance = 0
        self.daily_pnl = 0
        self.daily_trades = 0
        self.daily_start_time = datetime.now()
        self.is_active = False
        self.circuit_breaker_triggered = False
        self.cooldown_until = {}  # Dict of direction -> timestamp for cooldown periods
        
        # Performance metrics
        self.latency_samples = deque(maxlen=100)
        self.successful_trades = 0
        self.failed_trades = 0
        self.last_metrics_report = time.time()
        
        # Locks for thread safety
        self.order_lock = threading.RLock()
        self.book_lock = threading.RLock()
        self.price_lock = threading.RLock()
        
        # Event loop for async operations
        self.loop = asyncio.get_event_loop()
        self.shutdown_event = asyncio.Event()
        
        # WebSocket connection management
        self.ws_reconnect_count = 0
        self.max_reconnect_attempts = 10
        
        # Initialize Redis client for order book caching
        try:
            self.redis_client = redis.Redis(
                host='localhost', 
                port=6379, 
                db=0,
                socket_timeout=0.1,  # Fast timeout for low latency
                health_check_interval=30
            )
            # Test connection
            self.redis_client.ping()
            self.use_redis_cache = True
            self.logger.info("Redis cache initialized successfully")
        except Exception as e:
            self.logger.warning(f"Redis initialization failed, using in-memory only: {e}")
            self.use_redis_cache = False
        
        # Add trade tracking for stop loss
        self.trade_entries = {}  # Dict of order_id -> entry info for stop loss tracking
        self.trade_lock = threading.RLock()
        
        self.heartbeat_interval = 5  # 5 seconds between heartbeats
        
    def _handle_coroutine_exception(self, future):
        """Handle exceptions from scheduled coroutines."""
        try:
            future.result()
        except Exception as e:
            self.logger.error(f"Unhandled exception in coroutine: {e}")

    def start(self):
        """Start the HFT market making strategy with single-thread event loop model"""
        self.logger.info(f"Starting HFT market maker for {self.symbol}")
        
        # Optimize process priority
        try:
            os.nice(-10)  # Higher priority (lower nice value)
            self.logger.info("Adjusted process priority for low latency")
        except Exception as e:
            self.logger.warning(f"Could not adjust process priority: {e}")
        
        # Optimize memory management without creating a separate thread
        try:
            # Set soft memory limit to 700MB (leave room for OS)
            soft, hard = resource.getrlimit(resource.RLIMIT_AS)
            resource.setrlimit(resource.RLIMIT_AS, (700 * 1024 * 1024, hard))
            
            # Disable automatic garbage collection
            gc.disable()
            
            # Note: No separate GC thread created here
            self.logger.info("Memory optimization configured")
        except Exception as e:
            self.logger.warning(f"Could not optimize memory settings: {e}")
        
        # Get initial account balance
        self._fetch_initial_balance()
        
        # Initialize WebSocket connection for order book data
        self._initialize_websocket()
        
        # Modified event loop handling:
        try:
            # Try to get existing loop instead of creating a new one
            self.loop = asyncio.get_event_loop()
        except RuntimeError:
            # If there's no loop in this thread, create one
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
        
        # Instead of running the loop directly in this thread,
        # schedule the tasks in the existing loop
        if not self.loop.is_running():
            # Only run the loop if it's not already running
            self._run_event_loop()
        else:
            # If loop is already running, just schedule our tasks
            self.loop.call_soon_threadsafe(self._scheduled_gc_task)
            self.loop.call_soon_threadsafe(self._monitoring_task)
            # Schedule our main coroutine
            future = asyncio.run_coroutine_threadsafe(self._strategy_coroutine(), self.loop)
            # Add callback to handle exceptions
            future.add_done_callback(self._handle_coroutine_exception)
        
        self.is_active = True
        self.logger.info("HFT market maker started successfully")
        return True
        
    def stop(self):
        """Stop the HFT market making strategy and clean up resources"""
        self.logger.info("Stopping HFT market maker...")
        self.is_active = False
        
        # Signal event loop to stop
        if self.loop:
            self.loop.call_soon_threadsafe(self.shutdown_event.set)
        
        # No need to join threads as they no longer exist
        
        # Cancel all active orders
        self._cancel_all_orders()
        
        # Close WebSocket connection
        self._close_websocket()
        
        self.logger.info("HFT market maker stopped")
        return True
    
    def _fetch_initial_balance(self):
        """Fetch initial account balance for PnL tracking"""
        try:
            account_info = self.binance_client.get_account_info()
            
            # Extract balances for base and quote assets
            balances = account_info.get('balances', [])
            if isinstance(account_info, dict) and 'result' in account_info:
                balances = account_info['result'].get('balances', [])
                
            base_balance = 0
            quote_balance = 0
            
            for balance in balances:
                asset = balance.get('asset')
                if asset == self.base_asset:
                    base_balance = float(balance.get('free', 0)) + float(balance.get('locked', 0))
                elif asset == self.quote_asset:
                    quote_balance = float(balance.get('free', 0)) + float(balance.get('locked', 0))
            
            # Calculate total balance in USDT
            current_price = self.binance_client.get_symbol_price(self.symbol)
            total_balance = quote_balance + (base_balance * float(current_price))
            
            self.initial_balance = total_balance
            self.current_balance = total_balance
            self.logger.info(f"Initial balance: {self.initial_balance:.2f} USDT")
            
            # Reset daily metrics
            self.daily_pnl = 0
            self.daily_trades = 0
            self.daily_start_time = datetime.now()
            
        except Exception as e:
            self.logger.error(f"Error fetching initial balance: {e}")
    
    def _initialize_websocket(self):
        """
        Initialize WebSocket connection for order book data with enhanced stability
        - Implements exponential backoff for reconnection
        - Tracks connection quality metrics
        - Optimized for t2.micro instance constraints
        """
        try:
            # Track connection attempt start time
            attempt_start = time.time()
            
            # Apply exponential backoff on reconnection attempts
            if self.ws_reconnect_count > 0:
                backoff_seconds = min(60, 2 ** (self.ws_reconnect_count - 1))
                self.logger.info(f"Applying reconnection backoff: waiting {backoff_seconds}s before attempt {self.ws_reconnect_count + 1}")
                time.sleep(backoff_seconds)
            
            # Create direct connection to WebSocket API using our client
            api_key = os.getenv("API_KEY", config.API_KEY)
            private_key_path = os.getenv("PRIVATE_KEY", config.PRIVATE_KEY)
            private_key_pass = None if os.getenv("PRIVATE_KEY_PASS") == "None" else os.getenv("PRIVATE_KEY_PASS",
                                    getattr(config, 'PRIVATE_KEY_PASS', None))
            
            # Clear old client if exists
            if self.ws_client:
                try:
                    self.ws_client.close()
                except:
                    pass
                self.ws_client = None
                
            # Connection setup with connection quality optimizations
            ping_interval = 10  # More frequent pings for reliable connection
            if self.ws_reconnect_count > 3:
                ping_interval = 15  # Save bandwidth on repeated failures
                
            # Create new client
            self.ws_client = BinanceWebSocketAPIClient(
                api_key=api_key,
                private_key_path=private_key_path,
                private_key_pass=private_key_pass,
                use_testnet=config.USE_TESTNET,
                ping_interval=ping_interval,
                timeout=5  # Faster timeout detection for unstable networks
            )
            
            # Verify connection with retry logic
            max_verify_attempts = 2
            for verify_attempt in range(1, max_verify_attempts + 1):
                try:
                    response = self.ws_client.ping_server()
                    if response and response.get('status') == 200:
                        # Successful connection
                        break
                    else:
                        if verify_attempt < max_verify_attempts:
                            self.logger.warning(f"Connection verification attempt {verify_attempt} failed: {response}")
                            time.sleep(1)  # Brief pause between verification attempts
                        else:
                            self.logger.error(f"Failed to connect to WebSocket API: {response}")
                            self.ws_reconnect_count += 1
                            return False
                except Exception as e:
                    if verify_attempt < max_verify_attempts:
                        self.logger.warning(f"Connection verification attempt {verify_attempt} error: {e}")
                        time.sleep(1)
                    else:
                        self.logger.error(f"Failed to verify WebSocket connection: {e}")
                        self.ws_reconnect_count += 1
                        return False
            
            # Successful connection established
            self.logger.info("WebSocket API connected successfully")
            
            # Schedule reconnection counter reset after stable period
            if self.loop and self.is_active:
                def schedule_coro():
                    asyncio.ensure_future(self._schedule_reconnect_counter_reset(), loop=self.loop)
                    
                self.loop.call_soon_threadsafe(schedule_coro)
            
            # Track connection latency for quality monitoring
            connection_latency = time.time() - attempt_start
            self.logger.debug(f"WebSocket connection established in {connection_latency:.2f}s")
            
            # Start WebSocket market data stream using the main client's websocket manager
            if hasattr(self.binance_client, 'ws_manager'):
                try:
                    self.binance_client.ws_manager.start_bookticker_stream(self.symbol)
                    self.binance_client.ws_manager.start_kline_stream(self.symbol, interval='1m')
                    # Add depth stream subscription with 100ms updates
                    self.binance_client.ws_manager.start_depth_stream(self.symbol, speed=100)
                    self.logger.info(f"Subscribed to {self.symbol} book ticker, kline, and depth streams")
                    
                    # Update heartbeat timestamp to reset monitoring cycle
                    self.last_heartbeat = time.time()
                except Exception as e:
                    self.logger.error(f"Failed to subscribe to market data streams: {e}")
                    # Don't count this as connection failure, socket API still usable
            else:
                self.logger.warning("WebSocket manager not available, using REST API for fallback")
            
            # Synchronize state after reconnection
            if self.ws_reconnect_count > 0:
                self._synchronize_state_after_reconnect()
            
            return True
            
        except Exception as e:
            self.logger.error(f"Error initializing WebSocket: {e}")
            self.ws_reconnect_count += 1
            return False
    
    def _close_websocket(self):
        """Close WebSocket connection"""
        try:
            if self.ws_client:
                self.ws_client.close()
                self.ws_client = None
            self.logger.info("WebSocket connection closed")
        except Exception as e:
            self.logger.error(f"Error closing WebSocket: {e}")
    
    def _run_event_loop(self):
        """Run the asyncio event loop for strategy execution with all tasks integrated"""
        try:
            # Add GC task directly to event loop
            self.loop.call_later(30, self._scheduled_gc_task)
            
            # Add monitoring task directly to event loop
            self.loop.call_later(10, self._monitoring_task)
            
            # Run the main strategy coroutine
            self.loop.run_until_complete(self._strategy_coroutine())
        except Exception as e:
            self.logger.error(f"Error in event loop: {e}")
        finally:
            # Cleanup
            pending = asyncio.all_tasks(self.loop)
            for task in pending:
                task.cancel()
            
            self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self.loop.close()
            self.logger.info("Event loop closed")
    
    async def _strategy_coroutine(self):
        """Main strategy coroutine with reduced task count for t2.micro"""
        # Combine multiple tasks to reduce coroutine count
        tasks = [
            self._market_making_cycle(),
            self._combined_risk_and_heartbeat_cycle(),  # Combined task
            self._combined_order_and_threshold_cycle()  # Combined task
        ]
        
        # Wait for shutdown event or task completion
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _combined_risk_and_heartbeat_cycle(self):
        """Combined risk management and heartbeat monitoring to reduce coroutines"""
        self.last_heartbeat = time.time()
        
        while not self.shutdown_event.is_set():
            try:
                current_time = time.time()
                
                # ------ Risk management logic ------
                # Calculate current PnL
                await self._update_current_balance()
                
                # Check for circuit breaker conditions
                if self.current_balance > 0 and self.initial_balance > 0:
                    daily_pnl_percent = self.daily_pnl / self.initial_balance
                    
                    # Level 3 (global) circuit breaker
                    if daily_pnl_percent <= -self.daily_meltdown_level:
                        if not self.circuit_breaker_triggered:
                            self.logger.warning(f"GLOBAL CIRCUIT BREAKER TRIGGERED: Daily loss {daily_pnl_percent:.2%}")
                            self.circuit_breaker_triggered = True
                            await self._cancel_all_orders_async()
                    
                    # Reset level 3 circuit breaker if we're back above threshold
                    elif self.circuit_breaker_triggered and daily_pnl_percent > -self.daily_meltdown_level * 0.8:
                        self.logger.info(f"Global circuit breaker reset: PnL recovered to {daily_pnl_percent:.2%}")
                        self.circuit_breaker_triggered = False
                
                # ------ Heartbeat monitoring -------
                time_since_heartbeat = current_time - self.last_heartbeat
                
                if time_since_heartbeat > self.heartbeat_interval * 3:
                    self.logger.warning(f"No heartbeat for {time_since_heartbeat:.2f}s (threshold: {self.heartbeat_interval * 3:.1f}s)")
                    
                    # Attempt to reconnect WebSocket if not in circuit breaker mode
                    if not self.circuit_breaker_triggered:
                        self.logger.info("Reconnecting WebSocket due to heartbeat timeout")
                        
                        # Close existing connection
                        self._close_websocket()
                        
                        # Reinitialize WebSocket with backoff handled inside method
                        success = self._initialize_websocket()
                        if success:
                            self.logger.info("WebSocket reconnected successfully")
                            self.last_heartbeat = time.time()
                        else:
                            self.logger.error("Failed to reconnect WebSocket")
                
                # Use a single sleep for both functions
                await asyncio.sleep(1)
                
            except Exception as e:
                self.logger.error(f"Error in combined risk and heartbeat cycle: {e}")
                await asyncio.sleep(5)

    async def _combined_order_and_threshold_cycle(self):
        """Combined order timeout checking and dynamic threshold updating"""
        last_threshold_update = time.time()
        
        while not self.shutdown_event.is_set():
            try:
                current_time = time.time()
                
                # ------ Order timeout checking logic ------
                # Get a copy of active orders to avoid modification during iteration
                with self.order_lock:
                    orders = list(self.active_orders.items())
                
                for order_id, order in orders:
                    order_age = current_time - order['timestamp']
                    
                    # Phase 1 timeout: adjust price and retry
                    if order['phase'] == 1 and order_age > self.order_timeout_phase1:
                        await self._handle_phase1_timeout(order_id, order)
                    
                    # Phase 2 timeout: cancel order completely
                    elif order['phase'] == 2 and order_age > self.order_timeout_phase2:
                        await self._handle_phase2_timeout(order_id, order)
                
                # ------ Dynamic threshold updating logic ------
                if current_time - last_threshold_update >= 10:  # Update every 10 seconds
                    # Only update if we have enough price data
                    if len(self.last_prices) >= 10:
                        # Calculate returns with copy to avoid race conditions
                        with self.price_lock:
                            prices = list(self.last_prices)
                            
                        # Calculate returns
                        returns = [prices[i]/prices[i-1] - 1 for i in range(1, len(prices))]
                        
                        # Use JIT-compiled function for standard deviation calculation
                        std_dev = calculate_std_dev(returns)
                        two_sigma = 2 * std_dev
                        
                        # Set dynamic threshold to max of base threshold and 2-sigma
                        self.dynamic_spread_threshold = max(self.spread_threshold, two_sigma)
                        
                        # Log threshold adjustment if significant
                        if abs(self.dynamic_spread_threshold - self.spread_threshold) > 0.0001:
                            self.logger.info(f"Dynamic spread threshold adjusted to {self.dynamic_spread_threshold:.4%}")
                    
                    last_threshold_update = current_time
                
                await asyncio.sleep(0.05)
                
            except Exception as e:
                self.logger.error(f"Error in combined order and threshold cycle: {e}")
                await asyncio.sleep(0.1)
    
    async def _market_making_cycle(self):
        """Core market making logic that runs continuously"""
        last_execution_time = time.time()
        min_cycle_time = 0.01  # Minimum 10ms between cycles
        
        while not self.shutdown_event.is_set():
            try:
                # Rate limiting
                elapsed = time.time() - last_execution_time
                if elapsed < min_cycle_time:
                    await asyncio.sleep(min_cycle_time - elapsed)
                
                # Skip if circuit breaker is active
                if self.circuit_breaker_triggered:
                    await asyncio.sleep(0.5)  # Sleep longer when circuit breaker is active
                    continue
                
                # Get latest book data
                with self.book_lock:
                    book_data = self.order_book.copy()
                
                # Check if we have valid order book data
                if not book_data['bids'] or not book_data['asks']:
                    await asyncio.sleep(0.01)
                    continue
                
                # Calculate current spread
                best_bid = float(book_data['bids'][0][0]) if book_data['bids'] else None
                best_ask = float(book_data['asks'][0][0]) if book_data['asks'] else None
                
                if not best_bid or not best_ask:
                    await asyncio.sleep(0.01)
                    continue
                
                current_spread_pct = (best_ask - best_bid) / best_bid
                
                # Check if spread exceeds threshold
                if current_spread_pct >= self.dynamic_spread_threshold:
                    # Calculate order book depth
                    bid_depth = sum(float(qty) for _, qty in book_data['bids'][:3])
                    ask_depth = sum(float(qty) for _, qty in book_data['asks'][:3])
                    
                    # Only place orders if depth is sufficient
                    if bid_depth > 5 and ask_depth > 5:  # 5 BTC minimum depth
                        # Dynamic order pricing
                        offset = self.price_offset_ratio
                        if bid_depth > 10:  # Deeper book allows tighter spreads
                            offset *= 0.67  # Reduce to 0.02% for deep books
                        
                        # Calculate order prices
                        bid_price = best_bid * (1 + offset)
                        ask_price = best_ask * (1 - offset)
                        
                        # Calculate order quantity based on depth and min notional
                        min_notional = config.MIN_NOTIONAL_VALUE
                        avg_price = (best_bid + best_ask) / 2
                        max_qty = min(bid_depth, ask_depth) * 0.20  # 20% of book depth
                        qty = max(min_notional / avg_price, max_qty)
                        
                        # Round quantity to appropriate precision
                        qty = self._round_quantity(qty)
                        
                        # Check for cooldown periods
                        current_time = time.time()
                        can_buy = current_time > self.cooldown_until.get('BUY', 0)
                        can_sell = current_time > self.cooldown_until.get('SELL', 0)
                        
                        # Place orders if not in cooldown
                        if can_buy:
                            await self._place_order('BUY', qty, bid_price)
                        
                        if can_sell:
                            await self._place_order('SELL', qty, ask_price)
                
                # Update execution time
                last_execution_time = time.time()
                
            except Exception as e:
                self.logger.error(f"Error in market making cycle: {e}")
                await asyncio.sleep(0.1)  # Brief pause on error
    
    async def _place_order(self, side, quantity, price):
        """Place a limit order with timeout tracking"""
        try:
            # Start timing for latency measurement
            start_time = time.time()
            
            # Format price with appropriate precision
            formatted_price = format_price(price, 8)  # Using default precision of 8
            
            # Generate client order id for tracking
            client_order_id = f"hft_{int(time.time() * 1000)}_{side.lower()[:1]}"
            
            # Place order using WebSocket API for minimum latency
            params = {
                'symbol': self.symbol,
                'side': side,
                'type': 'LIMIT',
                'timeInForce': 'GTC',
                'quantity': str(quantity),
                'price': formatted_price,
                'newClientOrderId': client_order_id
            }
            
            # Use direct WebSocket API client for lowest latency
            request_id = self.ws_client._send_signed_request("order.place", params)
            
            # Store order details for timeout tracking
            with self.order_lock:
                self.active_orders[client_order_id] = {
                    'id': client_order_id,
                    'timestamp': time.time(),
                    'side': side,
                    'price': price,
                    'quantity': quantity,
                    'status': 'PENDING',
                    'phase': 1,  # Initial phase
                    'request_id': request_id
                }
            
            # Measure and record order placement latency
            latency = (time.time() - start_time) * 1000  # Convert to ms
            self.latency_samples.append(latency)
            
            self.logger.debug(f"Placed {side} order {client_order_id}: {quantity} @ {formatted_price} (latency: {latency:.2f}ms)")
            return client_order_id
            
        except Exception as e:
            self.logger.error(f"Error placing {side} order: {e}")
            self.failed_trades += 1
            return None
    
    async def _check_order_status(self, client_order_id):
        """Check the status of an order"""
        try:
            params = {
                'symbol': self.symbol,
                'origClientOrderId': client_order_id
            }
            
            request_id = self.ws_client._send_signed_request("order.status", params)
            
            # Wait for response with a short timeout
            response = await asyncio.wait_for(
                self._wait_for_response(request_id),
                timeout=0.5
            )
            
            if response and response.get('status') == 200:
                order_status = response.get('result', {}).get('status')
                return order_status
            else:
                self.logger.warning(f"Failed to get status for order {client_order_id}: {response}")
                return None
                
        except asyncio.TimeoutError:
            self.logger.warning(f"Timeout checking status for order {client_order_id}")
            return None
        except Exception as e:
            self.logger.error(f"Error checking order status: {e}")
            return None
    
    async def _cancel_order(self, client_order_id):
        """Cancel an order by client order ID"""
        try:
            params = {
                'symbol': self.symbol,
                'origClientOrderId': client_order_id
            }
            
            request_id = self.ws_client._send_signed_request("order.cancel", params)
            
            # Wait for response with a short timeout
            response = await asyncio.wait_for(
                self._wait_for_response(request_id),
                timeout=0.5
            )
            
            if response and response.get('status') == 200:
                self.logger.debug(f"Successfully canceled order {client_order_id}")
                return True
            else:
                self.logger.warning(f"Failed to cancel order {client_order_id}: {response}")
                return False
                
        except asyncio.TimeoutError:
            self.logger.warning(f"Timeout canceling order {client_order_id}")
            return False
        except Exception as e:
            self.logger.error(f"Error canceling order: {e}")
            return False
    
    async def _wait_for_response(self, request_id):
        """Wait for a response from WebSocket API"""
        try:
            # Create a future to get the response
            future = asyncio.Future()
            
            def callback(response):
                if not future.done():
                    future.set_result(response)
            
            # Register callback
            with self.ws_client.lock:
                self.ws_client.request_callbacks[request_id] = callback
            
            # Wait for response or timeout
            return await future
            
        except Exception as e:
            self.logger.error(f"Error waiting for response: {e}")
            return None
    
    async def _handle_phase1_timeout(self, order_id, order):
        """Handle phase 1 order timeout by adjusting price and retrying"""
        try:
            self.logger.debug(f"Phase 1 timeout for {order['side']} order {order_id}")
            
            # Cancel the existing order
            cancel_success = await self._cancel_order(order_id)
            
            if cancel_success:
                # Calculate new price with adjustment
                side = order['side']
                original_price = order['price']
                quantity = order['quantity']
                
                # Adjust price by 0.01% in favorable direction
                price_adjustment = 0.0001  # 0.01%
                if side == 'BUY':
                    new_price = original_price * (1 + price_adjustment)
                else:
                    new_price = original_price * (1 - price_adjustment)
                
                # Place new order
                new_order_id = await self._place_order(side, quantity, new_price)
                
                if new_order_id:
                    # Update phase for the new order
                    with self.order_lock:
                        if new_order_id in self.active_orders:
                            self.active_orders[new_order_id]['phase'] = 2
                
                # Remove old order from tracking
                with self.order_lock:
                    if order_id in self.active_orders:
                        del self.active_orders[order_id]
                        
            else:
                # Check if order was filled before we could cancel
                status = await self._check_order_status(order_id)
                if status == 'FILLED':
                    self.logger.info(f"Order {order_id} was filled before timeout handling")
                    with self.order_lock:
                        if order_id in self.active_orders:
                            del self.active_orders[order_id]
                            self.successful_trades += 1
                
        except Exception as e:
            self.logger.error(f"Error handling phase 1 timeout: {e}")
    
    async def _handle_phase2_timeout(self, order_id, order):
        """Handle phase 2 order timeout by canceling and entering cooldown"""
        try:
            self.logger.debug(f"Phase 2 timeout for {order['side']} order {order_id}")
            
            # Cancel the order
            cancel_success = await self._cancel_order(order_id)
            
            # Start cooldown period for this side
            side = order['side']
            cooldown_seconds = 10  # 10 second cooldown
            self.cooldown_until[side] = time.time() + cooldown_seconds
            
            self.logger.info(f"Starting {cooldown_seconds}s cooldown for {side} orders")
            
            # Remove from active orders
            with self.order_lock:
                if order_id in self.active_orders:
                    del self.active_orders[order_id]
            
        except Exception as e:
            self.logger.error(f"Error handling phase 2 timeout: {e}")
    
    async def _update_current_balance(self):
        """Update current balance and PnL calculations"""
        try:
            account_info = self.binance_client.get_account_info()
            
            # Extract balances for base and quote assets
            balances = account_info.get('balances', [])
            if isinstance(account_info, dict) and 'result' in account_info:
                balances = account_info['result'].get('balances', [])
                
            base_balance = 0
            quote_balance = 0
            
            for balance in balances:
                asset = balance.get('asset')
                if asset == self.base_asset:
                    base_balance = float(balance.get('free', 0)) + float(balance.get('locked', 0))
                elif asset == self.quote_asset:
                    quote_balance = float(balance.get('free', 0)) + float(balance.get('locked', 0))
            
            # Calculate total balance in USDT
            current_price = 0
            with self.price_lock:
                current_price = self.last_trade_price
            
            if not current_price:
                current_price = float(self.binance_client.get_symbol_price(self.symbol))
            
            total_balance = quote_balance + (base_balance * current_price)
            
            old_balance = self.current_balance
            self.current_balance = total_balance
            
            # Update daily PnL
            balance_change = total_balance - old_balance
            if abs(balance_change) > 0.001:  # Only count significant changes (>0.001 USDT)
                self.daily_pnl += balance_change
                
        except Exception as e:
            self.logger.error(f"Error updating current balance: {e}")
    
    async def _place_hedge_order(self, side, quantity, price):
        """Place a hedge order with faster market conversion on timeout"""
        try:
            self.logger.info(f"Placing hedge {side} order: {quantity} @ {price}")
            
            # Place limit order first
            client_order_id = await self._place_order(side, quantity, price)
            
            if not client_order_id:
                # If limit order placement fails, try market order with fallback
                await self._place_market_hedge_fallback(side, quantity)
                return
                
            # Start a timeout check for the hedge order
            hedge_timeout = 1.0  # 1 second timeout for hedge orders
            
            await asyncio.sleep(hedge_timeout)
            
            # Check if order was filled
            with self.order_lock:
                if client_order_id in self.active_orders:
                    # Cancel the limit order and place market order instead
                    self.logger.info(f"Hedge order {client_order_id} timed out, converting to market order")
                    
                    # Cancel the limit order
                    cancel_success = await self._cancel_order(client_order_id)
                    
                    # Place market order with fallback
                    await self._place_market_hedge_fallback(side, quantity)
                    
                    # Remove from active orders
                    with self.order_lock:
                        if client_order_id in self.active_orders:
                            del self.active_orders[client_order_id]
            
        except Exception as e:
            self.logger.error(f"Error placing hedge order: {e}")
    
    async def _place_market_hedge(self, side, quantity):
        """Place a market order for hedging"""
        try:
            # Format parameters
            params = {
                'symbol': self.symbol,
                'side': side,
                'type': 'MARKET',
                'quantity': str(quantity)
            }
            
            # Send market order request
            request_id = self.ws_client._send_signed_request("order.place", params)
            
            # Wait for response
            response = await asyncio.wait_for(
                self._wait_for_response(request_id),
                timeout=0.5
            )
            
            if response and response.get('status') == 200:
                self.logger.info(f"Market hedge order placed successfully: {side} {quantity}")
                return True
            else:
                self.logger.warning(f"Failed to place market hedge order: {response}")
                return False
                
        except Exception as e:
            self.logger.error(f"Error placing market hedge order: {e}")
            return False
    
    async def _place_market_hedge_fallback(self, side, quantity):
        """Place a market order for hedging with fallback to REST API"""
        try:
            # Try WebSocket API first
            ws_success = await self._place_market_hedge(side, quantity)
            
            # Fall back to REST API if WebSocket fails
            if not ws_success:
                self.logger.warning("WebSocket API market order failed, falling back to REST API")
                try:
                    loop = self.loop
                    await loop.run_in_executor(
                        None, 
                        lambda: self.binance_client.place_market_order(self.symbol, side, quantity)
                    )
                    self.logger.info(f"Market hedge order placed via REST API: {side} {quantity}")
                    return True
                except Exception as rest_error:
                    self.logger.error(f"REST API fallback also failed: {rest_error}")
                    return False
            
            return ws_success
                
        except Exception as e:
            self.logger.error(f"Error placing market hedge order: {e}")
            return False
    
    def handle_websocket_message(self, message):
        """Process WebSocket message from the main manager"""
        try:
            # Handle book ticker messages
            if hasattr(message, 's') and hasattr(message, 'b') and hasattr(message, 'a'):
                # This is a book ticker message
                symbol = message.s
                
                if symbol == self.symbol:
                    # Update order book best prices
                    with self.book_lock:
                        self.order_book['bids'] = [[message.b, message.B]]
                        self.order_book['asks'] = [[message.a, message.A]]
                    
                    # Update last trade price (midpoint)
                    with self.price_lock:
                        price = (float(message.b) + float(message.a)) / 2
                        self.last_trade_price = price
                        self.last_prices.append(price)
            
            # Handle execution reports for our orders
            elif hasattr(message, 'e') and message.e == 'executionReport':
                self._handle_execution_report(message)
                
            # Handle kline data for volatility calculations
            elif hasattr(message, 'e') and message.e == 'kline':
                if hasattr(message, 'k') and hasattr(message.k, 'c'):
                    with self.price_lock:
                        price = float(message.k.c)
                        self.last_prices.append(price)
                        
            # Handle depth updates for order book depth tracking
            elif hasattr(message, 'e') and message.e == 'depthUpdate':
                self._handle_depth_update(message)
                
        except Exception as e:
            self.logger.error(f"Error handling WebSocket message: {e}")
    
    def _handle_execution_report(self, message):
        """Process execution report for order updates"""
        try:
            # Extract relevant fields from execution report
            client_order_id = getattr(message, 'C', None)
            order_id = getattr(message, 'i', None)
            status = getattr(message, 'X', None)
            side = getattr(message, 'S', None)
            symbol = getattr(message, 's', None)
            price = getattr(message, 'p', None)
            executed_qty = getattr(message, 'l', None)
            
            # Skip messages for other symbols
            if symbol != self.symbol:
                return
                
            # Skip if not our order (no client_order_id starting with "hft_")
            if not client_order_id or not client_order_id.startswith("hft_"):
                return
            
            self.logger.debug(f"Order update: {client_order_id} status={status} side={side}")
            
            # Handle order status updates
            if status == 'FILLED':
                self._handle_filled_order(client_order_id, side, float(price), float(executed_qty))
                
            elif status == 'PARTIALLY_FILLED':
                self.logger.info(f"Partial fill for order {client_order_id}: {executed_qty}")
                
            elif status == 'REJECTED' or status == 'EXPIRED':
                self.logger.warning(f"Order {client_order_id} {status}: {getattr(message, 'r', 'No reason')}")
                self.failed_trades += 1
                
                # Remove from active orders
                with self.order_lock:
                    if client_order_id in self.active_orders:
                        del self.active_orders[client_order_id]
                        
            elif status == 'CANCELED':
                self.logger.debug(f"Order {client_order_id} canceled")
                
                # Remove from active orders
                with self.order_lock:
                    if client_order_id in self.active_orders:
                        del self.active_orders[client_order_id]
                        
        except Exception as e:
            self.logger.error(f"Error processing execution report: {e}")
    
    def _handle_filled_order(self, client_order_id, side, price, quantity):
        """Handle a filled order and immediately place hedge order if needed"""
        try:
            self.logger.info(f"{side} order {client_order_id} filled: {quantity} @ {price}")
            
            # Update trade counters
            self.successful_trades += 1
            self.daily_trades += 1
            
            # Remove from active orders
            with self.order_lock:
                if client_order_id in self.active_orders:
                    order_data = self.active_orders.pop(client_order_id)
                else:
                    order_data = {'side': side, 'price': price, 'quantity': quantity}
            
            # Track entry for stop loss if this is a buy
            if side == 'BUY':
                # Calculate stop loss price
                stop_loss_price = price * (1 - self.single_loss_limit)
                
                # Store entry information with thread safety
                with self.trade_lock:
                    self.trade_entries[client_order_id] = {
                        'entry_price': price,
                        'stop_loss_price': stop_loss_price,
                        'quantity': quantity,
                        'timestamp': time.time()
                    }
                
                self.logger.debug(f"Set stop loss for {client_order_id} at {stop_loss_price}")
            
            # Place immediate hedge order
            if side == 'BUY':
                # Place sell order at a small profit
                hedge_price = price * (1 + self.price_offset_ratio)
                
                # Use event loop to place the hedge order
                if self.loop and self.is_active and not self.circuit_breaker_triggered:
                    self.loop.call_soon_threadsafe(
                        lambda: asyncio.ensure_future(
                            self._place_hedge_order('SELL', quantity, hedge_price)
                        )
                    )
            elif side == 'SELL':
                # Place buy order at a small profit 
                hedge_price = price * (1 - self.price_offset_ratio)
                
                # Use event loop to place the hedge order
                if self.loop and self.is_active and not self.circuit_breaker_triggered:
                    self.loop.call_soon_threadsafe(
                        lambda: asyncio.ensure_future(
                            self._place_hedge_order('BUY', quantity, hedge_price)
                        )
                    )
                    
        except Exception as e:
            self.logger.error(f"Error handling filled order: {e}")
    
    def _handle_depth_update(self, message):
        """Process order book depth update messages"""
        try:
            if getattr(message, 's', None) != self.symbol:
                return
                
            # Extract bids and asks
            bids = getattr(message, 'b', [])
            asks = getattr(message, 'a', [])
            
            # Update order book data
            with self.book_lock:
                # Update bids
                for bid in bids:
                    price, qty = float(bid[0]), float(bid[1])
                    if qty == 0:
                        # Remove price level
                        self.order_book['bids'] = [b for b in self.order_book['bids'] if float(b[0]) != price]
                    else:
                        # Update or add price level
                        updated = False
                        for i, existing_bid in enumerate(self.order_book['bids']):
                            if float(existing_bid[0]) == price:
                                self.order_book['bids'][i] = [str(price), str(qty)]
                                updated = True
                                break
                        if not updated:
                            self.order_book['bids'].append([str(price), str(qty)])
                            # Sort bids in descending order
                            self.order_book['bids'].sort(key=lambda x: float(x[0]), reverse=True)
                
                # Update asks
                for ask in asks:
                    price, qty = float(ask[0]), float(ask[1])
                    if qty == 0:
                        # Remove price level
                        self.order_book['asks'] = [a for a in self.order_book['asks'] if float(a[0]) != price]
                    else:
                        # Update or add price level
                        updated = False
                        for i, existing_ask in enumerate(self.order_book['asks']):
                            if float(existing_ask[0]) == price:
                                self.order_book['asks'][i] = [str(price), str(qty)]
                                updated = True
                                break
                        if not updated:
                            self.order_book['asks'].append([str(price), str(qty)])
                            # Sort asks in ascending order
                            self.order_book['asks'].sort(key=lambda x: float(x[0]))
                
                # Keep only top 10 levels
                self.order_book['bids'] = self.order_book['bids'][:10]
                self.order_book['asks'] = self.order_book['asks'][:10]
                
        except Exception as e:
            self.logger.error(f"Error handling depth update: {e}")
    
    async def _cancel_all_orders_async(self):
        """Cancel all active orders asynchronously"""
        try:
            self.logger.info("Cancelling all active orders")
            
            # Get a copy of active orders to avoid modification during iteration
            with self.order_lock:
                order_ids = list(self.active_orders.keys())
            
            # Cancel each order
            for order_id in order_ids:
                await self._cancel_order(order_id)
            
            # Clear active orders dictionary
            with self.order_lock:
                self.active_orders.clear()
                
            return True
            
        except Exception as e:
            self.logger.error(f"Error cancelling all orders: {e}")
            return False
    
    def _cancel_all_orders(self):
        """Non-async version of order cancellation for shutdown"""
        try:
            self.logger.info("Cancelling all active orders (sync)")
            
            # Use REST API for more reliable cancellation during shutdown
            open_orders = self.binance_client.get_open_orders(symbol=self.symbol)
            
            if not open_orders:
                return True
                
            cancel_count = 0
            
            # Process different response formats
            if isinstance(open_orders, list):
                # REST API format
                for order in open_orders:
                    if 'orderId' in order and 'symbol' in order:
                        try:
                            self.binance_client.cancel_order(order['symbol'], order['orderId'])
                            cancel_count += 1
                        except Exception as e:
                            self.logger.error(f"Error cancelling order {order['orderId']}: {e}")
            else:
                # WebSocket API format with 'result' field
                orders = open_orders.get('result', [])
                for order in orders:
                    if 'orderId' in order and 'symbol' in order:
                        try:
                            self.binance_client.cancel_order(order['symbol'], order['orderId'])
                            cancel_count += 1
                        except Exception as e:
                            self.logger.error(f"Error cancelling order {order['orderId']}: {e}")
            
            self.logger.info(f"Cancelled {cancel_count} open orders")
            
            # Clear active orders
            with self.order_lock:
                self.active_orders.clear()
                
            return True
            
        except Exception as e:
            self.logger.error(f"Error in cancel_all_orders: {e}")
            return False
    
    def _reset_daily_metrics(self):
        """Reset daily performance metrics"""
        try:
            self.logger.info("Resetting daily metrics")
            
            # Save previous day stats for logging
            prev_pnl = self.daily_pnl
            prev_trades = self.daily_trades
            
            # Calculate daily performance
            daily_return = prev_pnl / self.initial_balance if self.initial_balance > 0 else 0
            
            # Log summary
            self.logger.info(f"Daily summary: PnL={prev_pnl:.2f} USDT ({daily_return:.2%}), Trades={prev_trades}")
            
            # Reset metrics
            self.daily_pnl = 0
            self.daily_trades = 0
            self.daily_start_time = datetime.now()
            
            # Update initial balance to current balance
            self.initial_balance = self.current_balance
            
            # Reset circuit breaker
            self.circuit_breaker_triggered = False
            
        except Exception as e:
            self.logger.error(f"Error resetting daily metrics: {e}")
    
    def _report_metrics(self):
        """Generate and log performance metrics"""
        try:
            # Calculate latency statistics
            if self.latency_samples:
                avg_latency = sum(self.latency_samples) / len(self.latency_samples)
                max_latency = max(self.latency_samples)
            else:
                avg_latency = 0
                max_latency = 0
            
            # Calculate success rate
            total_trades_attempted = self.successful_trades + self.failed_trades
            success_rate = self.successful_trades / total_trades_attempted * 100 if total_trades_attempted > 0 else 0
            
            # Calculate PnL metrics
            hourly_trades = self.daily_trades / ((datetime.now() - self.daily_start_time).total_seconds() / 3600) if (datetime.now() - self.daily_start_time).total_seconds() > 0 else 0
            
            pnl_percent = self.daily_pnl / self.initial_balance * 100 if self.initial_balance > 0 else 0
            
            # Log metrics
            self.logger.info(f"Performance metrics:")
            self.logger.info(f"- Latency: avg={avg_latency:.2f}ms, max={max_latency:.2f}ms")
            self.logger.info(f"- Trades: successful={self.successful_trades}, failed={self.failed_trades}, " +
                           f"success_rate={success_rate:.1f}%, hourly_rate={hourly_trades:.1f}")
            self.logger.info(f"- PnL: daily={self.daily_pnl:.2f} USDT ({pnl_percent:.2f}%)")
            self.logger.info(f"- Order book depth: bids={len(self.order_book['bids'])}, asks={len(self.order_book['asks'])}")
            self.logger.info(f"- Active orders: {len(self.active_orders)}")
            
            # Write metrics to JSON file for external monitoring
            metrics = {
                "timestamp": datetime.now().isoformat(),
                "latency_ms": {
                    "avg": round(avg_latency, 2),
                    "max": round(max_latency, 2)
                },
                "trades": {
                    "successful": self.successful_trades,
                    "failed": self.failed_trades,
                    "success_rate": round(success_rate, 1),
                    "hourly_rate": round(hourly_trades, 1)
                },
                "pnl": {
                    "daily": round(self.daily_pnl, 2),
                    "percent": round(pnl_percent, 2)
                },
                "risk_status": "STOPPED" if self.circuit_breaker_triggered else "NORMAL"
            }
            
            # Write to file with atomic replacement
            with open("hft_metrics_tmp.json", "w") as f:
                json.dump(metrics, f, indent=2)
            
            # Atomic replace
            os.replace("hft_metrics_tmp.json", "hft_metrics.json")
            
        except Exception as e:
            self.logger.error(f"Error generating metrics report: {e}")
    
    async def _schedule_reconnect_counter_reset(self):
        """Schedule reconnect counter reset as async coroutine instead of new thread"""
        try:
            # Wait for stable period (2 minutes)
            await asyncio.sleep(120)
            
            if self.ws_client and self.is_active and not self.circuit_breaker_triggered:
                # Only reset if we're still connected after the wait period
                old_count = self.ws_reconnect_count
                if old_count > 0:
                    self.ws_reconnect_count = 0
                    self.logger.info(f"Connection stable for 2 minutes, reconnection counter reset from {old_count} to 0")
        except Exception as e:
            self.logger.error(f"Error in reconnect counter reset: {e}")

    def _synchronize_state_after_reconnect(self):
        """
        Synchronize critical state data after a successful reconnection
        - Refreshes order book data
        - Validates active orders are still valid
        - Updates cached price data
        """
        try:
            self.logger.info("Synchronizing state after reconnection")
            
            # Schedule a check of all active orders to ensure they're still valid
            # Use the main thread's event loop to schedule this
            if self.loop and self.is_active:
                self.loop.call_soon_threadsafe(
                    lambda: asyncio.ensure_future(self._validate_active_orders_after_reconnect())
                )
                
            # Refresh current market price
            current_price = self.binance_client.get_symbol_price(self.symbol)
            with self.price_lock:
                self.last_trade_price = float(current_price)
                # Avoid corrupting price history with a potentially stale value
                if len(self.last_prices) > 0:
                    self.last_prices.append(float(current_price))
                    
            self.logger.info("State synchronization after reconnection completed")
        except Exception as e:
            self.logger.error(f"Error synchronizing state after reconnection: {e}")

    async def _validate_active_orders_after_reconnect(self):
        """
        Validate all active orders are still valid after a reconnection
        Identifies and resolves any inconsistencies between local state and exchange state
        """
        try:
            self.logger.info("Validating active orders after reconnection")
            
            # Get open orders from exchange
            open_orders = self.binance_client.get_open_orders(symbol=self.symbol)
            
            # Extract order IDs from exchange response
            exchange_order_ids = []
            if isinstance(open_orders, list):
                exchange_order_ids = [order.get('clientOrderId') for order in open_orders if 'clientOrderId' in order]
            elif isinstance(open_orders, dict) and 'result' in open_orders:
                exchange_order_ids = [order.get('clientOrderId') for order in open_orders['result'] if 'clientOrderId' in order]
                
            # Filter to only our HFT orders
            exchange_order_ids = [order_id for order_id in exchange_order_ids if order_id and order_id.startswith("hft_")]
            
            # Get local order IDs with thread safety
            with self.order_lock:
                local_order_ids = list(self.active_orders.keys())
            
            # Find discrepancies
            missing_from_exchange = [order_id for order_id in local_order_ids if order_id not in exchange_order_ids]
            missing_locally = [order_id for order_id in exchange_order_ids if order_id not in local_order_ids]
            
            # Handle orders we think are active but exchange doesn't have
            for order_id in missing_from_exchange:
                self.logger.warning(f"Order {order_id} missing from exchange after reconnection, removing from local state")
                with self.order_lock:
                    if order_id in self.active_orders:
                        del self.active_orders[order_id]
            
            # Handle orders exchange has but we don't track locally
            for order_id in missing_locally:
                self.logger.warning(f"Order {order_id} found on exchange but missing locally, cancelling for safety")
                # Cancel order since we've lost track of its context
                params = {
                    'symbol': self.symbol,
                    'origClientOrderId': order_id
                }
                try:
                    request_id = self.ws_client._send_signed_request("order.cancel", params)
                except Exception as e:
                    self.logger.error(f"Failed to cancel orphaned order {order_id}: {e}")
                    
            self.logger.info(f"Active orders validated after reconnection: {len(local_order_ids)} local, {len(exchange_order_ids)} on exchange")
        except Exception as e:
            self.logger.error(f"Error validating active orders after reconnection: {e}")

    def _cache_order_book(self, book_data):
        """Cache order book data in Redis to reduce memory pressure"""
        if not self.use_redis_cache:
            return
            
        try:
            # Use pickle for efficient serialization
            serialized = pickle.dumps(book_data)
            self.redis_client.set(f"order_book:{self.symbol}", serialized, ex=60)  # 1 minute TTL
        except Exception as e:
            self.logger.debug(f"Redis cache write failed: {e}")
            
    def _get_cached_order_book(self):
        """Retrieve order book from Redis cache"""
        if not self.use_redis_cache:
            return None
            
        try:
            cached_data = self.redis_client.get(f"order_book:{self.symbol}")
            if cached_data:
                return pickle.loads(cached_data)
        except Exception as e:
            self.logger.debug(f"Redis cache read failed: {e}")
        
        return None
    
    def _scheduled_gc_task(self):
        """Garbage collection task scheduled in event loop instead of separate thread"""
        if not self.is_active:
            return
        
        try:
            # Only run GC if memory usage is high
            process = psutil.Process(os.getpid())
            memory_info = process.memory_info()
            memory_usage_mb = memory_info.rss / 1024 / 1024
            
            if memory_usage_mb > 300:  # 300MB threshold
                self.logger.debug(f"Running manual GC (memory: {memory_usage_mb:.1f}MB)")
                gc.collect()
        except Exception as e:
            self.logger.error(f"Error in GC task: {e}")
        
        # Reschedule the task if still active
        if self.is_active and self.loop:
            self.loop.call_later(30, self._scheduled_gc_task)

    def _monitoring_task(self):
        """Monitoring task scheduled in event loop instead of separate thread"""
        if not self.is_active:
            return
            
        try:
            # Report metrics every 5 minutes
            if time.time() - self.last_metrics_report > 300:  # 5 minutes
                self._report_metrics()
                self.last_metrics_report = time.time()
        except Exception as e:
            self.logger.error(f"Error in monitoring task: {e}")
        
        # Reschedule the task
        if self.is_active and self.loop:
            self.loop.call_later(10, self._monitoring_task)