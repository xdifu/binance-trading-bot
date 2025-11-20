import sys
import unittest
from unittest.mock import MagicMock, patch
from pathlib import Path

# Ensure project root is in path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from binance_api.client import BinanceClient
from binance.error import ClientError

class TestBinanceClient(unittest.TestCase):
    def setUp(self):
        # Mock environment variables
        self.env_patcher = patch.dict('os.environ', {
            'API_KEY': 'test_api_key',
            'API_SECRET': 'test_api_secret'
        })
        self.env_patcher.start()
        
        # Mock config
        self.config_patcher = patch('binance_api.client.config')
        self.mock_config = self.config_patcher.start()
        self.mock_config.API_KEY = 'test_api_key'
        self.mock_config.API_SECRET = 'test_api_secret'
        self.mock_config.BASE_URL = 'https://api.binance.com'
        self.mock_config.USE_TESTNET = False
        
        # Mock Spot client
        self.spot_patcher = patch('binance_api.client.Spot')
        self.mock_spot_cls = self.spot_patcher.start()
        self.mock_rest_client = MagicMock()
        self.mock_spot_cls.return_value = self.mock_rest_client
        
        # Mock BinanceWSClient
        self.ws_patcher = patch('binance_api.client.BinanceWSClient')
        self.mock_ws_cls = self.ws_patcher.start()
        self.mock_ws_client = MagicMock()
        self.mock_ws_cls.return_value = self.mock_ws_client
        # Mock the inner client and its ping_server method
        self.mock_ws_client.client.ping_server.return_value = {'status': 200}
        self.mock_ws_client.client.get_server_time.return_value = {'status': 200, 'result': {'serverTime': 1600000000000}}
        
        # Mock REST client time
        self.mock_rest_client.time.return_value = {'serverTime': 1600000000000}
        
        # Set time_offset to int to avoid MagicMock issues
        self.mock_ws_client.client.time_offset = 0

    def tearDown(self):
        self.env_patcher.stop()
        self.config_patcher.stop()
        self.spot_patcher.stop()
        self.ws_patcher.stop()

    def test_init_ws_available(self):
        """Test initialization when WS is available"""
        with patch('binance_api.client.WEBSOCKET_API_AVAILABLE', True):
            client = BinanceClient()
            self.assertTrue(client.websocket_available)
            self.assertIsNotNone(client.ws_client)
            self.assertIsNotNone(client.rest_client)

    def test_init_ws_unavailable(self):
        """Test initialization when WS connection fails"""
        self.mock_ws_client.client.ping_server.return_value = {'status': 500}
        with patch('binance_api.client.WEBSOCKET_API_AVAILABLE', True):
            client = BinanceClient()
            self.assertFalse(client.websocket_available)
            # Should still have REST client
            self.assertIsNotNone(client.rest_client)

    def test_fallback_logic_success_ws(self):
        """Test successful execution via WS"""
        with patch('binance_api.client.WEBSOCKET_API_AVAILABLE', True):
            client = BinanceClient()
            client.websocket_available = True
            
            # Mock WS method (BinanceClient calls new_order on WS client for place_limit_order)
            self.mock_ws_client.new_order.return_value = {'status': 200, 'result': {'orderId': 1}}
            
            result = client.place_limit_order('BTCUSDT', 'BUY', 1.0, 50000.0)
            
            self.mock_ws_client.new_order.assert_called_once()
            self.mock_rest_client.new_order.assert_not_called()
            self.assertEqual(result, {'orderId': 1})

    def test_fallback_logic_ws_failure_retry_success(self):
        """Test WS failure followed by successful retry"""
        with patch('binance_api.client.WEBSOCKET_API_AVAILABLE', True):
            client = BinanceClient()
            client.websocket_available = True
            
            # Mock WS method to fail once then succeed
            self.mock_ws_client.new_order.side_effect = [
                Exception("Connection error"),
                {'status': 200, 'result': {'orderId': 1}}
            ]
            # Mock ping to succeed
            self.mock_ws_client.client.ping_server.return_value = {'status': 200}
            
            result = client.place_limit_order('BTCUSDT', 'BUY', 1.0, 50000.0)
            
            self.assertEqual(self.mock_ws_client.new_order.call_count, 2)
            self.mock_rest_client.new_order.assert_not_called()
            self.assertEqual(result, {'orderId': 1})

    def test_fallback_logic_ws_failure_fallback_rest(self):
        """Test WS failure falling back to REST"""
        with patch('binance_api.client.WEBSOCKET_API_AVAILABLE', True):
            client = BinanceClient()
            client.websocket_available = True
            
            # Mock WS method to fail consistently
            self.mock_ws_client.new_order.side_effect = Exception("Connection error")
            # Mock ping to FAIL
            self.mock_ws_client.client.ping_server.return_value = {'status': 500}
            
            # Mock REST method
            self.mock_rest_client.new_order.return_value = {'orderId': 1}
            
            result = client.place_limit_order('BTCUSDT', 'BUY', 1.0, 50000.0)
            
            self.assertEqual(self.mock_ws_client.new_order.call_count, 2) # 2 retries
            self.mock_rest_client.new_order.assert_called_once()
            self.assertFalse(client.websocket_available) # Should be marked unavailable
            self.assertEqual(result, {'orderId': 1})

    def test_new_oco_order_params(self):
        """Test new_oco_order parameter passing"""
        with patch('binance_api.client.WEBSOCKET_API_AVAILABLE', True):
            client = BinanceClient()
            client.websocket_available = True
            
            # Mock WS method
            self.mock_ws_client.new_oco_order.return_value = {'status': 200, 'result': {'orderListId': 1}}
            
            # Call with standard params
            result = client.new_oco_order(
                symbol='BTCUSDT',
                side='SELL',
                quantity=1.0,
                price=50000.0,
                stopPrice=49000.0,
                stopLimitPrice=48900.0
            )
            print(f"OCO Result: {result}")
            
            # Verify WS client received correct params
            call_args = self.mock_ws_client.new_oco_order.call_args
            if call_args is None:
                self.fail("new_oco_order was not called on WS client")
            
            self.assertEqual(call_args.kwargs['symbol'], 'BTCUSDT')
            self.assertEqual(call_args.kwargs['side'], 'SELL')
            # Note: quantities/prices are formatted to strings in the client method
            self.assertIn('quantity', call_args.kwargs)
            self.assertIn('price', call_args.kwargs)
            self.assertIn('stopPrice', call_args.kwargs)
            self.assertIn('stopLimitPrice', call_args.kwargs)

    def test_cancel_order(self):
        """Test cancel_order"""
        with patch('binance_api.client.WEBSOCKET_API_AVAILABLE', True):
            client = BinanceClient()
            client.websocket_available = True
            
            self.mock_ws_client.cancel_order.return_value = {'status': 200, 'result': {'orderId': 1, 'status': 'CANCELED'}}
            
            result = client.cancel_order('BTCUSDT', 1)
            
            self.mock_ws_client.cancel_order.assert_called_with(symbol='BTCUSDT', orderId=1)
            self.assertEqual(result['status'], 'CANCELED')

    def test_get_open_orders(self):
        """Test get_open_orders"""
        with patch('binance_api.client.WEBSOCKET_API_AVAILABLE', True):
            client = BinanceClient()
            client.websocket_available = True
            
            self.mock_ws_client.get_open_orders.return_value = {'status': 200, 'result': [{'orderId': 1}]}
            
            result = client.get_open_orders('BTCUSDT')
            
            self.mock_ws_client.get_open_orders.assert_called_with(symbol='BTCUSDT')
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]['orderId'], 1)

    def test_get_account_info(self):
        """Test get_account_info"""
        with patch('binance_api.client.WEBSOCKET_API_AVAILABLE', True):
            client = BinanceClient()
            client.websocket_available = True
            
            self.mock_ws_client.account.return_value = {'status': 200, 'result': {'balances': []}}
            
            result = client.get_account_info()
            
            self.mock_ws_client.account.assert_called_once()
            self.assertEqual(result, {'balances': []})

    def test_get_exchange_info(self):
        """Test get_exchange_info"""
        with patch('binance_api.client.WEBSOCKET_API_AVAILABLE', True):
            client = BinanceClient()
            client.websocket_available = True
            
            self.mock_ws_client.exchange_info.return_value = {'status': 200, 'result': {'symbols': []}}
            
            result = client.get_exchange_info('BTCUSDT')
            
            self.mock_ws_client.exchange_info.assert_called_with(symbol='BTCUSDT')
            self.assertEqual(result, {'symbols': []})

    def test_get_symbol_price(self):
        """Test get_symbol_price"""
        with patch('binance_api.client.WEBSOCKET_API_AVAILABLE', True):
            client = BinanceClient()
            client.websocket_available = True
            
            self.mock_ws_client.ticker_price.return_value = {'status': 200, 'result': {'symbol': 'BTCUSDT', 'price': '50000.00'}}
            
            price = client.get_symbol_price('BTCUSDT')
            
            self.mock_ws_client.ticker_price.assert_called_with(symbol='BTCUSDT')
            self.assertEqual(price, 50000.0)

if __name__ == '__main__':
    unittest.main()
