import sys
import types
import threading
from queue import Queue
from unittest.mock import MagicMock

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

    response = client.unsubscribe_user_data_stream()

    client._send_request.assert_called_once_with("userDataStream.unsubscribe", None)
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
