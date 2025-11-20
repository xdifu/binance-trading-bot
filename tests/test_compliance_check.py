import unittest
from unittest.mock import MagicMock, patch
import logging
import sys
from pathlib import Path
import types

# Ensure project root is in path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Mock binance module
if "binance" not in sys.modules:
    binance_module = types.ModuleType("binance")
    spot_module = types.ModuleType("binance.spot")
    error_module = types.ModuleType("binance.error")
    exceptions_module = types.ModuleType("binance.exceptions")
    
    class DummySpot:
        def __init__(self, *args, **kwargs): pass
        
    class DummyClientError(Exception):
        def __init__(self, error_code=None, error_message=None, header=None):
            self.error_code = error_code
            self.error_message = error_message
            self.header = header
            
    class DummyBinanceAPIException(Exception):
        def __init__(self, response, status_code, text):
            self.code = 0
            self.message = text

    spot_module.Spot = DummySpot
    error_module.ClientError = DummyClientError
    exceptions_module.BinanceAPIException = DummyBinanceAPIException
    
    binance_module.spot = spot_module
    binance_module.error = error_module
    binance_module.exceptions = exceptions_module
    binance_module.lib = types.SimpleNamespace(utils=types.SimpleNamespace(websocket_api_signature=lambda api_key, api_secret, params: params))
    
    sys.modules["binance"] = binance_module
    sys.modules["binance.spot"] = spot_module
    sys.modules["binance.error"] = error_module
    sys.modules["binance.exceptions"] = exceptions_module

# Mock dotenv
if "dotenv" not in sys.modules:
    dotenv_module = types.ModuleType("dotenv")
    dotenv_module.load_dotenv = lambda *args, **kwargs: True
    sys.modules["dotenv"] = dotenv_module

# Mock config
if "config" not in sys.modules:
    config_module = types.ModuleType("config")
    config_module.BINANCE_API_KEY = "test_key"
    config_module.BINANCE_API_SECRET = "test_secret"
    config_module.BINANCE_TESTNET = True
    sys.modules["config"] = config_module

# Mock websocket
if "websocket" not in sys.modules:
    websocket_module = types.ModuleType("websocket")
    websocket_module.WebSocketApp = MagicMock
    class WebSocketConnectionClosedException(Exception): pass
    websocket_module.WebSocketConnectionClosedException = WebSocketConnectionClosedException
    websocket_module.ABNF = types.SimpleNamespace(OPCODE_TEXT=1, OPCODE_BINARY=2)
    sys.modules["websocket"] = websocket_module

# Mock requests
if "requests" not in sys.modules:
    requests_module = types.ModuleType("requests")
    requests_module.Session = MagicMock
    sys.modules["requests"] = requests_module

from binance_api.client import BinanceClient
from binance_api.websocket_api_client import BinanceWSClient

class TestOCOCompliance(unittest.TestCase):
    def setUp(self):
        self.logger = logging.getLogger("test_compliance")
        
    def test_oco_parameter_translation_sell(self):
        """Test that REST-style parameters are translated to WS-style for SELL OCO"""
        # Setup WS client
        ws_client = BinanceWSClient.__new__(BinanceWSClient)
        ws_client.logger = self.logger
        ws_client.client = MagicMock()
        ws_client.client._send_signed_request.return_value = "req-1"
        ws_client.client._wait_for_response.return_value = {"status": 200, "result": {"orderListId": 1}}
        
        # Call new_oco_order with REST-style params
        # SELL: Limit (Maker) is ABOVE, Stop is BELOW
        # limitIcebergQty -> aboveIcebergQty
        # stopIcebergQty -> belowIcebergQty
        ws_client.new_oco_order(
            symbol="BTCUSDT",
            side="SELL",
            quantity=1.0,
            price=50000,
            stopPrice=49000,
            limitIcebergQty=0.5,
            stopIcebergQty=0.6,
            limitClientOrderId="my_limit_id",
            stopClientOrderId="my_stop_id"
        )
        
        # Verify arguments passed to _send_signed_request
        call_args = ws_client.client._send_signed_request.call_args
        self.assertIsNotNone(call_args)
        method, params = call_args[0]
        
        self.assertEqual(method, "orderList.place.oco")
        # Check translation
        self.assertEqual(params.get("aboveIcebergQty"), 0.5, "limitIcebergQty should map to aboveIcebergQty for SELL")
        self.assertEqual(params.get("belowIcebergQty"), 0.6, "stopIcebergQty should map to belowIcebergQty for SELL")
        self.assertEqual(params.get("aboveClientOrderId"), "my_limit_id")
        self.assertEqual(params.get("belowClientOrderId"), "my_stop_id")

    def test_oco_parameter_translation_buy(self):
        """Test that REST-style parameters are translated to WS-style for BUY OCO"""
        # Setup WS client
        ws_client = BinanceWSClient.__new__(BinanceWSClient)
        ws_client.logger = self.logger
        ws_client.client = MagicMock()
        ws_client.client._send_signed_request.return_value = "req-2"
        ws_client.client._wait_for_response.return_value = {"status": 200, "result": {"orderListId": 2}}
        
        # Call new_oco_order with REST-style params
        # BUY: Limit (Maker) is BELOW, Stop is ABOVE
        # limitIcebergQty -> belowIcebergQty
        # stopIcebergQty -> aboveIcebergQty
        ws_client.new_oco_order(
            symbol="BTCUSDT",
            side="BUY",
            quantity=1.0,
            price=40000,
            stopPrice=41000,
            limitIcebergQty=0.5,
            stopIcebergQty=0.6
        )
        
        # Verify arguments passed to _send_signed_request
        call_args = ws_client.client._send_signed_request.call_args
        self.assertIsNotNone(call_args)
        method, params = call_args[0]
        
        self.assertEqual(method, "orderList.place.oco")
        # Check translation
        self.assertEqual(params.get("belowIcebergQty"), 0.5, "limitIcebergQty should map to belowIcebergQty for BUY")
        self.assertEqual(params.get("aboveIcebergQty"), 0.6, "stopIcebergQty should map to aboveIcebergQty for BUY")

if __name__ == '__main__':
    unittest.main()
