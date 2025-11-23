
import unittest
from unittest.mock import MagicMock, patch
import sys
import os

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.grid_trader import GridTrader

class TestGridCalculation(unittest.TestCase):
    def setUp(self):
        # Patch config
        self.config_patcher = patch('core.grid_trader.config')
        self.mock_config = self.config_patcher.start()
        
        # Use configure_mock to set attributes as real values, not MagicMocks
        self.mock_config.configure_mock(**{
            'SYMBOL': "BTCUSDT",
            'GRID_LEVELS': 10,
            'RECALCULATION_PERIOD': 300,
            'ATR_PERIOD': 14,
            'ATR_RATIO': 0.5,
            'GRID_SPACING': 0.01,
            'MAX_GRID_SPACING': 0.05,
            'GRID_RANGE_PERCENT': 0.1,
            'MAX_GRID_RANGE': 0.2,
            'CAPITAL_PER_LEVEL': 10,
            'ENABLE_PROTECTIVE_MODE': False,
            'AUTO_PROTECTIVE_FOR_NON_MAJOR': True,
            'MAJOR_SYMBOLS': ["BTCUSDT", "ETHUSDT"],
            'CORE_ZONE_PERCENTAGE': 0.5,
            'MIN_NOTIONAL_VALUE': 5.0,
            'ENABLE_COMPOUND_INTEREST': False,
            'CAPITAL_PERCENTAGE_PER_LEVEL': 0.01,
            'MAX_CENTER_DEVIATION': 0.05,
            'CORE_CAPITAL_RATIO': 0.7,
            'LEVEL_REDUCTION_FACTOR': 1.0,
            'CORE_GRID_RATIO': 0.6,
            'FALLBACK_TICK_SIZE': 0.01,
            'FALLBACK_STEP_SIZE': 0.00001,
            'MIN_EXPECTED_PROFIT_BUFFER': 0.0,
            'MAX_ORDER_AGE_HOURS': 4,
            'PRICE_DEVIATION_THRESHOLD': 0.015,
            'PROTECTIVE_TREND_LEVEL_REDUCTION': 0.5,
            'PROTECTIVE_PAUSE_STRONG_TREND': True,
        })

        self.mock_client = MagicMock()
        
        # Mock client status
        self.mock_client.get_client_status.return_value = {"websocket_available": False}
        
        # Mock symbol info directly
        self.mock_client.get_symbol_info.return_value = {
            "symbol": "BTCUSDT",
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                {"filterType": "LOT_SIZE", "stepSize": "0.00001"}
            ]
        }
        
        # Mock exchange info (still good to have)
        self.mock_client.get_exchange_info.return_value = {
            "symbols": [{
                "symbol": "BTCUSDT",
                "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                    {"filterType": "LOT_SIZE", "stepSize": "0.00001"}
                ]
            }]
        }
        
        # Mock _get_current_atr to return a real number instead of MagicMock
        # This is called in __init__ and needs to return numeric value
        with patch.object(GridTrader, '_get_current_atr', return_value=100.0):
            self.grid_trader = GridTrader(self.mock_client)
        # self.grid_trader.symbol is now set from mock_config.SYMBOL
        self.grid_trader.logger = MagicMock()

    def tearDown(self):
        self.config_patcher.stop()

    def test_calculate_optimal_grid_center_sufficient_data(self):
        """Test that calculation uses sufficient historical data"""
        # Mock market metrics
        self.grid_trader.calculate_market_metrics = MagicMock(return_value=(100.0, 0.1))
        
        # Mock current price
        self.mock_client.get_symbol_price.return_value = 100.0
        
        # Mock historical klines (400 candles as per actual implementation)
        # Format: [open_time, open, high, low, close, volume, ...]
        mock_klines = [[0, 0, 0, 0, "100.0", 0] for _ in range(400)]
        self.grid_trader._get_cached_klines = MagicMock(return_value=mock_klines)
        
        # Run calculation
        center, range_val = self.grid_trader.calculate_optimal_grid_center()
        
        # Verify _get_cached_klines was called with correct limit (400 as per line 3322)
        self.grid_trader._get_cached_klines.assert_called_with(
            symbol="BTCUSDT",
            interval="4h",
            limit=400
        )
        
        # Verify center is close to 100 (should be exactly 100 with constant price)
        self.assertAlmostEqual(center, 100.0, places=2)

    def test_calculate_optimal_grid_center_insufficient_data_fallback(self):
        """Test fallback when insufficient data is returned"""
        self.grid_trader.calculate_market_metrics = MagicMock(return_value=(100.0, 0.1))
        self.mock_client.get_symbol_price.return_value = 100.0
        
        # Return only 10 candles
        mock_klines = [[0, 0, 0, 0, "100.0", 0] for _ in range(10)]
        self.grid_trader._get_cached_klines = MagicMock(return_value=mock_klines)
        
        center, range_val = self.grid_trader.calculate_optimal_grid_center()
        
        # Should fallback to current price
        self.assertEqual(center, 100.0)
        # Should verify warning logged
        self.grid_trader.logger.warning.assert_called()

if __name__ == '__main__':
    unittest.main()
