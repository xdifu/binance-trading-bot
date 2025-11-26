# AGENTS Guidelines / 行为准则

## Goal / 目标
- Always develop/modify by following official sources: `binance-spot-api-docs` (especially `binance-spot-api-docs/web-socket-api.md` and its testnet version) and `binance-connector-python`.
- Use WebSocket API by default for trading and data streams; REST is only a last-resort fallback after WS is confirmed unavailable and retries fail.

## Authoritative Sources / 权威来源
- `binance-spot-api-docs`: endpoints, request/response format, signatures, status/error handling.
- `binance-connector-python`: official code patterns for signing (HMAC/RSA/Ed25519), request sending, response unwrapping.

## Coding Rules / 编码规则
- Before any code change or plan update, align with the above sources; if conflicts arise, follow the official docs and reflect that in code.
- WebSocket endpoints must be official (testnet `wss://ws-api.testnet.binance.vision/ws-api/v3`, mainnet `wss://ws-api.binance.com/ws-api/v3`); WS responses must be unpacked using the `status + result` structure.
- Signing/auth must support all key types in docs; timestamp/`recvWindow` and related parameters must match official requirements.
- For user data streams, orders/order books/market data, fields/params/errors must match docs; OCO cancel must use `orderList.cancel`; streaming/order flows prefer WS.
- WS calls need retries and availability checks; only downgrade to REST after multiple WS failures, never downgrade prematurely.

## Testing & Verification / 测试与验证
- Tests should cover WS method names/params, `status/result` response unwrapping, WS→REST fallback switching, and signing/timestamp logic, using `binance-connector-python` test patterns as reference.
- If full tests cannot run, explicitly document the validation gaps and risks.
- When running tests locally, use the project venv at `grid_trading_bot/venv` (e.g., `grid_trading_bot/venv/bin/python -m pytest` from repo root).
具体指令是 cd /home/god/Binance/grid_trading_bot && ./venv/bin/python -m pytest

## Change Notes / 变更说明
- In summaries or commits, cite the relevant official sections/examples and state whether WS-first with REST fallback meets these guidelines.
