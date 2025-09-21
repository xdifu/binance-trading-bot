import logging
import sys
from pathlib import Path
from unittest import mock

import types

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

if "binance" not in sys.modules:
    binance_module = types.ModuleType("binance")
    sys.modules["binance"] = binance_module
else:
    binance_module = sys.modules["binance"]

if "binance.spot" not in sys.modules:
    spot_module = types.ModuleType("binance.spot")

    class DummySpot:  # pragma: no cover - simple stub
        def __init__(self, *args, **kwargs):
            pass

    spot_module.Spot = DummySpot
    sys.modules["binance.spot"] = spot_module
    binance_module.spot = spot_module

if "binance.error" not in sys.modules:
    error_module = types.ModuleType("binance.error")

    class DummyClientError(Exception):
        pass

    error_module.ClientError = DummyClientError
    sys.modules["binance.error"] = error_module
    binance_module.error = error_module

if "binance.exceptions" not in sys.modules:
    exceptions_module = types.ModuleType("binance.exceptions")

    class DummyBinanceAPIException(Exception):
        pass

    exceptions_module.BinanceAPIException = DummyBinanceAPIException
    sys.modules["binance.exceptions"] = exceptions_module
    binance_module.exceptions = exceptions_module

from core.grid_trader import GridTrader


class FakeBinanceClient:
    def __init__(self):
        self.balance = 1000

    def get_client_status(self):
        return {"websocket_available": False}

    def get_symbol_info(self, symbol):
        return {
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                {"filterType": "LOT_SIZE", "stepSize": "0.001"},
            ]
        }

    def get_symbol_price(self, symbol):
        return 100.0

    def get_historical_klines(self, symbol, interval, limit):
        return [[0, 0, 0, 0, 100.0] for _ in range(limit)]

    def check_balance(self, asset):
        return self.balance


@pytest.fixture
def patched_trader():
    client = FakeBinanceClient()
    with mock.patch.object(GridTrader, "_get_current_atr", return_value=0.01), \
        mock.patch.object(GridTrader, "calculate_market_metrics", return_value=(0.01, 0.0)), \
        mock.patch.object(GridTrader, "calculate_optimal_grid_center", return_value=(100.0, 0.12)):
        trader = GridTrader(client)
        trader.logger = logging.getLogger("grid_trader_test")
        yield trader, client


def test_grid_levels_restore_to_target_when_balance_recovers(patched_trader):
    trader, client = patched_trader

    # Reduce available balance to enforce a lower grid level count
    client.balance = trader.capital_per_level * 0.8 * 4
    reduced_grid = trader._calculate_grid_levels()

    assert trader.current_grid_levels < trader.config_grid_levels
    assert len(reduced_grid) == trader.current_grid_levels

    # Restore balance so that the configured grid level target is affordable again
    client.balance = trader.capital_per_level * 0.8 * (trader.config_grid_levels + 1)
    restored_grid = trader._calculate_grid_levels()

    assert trader.current_grid_levels == trader.config_grid_levels
    assert len(restored_grid) == trader.config_grid_levels
