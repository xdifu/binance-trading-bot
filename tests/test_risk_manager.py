import sys
import types
import time
import logging
import unittest

# Provide lightweight stubs for the optional binance modules used by BinanceClient
if "binance" not in sys.modules:
    binance_module = types.ModuleType("binance")
    spot_module = types.ModuleType("binance.spot")
    error_module = types.ModuleType("binance.error")

    class DummySpot:
        def __init__(self, *args, **kwargs):
            # Minimal stub that accepts arbitrary parameters
            pass

    class DummyClientError(Exception):
        def __init__(self, *args, **kwargs):
            super().__init__(*args)
            self.error_code = kwargs.get("error_code")

    spot_module.Spot = DummySpot
    error_module.ClientError = DummyClientError
    binance_module.spot = spot_module
    binance_module.error = error_module
    sys.modules["binance"] = binance_module
    sys.modules["binance.spot"] = spot_module
    sys.modules["binance.error"] = error_module

from binance_api.client import BinanceClient
from core.risk_manager import RiskManager
import config


class DummyRestClient:
    def __init__(self):
        self.captured_kwargs = None

    def new_oco_order(self, *args, **kwargs):
        self.captured_kwargs = kwargs
        return {"orderListId": 987654321, "orders": []}


class DummyBinanceClient(BinanceClient):
    def __init__(self):
        self.logger = logging.getLogger("DummyBinanceClient")
        self.websocket_available = False
        self.ws_client = None
        self.rest_client = DummyRestClient()
        self.time_offset = 0
        self.last_time_sync = int(time.time())
        self.time_sync_interval = 60 * 60
        self.symbol_price = 10.0
        self.asset = config.SYMBOL.replace("USDT", "")
        self._symbol_info = {
            "symbol": config.SYMBOL,
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.01000000"},
                {"filterType": "LOT_SIZE", "stepSize": "0.00100000"},
                {
                    "filterType": "PERCENT_PRICE_BY_SIDE",
                    "bidMultiplierUp": "1.2",
                    "askMultiplierDown": "0.8",
                },
            ],
        }

    def _sync_time(self):
        self.last_time_sync = int(time.time())
        return True

    def get_client_status(self):
        return {"websocket_available": False, "rest_available": True}

    def get_symbol_price(self, symbol):
        return self.symbol_price

    def get_symbol_info(self, symbol):
        return self._symbol_info

    def get_account_info(self):
        return {"balances": [{"asset": self.asset, "free": "5"}]}


class RiskManagerOCOOrderTest(unittest.TestCase):
    def test_rest_fallback_creates_oco_order(self):
        client = DummyBinanceClient()
        manager = RiskManager(client)

        created = manager._place_oco_orders()

        self.assertTrue(created)
        self.assertEqual(manager.oco_order_id, 987654321)
        self.assertIsNotNone(client.rest_client.captured_kwargs)
        self.assertNotIn("aboveType", client.rest_client.captured_kwargs)
        self.assertNotIn("belowType", client.rest_client.captured_kwargs)
        self.assertIn("timestamp", client.rest_client.captured_kwargs)


if __name__ == "__main__":
    unittest.main()
