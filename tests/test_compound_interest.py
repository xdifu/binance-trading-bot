import unittest
from unittest.mock import MagicMock, patch
import sys
import os

# Mock binance module before importing project modules
sys.modules['binance'] = MagicMock()
sys.modules['binance.spot'] = MagicMock()
sys.modules['binance.error'] = MagicMock()
sys.modules['binance.websocket'] = MagicMock()
sys.modules['binance.websocket.spot'] = MagicMock()
sys.modules['binance.websocket.spot.websocket_client'] = MagicMock()
sys.modules['dotenv'] = MagicMock()
sys.modules['pandas'] = MagicMock()
sys.modules['numpy'] = MagicMock()
sys.modules['utils.indicators'] = MagicMock()

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.grid_trader import GridTrader
import config

class TestCompoundInterest(unittest.TestCase):
    def setUp(self):
        self.mock_client = MagicMock()
        
        # Mock get_symbol_info for initialization
        self.mock_client.get_symbol_info.return_value = {
            'filters': [
                {'filterType': 'PRICE_FILTER', 'tickSize': '0.01'},
                {'filterType': 'LOT_SIZE', 'stepSize': '0.001'}
            ]
        }
        
        # Mock calculate_atr to return a float
        sys.modules['utils.indicators'].calculate_atr.return_value = 1.0
        
        # Start patches for config
        self.config_patcher = patch.object(config, 'ENABLE_COMPOUND_INTEREST', True)
        self.config_patcher.start()
        
        self.capital_patcher = patch.object(config, 'CAPITAL_PERCENTAGE_PER_LEVEL', 0.01)
        self.capital_patcher.start()
        
        self.min_notional_patcher = patch.object(config, 'MIN_NOTIONAL_VALUE', 5)
        self.min_notional_patcher.start()
        
        self.symbol_patcher = patch.object(config, 'SYMBOL', "ZECUSDT")
        self.symbol_patcher.start()
        
        # Mock client methods
        self.mock_client.get_symbol_price.return_value = 100.0
        self.mock_client.check_balance.side_effect = lambda asset: 1000.0 if asset == 'USDT' else 0.0
        
        self.trader = GridTrader(self.mock_client)

    def tearDown(self):
        patch.stopall()

    def test_compound_interest_calculation(self):
        # Scenario 1: 1000 USDT balance, 0 Base
        # Total Value = 1000
        # Expected Capital = 1000 * 0.01 = 10.0
        
        self.trader._calculate_grid_levels()
        self.assertAlmostEqual(self.trader.capital_per_level, 10.0)
        print(f"Test 1 Passed: Capital {self.trader.capital_per_level} matches expected 10.0")

        # Scenario 2: Balance increases to 2000 USDT
        self.mock_client.check_balance.side_effect = lambda asset: 2000.0 if asset == 'USDT' else 0.0
        
        self.trader._calculate_grid_levels()
        self.assertAlmostEqual(self.trader.capital_per_level, 20.0)
        print(f"Test 2 Passed: Capital {self.trader.capital_per_level} matches expected 20.0")
        
        # Scenario 3: Mixed Balance (1000 USDT + 10 Base @ 100.0)
        # Total Value = 1000 + (10 * 100) = 2000
        # Expected Capital = 20.0
        self.mock_client.check_balance.side_effect = lambda asset: 1000.0 if asset == 'USDT' else 10.0
        
        self.trader._calculate_grid_levels()
        self.assertAlmostEqual(self.trader.capital_per_level, 20.0)
        print(f"Test 3 Passed: Capital {self.trader.capital_per_level} matches expected 20.0")

    def test_min_capital_enforcement(self):
        # Scenario: Small balance (100 USDT)
        # Calculated = 1.0
        # Min Notional = 5
        # Safe Min = 5 * 1.1 = 5.5
        
        self.mock_client.check_balance.side_effect = lambda asset: 100.0 if asset == 'USDT' else 0.0
        
        self.trader._calculate_grid_levels()
        self.assertAlmostEqual(self.trader.capital_per_level, 5.5)
        print(f"Test 4 Passed: Capital {self.trader.capital_per_level} enforced to min 5.5")

if __name__ == '__main__':
    unittest.main()
