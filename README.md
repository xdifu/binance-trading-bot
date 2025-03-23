# Binance Grid Trading Bot

A robust, fully-featured cryptocurrency grid trading bot for Binance that implements an automated static grid trading strategy with real-time notifications and Telegram controls.

![Python Version](https://img.shields.io/badge/python-3.8+-blue.svg)
![License](https://img.shields.io/badge/license-MIT-green.svg)
![Binance API](https://img.shields.io/badge/Binance%20API-v3-orange.svg)
![Status](https://img.shields.io/badge/status-active-success.svg)

<div align="center">
  <img src="https://user-images.githubusercontent.com/YOUR_GITHUB_ID/grid-trading-diagram.png" alt="Grid Trading Diagram" width="600">
  <p><i>Conceptual diagram of grid trading strategy</i></p>
</div>

## 📋 Features

- **Adaptive Grid Trading**: Implements a dynamic BTC/USDT grid trading system that adjusts to market volatility
- **Advanced Risk Management**: Includes trailing stop-loss and take-profit with automatic activation
- **Telegram Integration**: Control the bot and receive real-time trading updates via Telegram
- **High Performance**: Uses WebSocket streams for real-time market data with REST API fallback
- **Modern Security**: Implements Ed25519 key authentication (Binance's recommended method)
- **Intelligent Grid Recalculation**: Periodically adjusts grid based on market volatility using ATR
- **Robust Error Handling**: Comprehensive error recovery with automatic reconnection
- **Performance Optimized**: High-performance message serialization with `msgspec`

## 🚀 Getting Started

### What is Grid Trading?

Grid trading is a strategy that places multiple buy and sell orders at incrementally increasing and decreasing price levels (a "grid"). When prices move up and hit a sell order, the bot automatically places a new buy order below. When prices move down and hit a buy order, the bot places a new sell order above.

This strategy profits from natural price oscillations without needing to predict market direction.

### Prerequisites

- Python 3.8 or higher
- Binance account with API access ([How to create Binance API keys](https://www.binance.com/en/support/faq/360002502072))
- Ed25519 API keys (recommended) or standard HMAC API keys
- Telegram bot token (optional, but recommended for monitoring)
- Ubuntu 24.04 or similar Linux distribution (recommended)

### Installation

1. Clone this repository:
   ```bash
   git clone https://github.com/yourusername/binance-grid-trading-bot.git
   cd binance-grid-trading-bot
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
   - Copy the `.env.example` file to .env
   ```bash
   cp .env.example .env
   ```
   - Fill in your Binance API credentials in the .env file
   - For Ed25519 keys, store your private key in the `key/` directory

### Configuration

Edit the config.py file to customize your trading strategy:

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

#### Configuration Guidelines for Beginners

- **Start Small**: Begin with smaller capital amounts (e.g., CAPITAL_PER_LEVEL = 5)
- **Conservative Grid**: Use fewer grid levels (4-5) and tighter range (±2-3%) when starting
- **Test First**: Consider starting with a testnet account by setting `USE_TESTNET=true` in your .env file

## 🔧 Usage

### Starting the Bot

Run the bot with:

```bash
python main.py
```

The bot will:
1. Connect to Binance API
2. Initialize WebSocket connections for real-time data
3. Start the Telegram bot (if configured)
4. Wait for your commands to start trading

### Telegram Commands

Once configured, the bot responds to these Telegram commands:

| Command | Description |
|---------|-------------|
| `/start` | Welcome message and basic information |
| `/help` | List available commands |
| `/status` | Show current grid trading status and active orders |
| `/startgrid` | Activate grid trading |
| `/stopgrid` | Deactivate grid trading |
| `/risk` | Display risk management status |

## ⚙️ How It Works

### Grid Trading Strategy

The bot implements a static grid trading strategy that creates a grid of orders around the current price:

1. **Grid Creation**: Places evenly-spaced buy orders below current price and sell orders above
2. **Order Execution**: When a buy order fills, places a sell order above that price
3. **Continuous Cycle**: When a sell order fills, places a buy order below that price
4. **Adaptive Recalculation**: Recalculates grid weekly based on 14-day ATR volatility

<div align="center">
  <img src="https://user-images.githubusercontent.com/YOUR_GITHUB_ID/grid-process-diagram.png" alt="Grid Process Flow" width="700">
</div>

### Risk Management

The bot includes advanced risk management features:

- **Trailing Stop-Loss**: 5% below lowest grid level, automatically adjusts as price moves up
- **Trailing Take-Profit**: 2% above highest grid level, adjusts higher as price increases
- **OCO Orders**: Implemented as One-Cancels-the-Other orders for efficient execution

### WebSocket Implementation

The bot uses WebSocket connections for:
- Real-time market data (price updates)
- Order execution and updates
- User data streams (account updates)

This provides significantly faster execution compared to REST API polling with automatic fallback to REST API if WebSocket is unavailable.

## 📊 Project Structure

```
grid_trading_bot/
├── main.py                   # Entry point with main bot logic
├── config.py                 # Configuration settings
├── .env                      # Environment variables (API keys, etc.)
├── binance_api/              # Binance API integration
│   ├── client.py             # Unified client with WebSocket/REST
│   ├── websocket_api_client.py  # WebSocket API implementation
│   └── websocket_manager.py  # WebSocket market data streams
├── core/                     # Core trading modules
│   ├── grid_trader.py        # Grid trading implementation
│   └── risk_manager.py       # Risk management implementation
├── tg_bot/                   # Telegram bot integration
│   └── bot.py                # Telegram bot implementation
├── utils/                    # Utility functions
│   ├── indicators.py         # Technical indicators (ATR, etc.)
│   └── format_utils.py       # Formatting utilities for prices, etc.
└── key/                      # API key storage (Ed25519 keys)
    ├── new_public_key.pem    # Ed25519 public key
    └── new_private_key.pem   # Ed25519 private key
```

## 🔒 Security Best Practices

1. **API Key Permissions**: Set API keys to only have trading permissions, not withdrawal permissions
2. **Private Key Protection**: Store private keys securely and never share them
3. **Ed25519 Keys**: Use Ed25519 keys (Binance's recommended authentication) when possible
4. **IP Restrictions**: Set IP restrictions on your API keys in Binance settings

## 🔍 Troubleshooting

### Common Issues

1. **"Invalid timestamp" errors**: 
   - Ensure your system clock is synchronized
   - The bot attempts to handle time synchronization automatically

2. **Order placement failures**:
   - Check if you have sufficient balance
   - Verify trading pair has enough liquidity
   - Ensure price/quantity meet minimum requirements

3. **WebSocket connection issues**:
   - The bot will automatically fall back to REST API
   - Check your internet connection stability
   - Verify firewall settings allow WebSocket connections

### Logging

The bot generates detailed logs in grid_bot.log. When reporting issues, please include relevant log sections.

## ❓ FAQ

**Q: How much capital do I need to start?**  
A: You can start with as little as $50-100, but recommended minimum is $200 to create an effective grid.

**Q: Is this bot suitable for beginners?**  
A: Yes, but you should understand basic trading concepts and start with small amounts.

**Q: Which trading pairs work best?**  
A: Liquid pairs like BTC/USDT work well. Avoid highly volatile or low-liquidity pairs.

**Q: How do I choose grid parameters?**  
A: Start conservative: 5-6 grid levels with 0.7% spacing and ±3% range. Adjust based on performance.

## 🤝 Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

1. Fork the repository
2. Create your feature branch: `git checkout -b feature/amazing-feature`
3. Commit your changes: `git commit -m 'Add some amazing feature'`
4. Push to the branch: `git push origin feature/amazing-feature`
5. Open a Pull Request

## ⚠️ Disclaimer

This bot is provided for educational and research purposes only. Use at your own risk. Cryptocurrency trading involves significant risk and you should never invest money you cannot afford to lose.

The authors and contributors are not responsible for any financial losses or damages incurred from using this software.

## 📝 License

This project is licensed under the MIT License - see the LICENSE file for details.

## 🙏 Acknowledgements

- [Binance API Documentation](https://binance-docs.github.io/apidocs/spot/en/)
- [python-binance](https://github.com/sammchardy/python-binance)
- [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot)
- [msgspec](https://github.com/jcrist/msgspec) for high-performance serialization

---
🚀 **Happy Trading!** 🚀