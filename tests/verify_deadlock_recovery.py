import unittest
from unittest.mock import MagicMock, patch
import sys
import os
import json
import time

# Mock binance module before importing anything that uses it
sys.modules['binance'] = MagicMock()
sys.modules['binance.spot'] = MagicMock()
sys.modules['binance.error'] = MagicMock()
sys.modules['dotenv'] = MagicMock()
sys.modules['pandas'] = MagicMock()
sys.modules['numpy'] = MagicMock()

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.grid_trader import GridTrader
import config

class TestDeadlockRecovery(unittest.TestCase):
    def setUp(self):
        # Mock BinanceClient
        self.mock_client = MagicMock()
        self.mock_client.get_symbol_price.return_value = 100.0
        self.mock_client.check_balance.return_value = 1000.0 # Default rich
        self.mock_client.get_client_status.return_value = {"websocket_available": True}
        self.mock_client.get_symbol_info.return_value = {
            'symbol': 'BTCUSDT',
            'filters': [
                {'filterType': 'PRICE_FILTER', 'tickSize': '0.01'},
                {'filterType': 'LOT_SIZE', 'stepSize': '0.00001'}
            ]
        }
        
        # Mock config
        config.MIN_VIABLE_CAPITAL = 12
        config.SYMBOL = 'BTCUSDT'
        config.GRID_LEVELS = 10
        config.GRID_SPACING = 0.01
        config.MAX_GRID_SPACING = 0.05
        config.ATR_PERIOD = 14
        config.RECALCULATION_PERIOD = 1
        config.CAPITAL_PER_LEVEL = 100
        config.GRID_RANGE_PERCENT = 0.2
        config.MAX_GRID_RANGE = 0.5
        config.ENABLE_PROTECTIVE_MODE = False
        config.AUTO_PROTECTIVE_FOR_NON_MAJOR = True
        config.MAJOR_SYMBOLS = ['BTCUSDT']
        config.ATR_RATIO = 0.2
        config.TRADING_FEE_RATE = 0.001
        config.PROFIT_MARGIN_MULTIPLIER = 1.2
        config.MIN_NOTIONAL_VALUE = 5
        config.BUY_SELL_SPREAD = 0.005
        
        # Initialize GridTrader
        self.trader = GridTrader(self.mock_client)
        self.trader.logger = MagicMock()
        self.trader.symbol = 'BTCUSDT' # Explicitly set symbol if needed, though config should handle it
        
        # Clean up cooldown file
        self.cooldown_file = self.trader._get_cooldown_file_path()
        if os.path.exists(self.cooldown_file):
            os.remove(self.cooldown_file)

    def tearDown(self):
        if os.path.exists(self.cooldown_file):
            os.remove(self.cooldown_file)

    def test_cooldown_mechanism(self):
        print("\n--- Testing Cooldown Mechanism ---")
        
        # 1. Verify initial state (no cooldown)
        self.assertFalse(self.trader._check_market_order_cooldown())
        
        # 2. Save cooldown
        self.trader._save_cooldown_state()
        
        # 3. Verify cooldown active
        self.assertTrue(self.trader._check_market_order_cooldown())
        
        # 4. Verify persistence
        # Create new trader instance
        new_trader = GridTrader(self.mock_client)
        self.assertTrue(new_trader._check_market_order_cooldown())
        print("Cooldown persistence verified.")

    def test_all_usdt_scenarios(self):
        print("\n--- Testing All USDT Scenarios ---")
        
        # Setup: 1000 USDT, 0 BTC
        self.mock_client.check_balance.side_effect = lambda asset: 1000.0 if asset == 'USDT' else 0.0
        
        # Case 1: Trend < -0.4 (Crash) -> IDLE
        strategy = self.trader._handle_asset_depletion(max_buy=10, max_sell=0, trend_strength=-0.5)
        self.assertEqual(strategy, 'idle')
        print("All USDT + Crash (-0.5) -> IDLE: PASS")
        
        # Case 2: Trend -0.3 (Downtrend) -> ONE_SIDED_BUY
        strategy = self.trader._handle_asset_depletion(max_buy=10, max_sell=0, trend_strength=-0.3)
        self.assertEqual(strategy, 'one_sided_buy')
        print("All USDT + Downtrend (-0.3) -> ONE_SIDED_BUY: PASS")
        
        # Case 3: Trend 0.6 (Pump) -> RESET
        strategy = self.trader._handle_asset_depletion(max_buy=10, max_sell=0, trend_strength=0.6)
        self.assertEqual(strategy, 'reset')
        print("All USDT + Pump (0.6) -> RESET: PASS")

    def test_all_coin_scenarios(self):
        print("\n--- Testing All Coin Scenarios ---")
        
        # Setup: 0 USDT, 0.1 BTC (Price 100 -> Value 10) - Wait, need > 12 USDT value
        # Price 100, need 0.2 BTC = 20 USDT
        self.mock_client.check_balance.side_effect = lambda asset: 0.0 if asset == 'USDT' else 0.2
        self.mock_client.get_symbol_price.return_value = 100.0
        
        # Case 1: Trend < -0.5 (Crash) -> FULL LIQUIDATION
        strategy = self.trader._handle_asset_depletion(max_buy=0, max_sell=10, trend_strength=-0.6)
        self.assertEqual(strategy, 'reset_sell_all')
        print("All Coin + Crash (-0.6) -> RESET_SELL_ALL: PASS")
        
        # Case 2: Trend -0.3 (Downtrend) -> REDUCE 50%
        strategy = self.trader._handle_asset_depletion(max_buy=0, max_sell=10, trend_strength=-0.3)
        self.assertEqual(strategy, 'reduce_50')
        print("All Coin + Downtrend (-0.3) -> REDUCE_50: PASS")
        
        # Case 3: Trend 0.0 (Range/Up) -> ONE_SIDED_SELL
        strategy = self.trader._handle_asset_depletion(max_buy=0, max_sell=10, trend_strength=0.0)
        self.assertEqual(strategy, 'one_sided_sell')
        print("All Coin + Range (0.0) -> ONE_SIDED_SELL: PASS")

    @patch('core.grid_trader.GridTrader._calculate_max_buy_levels')
    @patch('core.grid_trader.GridTrader._calculate_max_sell_levels')
    @patch('core.grid_trader.GridTrader.calculate_market_metrics')
    @patch('core.grid_trader.GridTrader._create_core_zone_grid')
    @patch('core.grid_trader.GridTrader.calculate_optimal_grid_center')
    def test_calculate_grid_levels_integration(self, mock_optimal_center, mock_create_grid, mock_metrics, mock_max_sell, mock_max_buy):
        print("\n--- Testing _calculate_grid_levels Integration ---")
        
        # Setup Mocks
        mock_metrics.return_value = (1.0, -0.3) # ATR 1.0, Trend -0.3
        mock_max_buy.return_value = 10
        mock_max_sell.return_value = 0 # All USDT
        mock_optimal_center.return_value = (100.0, 0.1) # Center 100, Range 10%
        
        # Mock create grid to return dummy levels
        mock_create_grid.return_value = [{'price': 99, 'side': 'BUY', 'capital': 10}]
        
        # Setup Balances for All USDT
        self.mock_client.check_balance.side_effect = lambda asset: 1000.0 if asset == 'USDT' else 0.0
        
        # Execute
        # Should trigger 'one_sided_buy' strategy
        grid = self.trader._calculate_grid_levels()
        
        # Verify
        if len(grid) == 0:
             print("Logger Error Calls:", self.trader.logger.error.call_args_list)
             print("Logger Warning Calls:", self.trader.logger.warning.call_args_list)
        
        self.assertTrue(len(grid) > 0)
        self.assertEqual(grid[0]['side'], 'BUY')
        print("Integration Test (All USDT, Trend -0.3 -> One Sided Buy): PASS")

if __name__ == '__main__':
    unittest.main()
