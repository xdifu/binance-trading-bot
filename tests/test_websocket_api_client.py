import sys
import types
import threading
import json
from queue import Queue
from unittest.mock import MagicMock
import base64

import pytest

# Ensure project root on path
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Provide lightweight stubs for external binance modules imported by websocket_api_client
if "binance" not in sys.modules:
    binance_module = types.ModuleType("binance")
    binance_module.lib = types.SimpleNamespace(utils=types.SimpleNamespace(websocket_api_signature=lambda api_key, api_secret, params: params))
    sys.modules["binance"] = binance_module

import logging
from binance_api.websocket_api_client import BinanceWebSocketAPIClient, BinanceWSClient
from cryptography.hazmat.primitives.asymmetric import ed25519


def _build_client_stub():
    """
    Build a BinanceWebSocketAPIClient without hitting network by bypassing __init__.
    Only injects the attributes needed for tested methods.
    """
    client = BinanceWebSocketAPIClient.__new__(BinanceWebSocketAPIClient)
    client.session_authenticated = False
    client.logger = logging.getLogger("ws_api_client_test")
    client.lock = threading.Lock()
    client.request_callbacks = {}
    client.response_queue = Queue()
    client.event_callback = None
    client.user_stream_active = False
    client.user_stream_subscription_id = None
    client._user_stream_keepalive_stop = threading.Event()
    client._user_stream_keepalive_thread = None
    return client


def test_subscribe_user_stream_uses_plain_method_when_authenticated():
    client = _build_client_stub()
    client.session_authenticated = True
    client._send_request = MagicMock(return_value="req123")
    client._wait_for_response = MagicMock(return_value={"status": 200, "result": {"subscriptionId": 9}})

    response = client.subscribe_user_data_stream()

    client._send_request.assert_called_once_with("userDataStream.subscribe")
    assert client.user_stream_active is True
    assert client.user_stream_subscription_id == 9
    assert response["status"] == 200


def test_subscribe_user_stream_signature_when_not_authenticated():
    client = _build_client_stub()
    client.session_authenticated = False
    client._send_signed_request = MagicMock(return_value="req999")
    client._wait_for_response = MagicMock(return_value={"status": 200, "result": {"subscriptionId": 3}})

    response = client.subscribe_user_data_stream()

    client._send_signed_request.assert_called_once_with("userDataStream.subscribe.signature")
    assert client.user_stream_active is True
    assert client.user_stream_subscription_id == 3
    assert response["status"] == 200


def test_unsubscribe_user_stream_resets_flags():
    client = _build_client_stub()
    client.user_stream_active = True
    client.user_stream_subscription_id = 5
    client._send_request = MagicMock(return_value="req444")
    client._wait_for_response = MagicMock(return_value={"status": 200})
    client._stop_user_stream_keepalive = MagicMock()

    response = client.unsubscribe_user_data_stream()

    client._send_request.assert_called_once_with("userDataStream.unsubscribe", None)
    client._stop_user_stream_keepalive.assert_called_once()
    assert client.user_stream_active is False
    assert client.user_stream_subscription_id is None
    assert response["status"] == 200


def test_event_callback_receives_payload_and_subscription_id():
    callback = MagicMock()
    client = _build_client_stub()
    client.event_callback = callback
    message = {"event": {"e": "outboundAccountPosition"}, "subscriptionId": 7}

    # Use internal message handler to mirror live behaviour
    client._handle_message = BinanceWebSocketAPIClient._handle_message.__get__(client, BinanceWebSocketAPIClient)
    client._handle_message(json_message := '{"event":{"e":"outboundAccountPosition"},"subscriptionId":7}')

    callback.assert_called_once_with(message["event"], message["subscriptionId"])


def test_event_callback_handles_raw_user_event_without_wrapper():
    callback = MagicMock()
    client = _build_client_stub()
    client.event_callback = callback

    client._handle_message = BinanceWebSocketAPIClient._handle_message.__get__(client, BinanceWebSocketAPIClient)
    payload = {"e": "executionReport", "s": "BTCUSDT"}
    client._handle_message(json.dumps(payload))

    callback.assert_called_once_with(payload, None)


def test_cancel_oco_order_uses_order_list_cancel_method():
    ws_adapter = BinanceWSClient.__new__(BinanceWSClient)
    ws_adapter.logger = logging.getLogger("ws_cancel_test")
    mock_inner = types.SimpleNamespace()
    mock_inner._send_signed_request = MagicMock(return_value="req-cancel")
    mock_inner._wait_for_response = MagicMock(return_value={"status": 200})
    ws_adapter.client = mock_inner

    result = ws_adapter.cancel_oco_order(orderListId=123)

    mock_inner._send_signed_request.assert_called_once_with("orderList.cancel", {"orderListId": 123})
    assert result["status"] == 200


def test_signed_request_prefers_ed25519_signature(monkeypatch):
    client = _build_client_stub()
    client.api_key = "test-key"
    client.api_secret = "hmac-should-not-be-used"
    client.private_key = ed25519.Ed25519PrivateKey.generate()
    client.get_adjusted_timestamp = MagicMock(return_value=1234567890123)

    captured = {}

    def fake_send_request(method, params, callback=None):
        captured["method"] = method
        captured["params"] = params
        return "req-abc"

    client._send_request = fake_send_request

    request_id = client._send_signed_request("order.place", {"symbol": "BTCUSDT"})
    assert request_id == "req-abc"

    # Ensure apiKey and signature exist and decode with Ed25519 public key
    signed_params = captured["params"]
    assert signed_params["apiKey"] == "test-key"
    assert "signature" in signed_params

    # Recreate the message that was signed (sorted, excluding signature)
    unsigned_items = sorted((k, v) for k, v in signed_params.items() if k != "signature")
    query_string = "&".join(f"{k}={v}" for k, v in unsigned_items)
    signature_bytes = base64.b64decode(signed_params["signature"])
    client.private_key.public_key().verify(signature_bytes, query_string.encode("utf-8"))


def test_oco_order_sell_parameter_mapping():
    """Verify SELL OCO orders correctly map price/stopPrice to above/below params"""
    ws_adapter = BinanceWSClient.__new__(BinanceWSClient)
    ws_adapter.logger = logging.getLogger("oco_test")
    
    # Create mock inner client
    mock_inner = types.SimpleNamespace()
    captured_params = {}
    
    def fake_signed_request(method, params):
        captured_params["method"] = method
        captured_params["params"] = params
        return "req-sell-oco"
    
    mock_inner._send_signed_request = fake_signed_request
    mock_inner._wait_for_response = MagicMock(return_value={"status": 200, "result": {"orderListId": 123}})
    ws_adapter.client = mock_inner
    
    # Place SELL OCO order
    result = ws_adapter.new_oco_order(
        symbol="BTCUSDT",
        side="SELL",
        quantity=1.0,
        price=50000,  # Take profit (limit) - should be abovePrice
        stopPrice=49000,  # Stop loss trigger - should be belowStopPrice
        stopLimitPrice=48900  # Stop limit execution - should be belowPrice
    )
    
    # Verify method name
    assert captured_params["method"] == "orderList.place.oco"
    
    # Verify parameter mapping for SELL
    params = captured_params["params"]
    assert params["symbol"] == "BTCUSDT"
    assert params["side"] == "SELL"
    assert params["quantity"] == "1.0"  # Converted to string
    
    # SELL: above leg = take profit (limit), below leg = stop loss
    assert params["aboveType"] == "LIMIT_MAKER"
    assert params["abovePrice"] == "50000"
    assert params["belowType"] == "STOP_LOSS_LIMIT"
    assert params["belowStopPrice"] == "49000"
    assert params["belowPrice"] == "48900"
    
    assert result["status"] == 200


def test_oco_order_buy_parameter_mapping():
    """Verify BUY OCO orders correctly invert leg assignment"""
    ws_adapter = BinanceWSClient.__new__(BinanceWSClient)
    ws_adapter.logger = logging.getLogger("oco_test")
    
    # Create mock inner client
    mock_inner = types.SimpleNamespace()
    captured_params = {}
    
    def fake_signed_request(method, params):
        captured_params["method"] = method
        captured_params["params"] = params
        return "req-buy-oco"
    
    mock_inner._send_signed_request = fake_signed_request
    mock_inner._wait_for_response = MagicMock(return_value={"status": 200, "result": {"orderListId": 456}})
    ws_adapter.client = mock_inner
    
    # Place BUY OCO order
    result = ws_adapter.new_oco_order(
        symbol="ETHUSDT",
        side="BUY",
        quantity=2.0,
        price=3000,  # Take profit (limit) - should be belowPrice
        stopPrice=3200,  # Stop loss trigger - should be aboveStopPrice
        stopLimitPrice=3250  # Stop limit execution - should be abovePrice
    )
    
    # Verify parameter mapping for BUY (inverted from SELL)
    params = captured_params["params"]
    assert params["symbol"] == "ETHUSDT"
    assert params["side"] == "BUY"
    
    # BUY: above leg = stop loss, below leg = take profit (limit)
    assert params["aboveType"] == "STOP_LOSS_LIMIT"
    assert params["aboveStopPrice"] == "3200"
    assert params["abovePrice"] == "3250"
    assert params["belowType"] == "LIMIT_MAKER"
    assert params["belowPrice"] == "3000"
    
    assert result["status"] == 200


def test_optional_parameters_account():
    """Verify account() method accepts optional parameters"""
    ws_adapter = BinanceWSClient.__new__(BinanceWSClient)
    ws_adapter.logger = logging.getLogger("param_test")
    
    # Create mock inner client
    mock_inner = types.SimpleNamespace()
    captured_params = {}
    
    def fake_signed_request(method, params):
        captured_params["method"] = method
        captured_params["params"] = params if params else {}
        return "req-account"
    
    mock_inner._send_signed_request = fake_signed_request
    mock_inner._wait_for_response = MagicMock(return_value={"status": 200, "result": {"balances": []}})
    ws_adapter.client = mock_inner
    
    # Test with optional parameters
    result = ws_adapter.account(omitZeroBalances=True, recvWindow=10000)
    
    params = captured_params["params"]
    assert params["omitZeroBalances"] is True
    assert params["recvWindow"] == 10000
    assert result["status"] == 200


def test_optional_parameters_get_open_orders():
    """Verify get_open_orders() method accepts recvWindow parameter"""
    ws_adapter = BinanceWSClient.__new__(BinanceWSClient)
    ws_adapter.logger = logging.getLogger("param_test")
    
    # Create mock inner client
    mock_inner = types.SimpleNamespace()
    captured_params = {}
    
    def fake_signed_request(method, params):
        captured_params["method"] = method
        captured_params["params"] = params if params else {}
        return "req-orders"
    
    mock_inner._send_signed_request = fake_signed_request
    mock_inner._wait_for_response = MagicMock(return_value={"status": 200, "result": []})
    ws_adapter.client = mock_inner
    
    # Test with optional recvWindow
    result = ws_adapter.get_open_orders(symbol="BTCUSDT", recvWindow=15000)
    
    params = captured_params["params"]
    assert params["symbol"] == "BTCUSDT"
    assert params["recvWindow"] == 15000
    assert result["status"] == 200
