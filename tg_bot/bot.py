import logging
import asyncio
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import config

class TelegramBot:
    def __init__(self, token, allowed_users, grid_trader=None, risk_manager=None, on_symbol_change=None):
        """Initialize the Telegram bot with the given token and allowed users"""
        self.application = Application.builder().token(token).build()
        self.allowed_users = allowed_users
        self.grid_trader = grid_trader
        self.risk_manager = risk_manager
        self.on_symbol_change = on_symbol_change  # Optional callback to propagate symbol changes to controller
        self.logger = logging.getLogger(__name__)
        self.is_running = False
        self.loop = None
        self._register_handlers()
    
    def _register_handlers(self):
        """Register command handlers for the bot"""
        commands = {
            "start": self._handle_start,
            "help": self._handle_help,
            "status": self._handle_status,
            "startgrid": self._handle_start_grid,
            "stopgrid": self._handle_stop_grid,
            "risk": self._handle_risk_status,
            "setsymbol": self._handle_set_symbol,  # New command for updating trading pair
        }
        
        for cmd, handler in commands.items():
            self.application.add_handler(CommandHandler(cmd, handler))
            
        self.application.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message)
        )
    
    def start(self):
        """Start the Telegram bot"""
        if self.is_running:
            self.logger.info("Telegram bot already running")
            return
            
        self.logger.info("Starting Telegram bot")
        self.is_running = True
        
        # Safer access to internal handlers for logging
        try:
            handlers_count = len(self.application.handlers)
            registered_commands = []
            for group in self.application.handlers:
                if group:  # Check if not empty
                    for handler in group:
                        if isinstance(handler, CommandHandler):
                            registered_commands.extend(handler.commands)
            self.logger.info(f"Bot registered commands: {', '.join(registered_commands)}")
        except Exception as e:
            self.logger.debug(f"Could not determine registered commands: {e}")
        
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        
        import threading
        self.bot_thread = threading.Thread(target=self._run_bot)
        self.bot_thread.daemon = True
        self.bot_thread.start()
    
    def _run_bot(self):
        """Run the bot in the current thread"""
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self.application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            close_loop=False,
            stop_signals=None
        ))
    
    def stop(self):
        """Stop the Telegram bot"""
        if not self.is_running:
            self.logger.info("Telegram bot not running")
            return
            
        self.logger.info("Stopping Telegram bot")
        if self.loop and self.application:
            asyncio.run_coroutine_threadsafe(self.application.stop(), self.loop)
        self.is_running = False
    
    def send_message(self, message, user_id=None):
        """Send a message to all allowed users or a specific user"""
        if not self.is_running or self.loop is None:
            self.logger.warning("Cannot send message: Telegram bot not running properly")
            return
            
        if user_id and user_id in self.allowed_users:
            asyncio.run_coroutine_threadsafe(
                self._send_telegram_message(user_id, message), self.loop
            )
        elif not user_id:
            for uid in self.allowed_users:
                asyncio.run_coroutine_threadsafe(
                    self._send_telegram_message(uid, message), self.loop
                )
    
    async def _send_telegram_message(self, user_id, text):
        """Helper method to send a message asynchronously"""
        try:
            await self.application.bot.send_message(chat_id=user_id, text=text)
        except Exception as e:
            self.logger.error(f"Failed to send message to user {user_id}: {e}")
    
    def _is_user_authorized(self, user_id):
        """Check if a user is authorized to use this bot"""
        return user_id in self.allowed_users
    
    async def _handle_authorized_command(self, update: Update, handler_func):
        """Common wrapper for authorized command handling"""
        user_id = update.effective_user.id
        if not self._is_user_authorized(user_id):
            await update.message.reply_text("You are not authorized to use this bot")
            return None
        return await handler_func(update)
    
    async def _handle_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command"""
        async def handler(update):
            await update.message.reply_text(
                "Welcome to Grid Trading Bot!\n"
                "Available commands:\n"
                "/help - Show help information\n"
                "/status - Show current status\n"
                "/startgrid - Start grid trading\n"
                "/stopgrid - Stop grid trading"
            )
        await self._handle_authorized_command(update, handler)
    
    async def _handle_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /help command"""
        async def handler(update):
            help_text = (
                "Available commands:\n"
                "/start - Welcome message\n"
                "/help - Show this help message\n"
                "/status - Get current grid trading status\n"
                "/startgrid - Start grid trading\n"
                "/stopgrid - Stop grid trading\n"
                "/risk - View risk management status\n"
                "/setsymbol [SYMBOL] - Change trading pair (e.g., /setsymbol BTCUSDT)"
            )
            await update.message.reply_text(help_text)
        await self._handle_authorized_command(update, handler)
    
    async def _handle_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /status command"""
        async def handler(update):
            if self.grid_trader:
                status = self._format_status_message()
                await update.message.reply_text(status)
            else:
                await update.message.reply_text("Grid trading system not initialized")
        await self._handle_authorized_command(update, handler)

    def _format_status_message(self):
        """Build a rich status view for Telegram in English."""
        gt = self.grid_trader
        is_running = getattr(gt, "is_running", False)

        # Market and trend
        trend_val = float(getattr(gt, "trend_strength", 0) or 0)
        trend_val = max(min(trend_val, 1.0), -1.0)
        trend_bar = self._trend_meter(trend_val)
        market_state = getattr(gt, "current_market_state", None)
        market_state_label = market_state.name if market_state else "UNKNOWN"

        # Price and grid
        symbol = getattr(gt, "symbol", "N/A")
        price = getattr(gt, "current_market_price", None)
        price_text = f"{price:.8f}" if isinstance(price, (int, float)) else "N/A"
        grid_levels = gt.grid if getattr(gt, "grid", None) else []
        total_levels = len(grid_levels)
        live_orders = sum(1 for lvl in grid_levels if lvl.get("order_id"))
        buy_orders = sum(1 for lvl in grid_levels if lvl.get("order_id") and lvl.get("side") == "BUY")
        sell_orders = sum(1 for lvl in grid_levels if lvl.get("order_id") and lvl.get("side") == "SELL")

        # API mode
        api_mode = "WebSocket API" if getattr(gt, "using_websocket", False) else "REST API"

        # Risk manager
        rm = getattr(self, "risk_manager", None)
        risk_text = "N/A"
        if rm and getattr(rm, "is_active", False):
            sl = getattr(rm, "stop_loss_price", None)
            tp = getattr(rm, "take_profit_price", None)
            risk_text = f"Active | SL: {sl:.8f} | TP: {tp:.8f}" if isinstance(sl, (int, float)) and isinstance(tp, (int, float)) else "Active"
        elif rm:
            risk_text = "Inactive"

        # Compose message
        lines = [
            f"Status: {'RUNNING [ON]' if is_running else 'STOPPED [OFF]'}",
            f"Symbol: {symbol}",
            f"Price: {price_text}",
            f"Trend: {trend_val:+.2f} {trend_bar}",
            f"Market State: {market_state_label}",
            f"Grid: {live_orders}/{total_levels} live orders (Buy {buy_orders} / Sell {sell_orders})",
            f"API: {api_mode}",
            f"Risk: {risk_text}",
        ]
        return "\n".join(lines)

    def _trend_meter(self, value):
        """Render a simple ASCII meter for trend in range [-1, 1]."""
        width = 20
        # Map [-1,1] to [0,width]
        filled = int(round((value + 1) / 2 * width))
        filled = max(0, min(width, filled))
        bar = "[" + "=" * filled + "." * (width - filled) + "]"
        if value > 0.25:
            label = "Bullish"
        elif value < -0.25:
            label = "Bearish"
        else:
            label = "Neutral"
        return f"{bar} {label}"
    
    async def _handle_start_grid(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /startgrid command"""
        async def handler(update):
            if self.grid_trader:
                result = self.grid_trader.start()
                await update.message.reply_text(result)
                
                if self.grid_trader.is_running and self.risk_manager:
                    grid_prices = [level['price'] for level in self.grid_trader.grid]
                    self.risk_manager.activate(min(grid_prices), max(grid_prices))
            else:
                await update.message.reply_text("Grid trading system not initialized")
        await self._handle_authorized_command(update, handler)
    
    async def _handle_stop_grid(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /stopgrid command"""
        async def handler(update):
            if self.grid_trader:
                result = self.grid_trader.stop()
                await update.message.reply_text(result)
                
                if self.risk_manager:
                    self.risk_manager.deactivate()
            else:
                await update.message.reply_text("Grid trading system not initialized")
        await self._handle_authorized_command(update, handler)
    
    async def _handle_risk_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /risk command - show risk management status"""
        async def handler(update):
            if self.risk_manager:
                if self.risk_manager.is_active:
                    status_text = (
                        f"Risk management status: Active\n"
                        f"Stop loss price: {self.risk_manager.stop_loss_price}\n"
                        f"Take profit price: {self.risk_manager.take_profit_price}\n"
                        f"Historical high: {self.risk_manager.highest_price}\n"
                        f"Historical low: {self.risk_manager.lowest_price}"
                    )
                    if self.risk_manager.oco_order_id:
                        status_text += f"\nOCO order ID: {self.risk_manager.oco_order_id}"
                else:
                    status_text = "Risk management status: Inactive"
            else:
                status_text = "Risk management module not initialized"
            
            await update.message.reply_text(status_text)
        await self._handle_authorized_command(update, handler)
    
    async def _handle_set_symbol(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /setsymbol command to update the trading pair"""
        async def handler(update):
            # Check if command has arguments
            if not context.args or len(context.args) != 1:
                await update.message.reply_text(
                    "Please provide a valid symbol format: /setsymbol BTCUSDT"
                )
                return

            # Get the input symbol and convert to uppercase
            new_symbol = context.args[0].upper()
            
            # Basic validation of symbol format
            if not new_symbol or not isinstance(new_symbol, str) or len(new_symbol) < 5:
                await update.message.reply_text("Invalid symbol format. Example: BTCUSDT")
                return
            
            # Verify the symbol exists on Binance
            await update.message.reply_text(f"Verifying symbol {new_symbol}...")
            
            try:
                # Check if grid_trader is initialized
                if not self.grid_trader or not self.grid_trader.binance_client:
                    await update.message.reply_text("Error: Trading system not initialized properly")
                    return
                
                # Get symbol info to verify it exists
                symbol_info = self.grid_trader.binance_client.get_symbol_info(new_symbol)
                
                if not symbol_info:
                    await update.message.reply_text(f"Error: Symbol {new_symbol} not found on Binance")
                    return
                    
                # Check if the symbol is tradable
                if symbol_info.get('status') != 'TRADING':
                    await update.message.reply_text(
                        f"Error: Symbol {new_symbol} exists but is not currently tradable. "
                        f"Status: {symbol_info.get('status')}"
                    )
                    return
                    
                # Show symbol details
                min_notional = "unknown"
                for f in symbol_info.get('filters', []):
                    if f.get('filterType') == 'NOTIONAL':
                        min_notional = f.get('minNotional', 'unknown')
                        break
                
                info_message = (
                    f"Symbol: {new_symbol}\n"
                    f"Status: {symbol_info.get('status')}\n"
                    f"Price precision: {self.grid_trader.price_precision}\n"
                    f"Quantity precision: {self.grid_trader.quantity_precision}\n"
                    f"Minimum notional value: {min_notional}"
                )
                await update.message.reply_text(info_message)

                # Delegate further orchestration (cancel/restart streams etc.) to controller if available
                if self.on_symbol_change:
                    await update.message.reply_text("Applying symbol change to trading system...")
                    controller_message = self.on_symbol_change(new_symbol)
                    if controller_message:
                        await update.message.reply_text(controller_message)
                else:
                    await update.message.reply_text(
                        "Symbol verified but controller is not configured; please restart the bot to apply changes."
                    )
                
            except Exception as e:
                error_message = f"❌ Failed to update symbol: {str(e)}"
                self.logger.error(error_message)
                await update.message.reply_text(error_message)
        
        await self._handle_authorized_command(update, handler)
    
    async def _handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle regular messages"""
        user_id = update.effective_user.id
        if not self._is_user_authorized(user_id):
            return
        
        await update.message.reply_text("Please use /help to see available commands")
