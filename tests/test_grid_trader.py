import logging
import threading
import types
import sys
import unittest
from unittest.mock import MagicMock, patch

binance_module = types.ModuleType("binance")
spot_module = types.ModuleType("binance.spot")


class DummySpot:
    def __init__(self, *args, **kwargs):
        pass


spot_module.Spot = DummySpot

error_module = types.ModuleType("binance.error")


class DummyClientError(Exception):
    pass


error_module.ClientError = DummyClientError

exceptions_module = types.ModuleType("binance.exceptions")


class DummyBinanceAPIException(Exception):
    pass


exceptions_module.BinanceAPIException = DummyBinanceAPIException
binance_module.spot = spot_module
binance_module.error = error_module
binance_module.exceptions = exceptions_module

sys.modules.setdefault("binance", binance_module)
sys.modules["binance.spot"] = spot_module
sys.modules["binance.error"] = error_module
sys.modules["binance.exceptions"] = exceptions_module

from core.grid_trader import GridTrader


class DummyBot:
    def __init__(self, risk_manager):
        self.risk_manager = risk_manager
        self.send_message = MagicMock()


def create_risk_manager():
    risk_manager = MagicMock()
    risk_manager.is_active = True
    risk_manager._place_oco_orders = MagicMock(return_value=True)

    def trigger_oco():
        risk_manager._place_oco_orders()
        return True

    risk_manager._check_for_missing_oco_orders = MagicMock(side_effect=trigger_oco)
    return risk_manager


def create_trader(simulation_mode, risk_manager):
    trader = GridTrader.__new__(GridTrader)
    trader.logger = logging.getLogger(f"grid_trader_test.{id(trader)}")
    trader.logger.setLevel(logging.INFO)
    trader.binance_client = MagicMock()
    trader.binance_client.place_limit_order = MagicMock(return_value={'orderId': 987654})
    trader.binance_client.check_balance = MagicMock(return_value=1000)
    trader.binance_client.get_client_status = MagicMock(return_value={'websocket_available': False})
    trader.telegram_bot = DummyBot(risk_manager)
    trader.symbol = "BTCUSDT"
    trader.simulation_mode = simulation_mode
    trader.using_websocket = False
    trader.pending_orders = {}
    trader.pending_locks = {}
    trader.balance_lock = threading.RLock()
    trader.tick_size = 0.01
    trader.price_precision = 2
    trader.quantity_precision = 6
    
    # Safe formatters that handle both float and MagicMock
    def safe_format_qty(qty):
        try:
            return f"{float(qty):.6f}"
        except (TypeError, ValueError):
            return "0.000000"
    
    def safe_format_price(price):
        try:
            return f"{float(price):.2f}"
        except (TypeError, ValueError):
            return "0.00"
    
    trader._adjust_quantity_precision = MagicMock(side_effect=safe_format_qty)
    trader._adjust_price_precision = MagicMock(side_effect=safe_format_price)
    trader._calculate_dynamic_capital_for_level = MagicMock(return_value=10)
    trader._place_replacement_grid_order = MagicMock()
    trader._lock_funds = MagicMock(return_value=True)
    trader._release_funds = MagicMock()
    trader.pending_orders = {}
    trader.pending_locks = {}
    return trader


class ProcessFilledOrderRiskTests(unittest.TestCase):
    @patch('core.grid_trader.config')
    def test_risk_manager_triggered_after_sell_fill_live_mode(self, mock_config):
        # Setup config mocks
        mock_config.BUY_SELL_SPREAD = 0.005
        mock_config.TRADING_FEE_RATE = 0.001
        mock_config.PROFIT_MARGIN_MULTIPLIER = 1.2
        mock_config.MIN_EXPECTED_PROFIT_BUFFER = 0.0
        mock_config.MIN_NOTIONAL_VALUE = 5.0
        
        risk_manager = create_risk_manager()
        trader = create_trader(simulation_mode=False, risk_manager=risk_manager)
        trader.grid = [{'order_id': 111, 'side': 'SELL', 'price': 100.0}]

        with self.assertLogs(trader.logger, level='INFO') as log_ctx:
            result = trader._process_filled_order(trader.grid[0], 0, 'SELL', 0.5, 100.0)

        self.assertTrue(result)
        risk_manager._check_for_missing_oco_orders.assert_called_once()
        risk_manager._place_oco_orders.assert_called_once()
        self.assertTrue(
            any("SELL order filled, triggering OCO order update" in message for message in log_ctx.output)
        )
        trader.binance_client.place_limit_order.assert_called_once()

    @patch('core.grid_trader.config')
    def test_risk_manager_triggered_after_sell_fill_simulation_mode(self, mock_config):
        # Setup config mocks
        mock_config.BUY_SELL_SPREAD = 0.005
        mock_config.TRADING_FEE_RATE = 0.001
        mock_config.PROFIT_MARGIN_MULTIPLIER = 1.2
        mock_config.MIN_EXPECTED_PROFIT_BUFFER = 0.0
        mock_config.MIN_NOTIONAL_VALUE = 5.0
        risk_manager = create_risk_manager()
        trader = create_trader(simulation_mode=True, risk_manager=risk_manager)
        trader.grid = [{'order_id': 222, 'side': 'SELL', 'price': 100.0}]

        with patch('core.grid_trader.time.time', return_value=1234567):
            with self.assertLogs(trader.logger, level='INFO') as log_ctx:
                result = trader._process_filled_order(trader.grid[0], 0, 'SELL', 0.5, 100.0)

        self.assertTrue(result)
        risk_manager._check_for_missing_oco_orders.assert_called_once()
        risk_manager._place_oco_orders.assert_called_once()
        self.assertTrue(
            any("SELL order filled, triggering OCO order update" in message for message in log_ctx.output)
        )
        trader.binance_client.place_limit_order.assert_not_called()


if __name__ == '__main__':
    unittest.main()
