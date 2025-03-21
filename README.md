# Binance Grid Trading Bot

A Python-based cryptocurrency grid trading bot for Binance that implements a static grid trading strategy with Telegram notifications and controls.

![Python Version](https://img.shields.io/badge/python-3.8+-blue.svg)
![License](https://img.shields.io/badge/license-MIT-green.svg)

## 📋 Features

- **Grid Trading Strategy**: Implements an automated BTC/USDT grid trading system
- **Risk Management**: Includes trailing stop-loss and take-profit features
- **Telegram Integration**: Control the bot and receive updates via Telegram
- **Binance API**: Uses official Binance API with Ed25519 key authentication
- **Automatic Grid Recalculation**: Adjusts grid based on market volatility using ATR

## 🚀 Getting Started

### Prerequisites

- Python 3.8 or higher
- Binance account with API access
- Telegram bot token (optional, but recommended)
- Ubuntu 24.04 (recommended OS)

### Installation

1. Clone this repository:
   ```bash
   git clone https://github.com/yourusername/binance-trading-bot.git
   cd binance-trading-bot
   ```

2. Create and activate a virtual environment:
   ```bash
   python -m venv venv
   source venv/bin/activate   # On Windows use: venv\Scripts\activate
   ```

3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

4. Set up API keys:
   - Copy the `.env.example` file to `.env` and fill in your Binance API credentials
   - Generate Ed25519 keys for Binance API authentication (see Binance documentation)
   - Store your private key in the `key/` directory

### Configuration

Edit the `config.py` file to customize your trading strategy:

```python
# Trading settings
SYMBOL = "BTCUSDT"              # Trading pair
GRID_LEVELS = 6                 # Number of grid levels (5-8 recommended)
GRID_SPACING = 0.7              # Grid spacing in percentage (0.6-0.8% recommended)
CAPITAL_PER_LEVEL = 10          # Capital per grid level in USDT (minimum $10)
GRID_RANGE_PERCENT = 4          # Grid range in percentage (±3-5% recommended)
RECALCULATION_PERIOD = 7        # Days between grid recalculations
ATR_PERIOD = 14                 # ATR calculation period

# Risk management settings
TRAILING_STOP_LOSS_PERCENT = 5  # Trailing stop-loss percentage
TRAILING_TAKE_PROFIT_PERCENT = 2 # Trailing take-profit percentage
```

## 🔧 Usage

### Starting the Bot

Run the bot with:

```bash
python main.py
```

### Telegram Commands

Once configured, the bot responds to these Telegram commands:

- `/start` - Welcome message and basic info
- `/help` - List available commands
- `/status` - Show grid trading status
- `/startgrid` - Activate grid trading
- `/stopgrid` - Deactivate grid trading
- `/risk` - Display risk management status

## ⚙️ How It Works

### Grid Trading Strategy

The bot implements a static grid trading strategy:

1. Creates a grid of buy and sell orders around the current market price
2. When a buy order is filled, it places a sell order above that price
3. When a sell order is filled, it places a buy order below that price
4. Recalculates the grid weekly based on 14-day ATR volatility

### Risk Management

The bot includes risk management features:
- **Trailing Stop-Loss**: 5% below the lowest grid level
- **Trailing Take-Profit**: 2% above the highest grid level

Both are implemented as OCO (One-Cancels-the-Other) orders for efficient execution.

## 📊 Project Structure

```
grid_trading_bot/
├── main.py                   # Entry point
├── config.py                 # Configuration settings
├── .env                      # Environment variables (API keys, etc.)
├── binance_api/              # Binance API client and websocket
│   ├── client.py             # Binance API client wrapper
│   └── websocket_manager.py  # WebSocket connection manager
├── core/                     # Core trading logic
│   ├── grid_trader.py        # Grid trading implementation
│   └── risk_manager.py       # Risk management implementation
├── tg_bot/                   # Telegram bot integration
│   └── bot.py                # Telegram bot implementation
├── utils/                    # Utility functions
│   ├── indicators.py         # Technical indicators (ATR, etc.)
│   └── format_utils.py       # Formatting utilities for prices, etc.
└── key/                      # API key storage
    ├── new_public_key.pem    # Ed25519 public key
    └── new_private_key.pem   # Ed25519 private key
```

## ⚠️ Disclaimer

This bot is provided for educational and research purposes only. Use at your own risk. Cryptocurrency trading involves significant risk and you should never invest money you cannot afford to lose.

## 📝 License

This project is licensed under the MIT License - see the LICENSE file for details.

## 🙏 Acknowledgements

- [Binance API](https://github.com/binance/binance-spot-api-docs)
- [python-binance](https://github.com/sammchardy/python-binance)
- [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot)