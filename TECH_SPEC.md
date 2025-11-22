# Grid Trading Bot Technical Specification

## 1. Design Philosophy (设计哲学)

### Objective
本系统的核心目标是在**保证资金安全**的前提下，通过**波动率适应性网格**（Volatility-Adaptive Grid）在震荡市场中捕获高频收益。系统旨在自动化执行“低买高卖”策略，同时具备极强的抗风险能力，能够在极端行情（如暴涨暴跌）中自动熔断或调整策略，以保护本金。

### Core Principles

*   **Deterministic Execution (确定性执行)**
    *   系统采用**本地状态管理**作为单一事实来源（Source of Truth），通过 `threading.RLock` 严格控制对共享资源（如 `self.grid`, `self.pending_orders`, `self.is_running`）的并发访问。
    *   在 `main.py` 的 `GridTradingBot` 类中，`state_lock` 确保了状态变更（如启动、停止、更新配置）的原子性，避免了多线程环境下的竞态条件。
    *   所有订单操作在执行前都会先在本地进行逻辑验证（如资金检查、价格合理性检查），确保发出的指令是确定且有效的。

*   **Resilience First (韧性优先)**
    *   **Hybrid Connectivity**: 系统实现了 WebSocket API 优先、REST API 兜底的双模通信机制。在 `BinanceClient._execute_with_fallback` 中，优先尝试低延迟的 WebSocket 调用；若发生连接级错误（如 `WebSocketConnectionClosedException`），则自动降级为高可靠的 REST API，确保业务连续性。
    *   **Watchdog Mechanism**: `main.py` 中的 `_grid_maintenance_thread` 作为守护线程，以 60 秒间隔运行，执行差异化健康检查：网格重算（每5分钟）、未成交订单修复（每60秒）、OCO风控单验证（每5分钟）。

*   **Capital Safety (资金安全)**
    *   **Pending Locks**: 在 `GridTrader` 中引入 `pending_locks` 机制，在订单发送前预先锁定对应资产。这防止了在异步订单确认延迟期间，同一笔资金被重复使用（Double Spending）。
    *   **OCO Protection**: `RiskManager` 利用 Binance 的 OCO (One-Cancels-the-Other) 订单类型，原子化地部署止损单（Stop-Loss）和止盈单（Take-Profit）。这意味着止损和止盈互斥，一旦一方触发，另一方自动撤销，从交易所撮合引擎层面保证了风控的绝对执行。

## 2. System Architecture (系统架构)

### Lifecycle Orchestration
系统的生命周期由 `main.py` 中的 `GridTradingBot` 类主导，采用多线程协作模式：

1.  **Initialization**: `GridTradingBot` 实例化 `BinanceClient`, `GridTrader`, `RiskManager` 和 `TelegramBot`。
2.  **Main Loop**: 主线程进入 `while True` 循环，主要负责响应系统信号（如 `KeyboardInterrupt`）和维持进程存活。
3.  **Watchdog Threads**:
    *   **`_grid_maintenance_thread`**: 核心守护线程。
        *   每 `GRID_RECALCULATION_INTERVAL` (5分钟) 触发 `grid_trader.check_grid_recalculation()`，评估是否需要重置网格。
        *   每 60 秒触发 `grid_trader._check_for_unfilled_grid_slots()`，修复因网络波动导致的漏单。
        *   每 5 分钟触发 `risk_manager._check_for_missing_oco_orders()`，确保风控单在线。
    *   **`_keep_alive_listen_key_thread`**: 每 30 分钟通过 REST API 延长 ListenKey 有效期，维持 User Data Stream 的心跳。
4.  **Event-Driven Updates**: `MarketDataWebsocketManager` 接收 WebSocket 推送（Kline, ExecutionReport, OutboundAccountPosition），并通过回调函数 (`_handle_websocket_message`) 实时驱动 `GridTrader` 和 `RiskManager` 更新状态。

### Module Interaction

*   **`GridTrader`**: 负责核心交易策略。它持有 `BinanceClient` 用于下单，持有 `TelegramBot` 用于通知。它维护 `self.grid` 状态，根据市场行情计算买卖点。
*   **`RiskManager`**: 负责资金保护。它监控实时价格，当价格触及 `Stop Loss` 或 `Take Profit` 时，触发 OCO 订单执行，并强制调用 `GridTrader.stop()` 熔断交易。
*   **`BinanceClient`**: 基础设施层。封装了底层 API 调用，向上传递统一的接口，向下处理协议差异（WS vs REST）和签名逻辑。
*   **`TelegramBot`**: 交互层。接收用户指令（如 `/status`, `/stop`），并将其转发给 `GridTradingBot` 执行。

## 3. Core Trading Logic & Algorithms (核心交易逻辑与算法)

### 3.1 Market State Detection (市场状态检测)
在 `core/grid_trader.py` 中，`calculate_market_metrics` 方法负责计算关键指标，这些指标随后被用于判断市场状态：

*   **Metrics Calculation**:
    $$ \text{Trend} = 0.2 \cdot T_{15m} + 0.5 \cdot T_{1h} + 0.3 \cdot T_{4h} $$
    系统采用多时间框架加权平均（Multi-Timeframe Weighted Average）来计算趋势强度，结合 15分钟 ATR (Average True Range) 评估波动率。
*   **State Classification**:
    *   **RANGING (震荡)**: 默认状态。当趋势强度弱且波动率适中时。
    *   **BREAKOUT (突破)**: 当趋势强度超过阈值且成交量显著放大时。
    *   **PUMP/CRASH**: 极端行情状态，通常由短期均线与长期均线的剧烈偏离及 RSI 指标触发。

### 3.2 Optimal Grid Calculation (最优网格计算)
`_calculate_grid_levels` 方法摒弃了简单的“当前价格即中心”的逻辑，采用均值回归思想：

*   **Grid Center Theory** (`calculate_optimal_grid_center`):
    $$ P_{\text{center}} = P_{\text{historical}} \times (1 - w_{\text{current}}) + P_{\text{current}} \times w_{\text{current}} $$
    其中 $w_{\text{current}}$ 主要根据**价格偏离度**（Price Deviation）动态调整，基础值为 0.8。当当前价格显著偏离历史中心时（Deviation > 15%），$w_{\text{current}}$ 提升至 0.85 以快速跟随行情；反之则保持 0.80 以保留均值回归特性。趋势强度（Trend Strength）主要影响 $P_{\text{historical}}$ 的内部构成权重。


*   **Asymmetric Distribution (非对称分布)**:
    系统将网格划分为 **Core Zone (核心区)** 和 **Edge Zone (边缘区)**。
    *   **Core Zone**: 范围定义为 $P_{\text{center}} \pm \text{CORE\_ZONE\_PERCENTAGE}$。在此区域内,网格密度更高,且资金分配系数 $\alpha > 1.0$ (如 1.5x),以最大化高频成交收益。
    *   **Edge Zone**: 远离中心区域,网格稀疏,资金分配系数 $\alpha < 1.0$,主要用于捕捉极端行情下的反弹或回调,降低资金占用。

*   **Dynamic Position Sizing (动态仓位管理 - 复利机制)**:
    当启用 `ENABLE_COMPOUND_INTEREST = True` 时,系统实现自动复利增长:
    *   **Dynamic Capital Calculation**:
        $$ C_{\text{per\_level}} = \max(C_{\text{min}}, V_{\text{available}} \times R_{\text{capital}}) $$
        其中:
        *   $V_{\text{available}} = (B_{\text{USDT}} - L_{\text{USDT}}) + (B_{\text{base}} - L_{\text{base}}) \times P_{\text{current}}$ (可用账户价值, 扣除锁定资金)
        *   $R_{\text{capital}}$ = `CAPITAL_PERCENTAGE_PER_LEVEL` (默认 0.01 即 1%)
        *   $C_{\text{min}} = \text{MIN\_NOTIONAL\_VALUE} \times 1.1$ (交易所最小订单要求 + 10% 安全边际)
    *   **Execution Timing**: 每次网格重算时 (24小时周期或价格突破触发),系统动态调整 `capital_per_level`。
    *   **Risk Management**: 
        *   **盈利时**: 利润自动滚入本金,放大后续网格订单金额,实现复利增长。
        *   **亏损时**: 仓位自动缩减 (反马丁格尔策略),降低风险暴露,延长生存周期。
        *   **Solvency Check**: 系统通过 `_calculate_max_buy_levels` 和 `_calculate_max_sell_levels` 严格验证资金充足性,自动调整网格层数以防止透支,优先保证偿付能力 (Solvency) 而非维持固定网格数量。


### 3.3 Order Lifecycle Management (订单生命周期)
*   **Placement**:
    *   调用 `_place_grid_order` 时，首先使用 `format_price` 将价格修正为符合 `PRICE_FILTER` 的精度。
    *   检查 `MIN_NOTIONAL` (如 5 USDT)，若订单价值不足，自动跳过或合并，避免 API 报错 `-1013 Filter failure: MIN_NOTIONAL`。
*   **Execution & Regeneration**:
    *   当收到 `FILLED` 事件（`ExecutionReport`）时，`handle_order_update` 被触发，首先将该层级的 `order_id` 清零，避免重复处理。
    *   **Opposite Order Logic**:
        *   若买单成交 ($P_{\text{buy}}$)，系统在相邻网格层级挂出卖单，价格为： $P_{\text{sell}} = P_{\text{buy}} \times (1 + \text{BUY\_SELL\_SPREAD})$，其中 `BUY_SELL_SPREAD` = 0.0025 (0.25%)。
        *   **Profit Margin Check**: 执行前验证 $\text{effective\_profit} > \text{round\_trip\_fee} \times \text{PROFIT\_MARGIN\_MULTIPLIER}$，其中 `effective_profit = expected_profit - round_trip_fee - slippage`，确保净利润高于双边手续费的 2 倍。

## 4. Risk Management System (风险管理系统)

### OCO Mechanics
`RiskManager` 利用 Binance OCO 订单实现原子化风控：
*   **Structure**: 一个 OCO 订单包含一个限价单（Limit Maker，用于止盈）和一个止损限价单（Stop-Loss Limit，用于止损）。
*   **Execution**:
    *   **Take Profit**: 当价格上涨至 $P_{\text{TP}}$，Limit Maker 单成交，Stop-Loss 单自动撤销。
    *   **Stop Loss**: 当价格下跌至 $P_{\text{SL\_Trigger}}$，Stop-Loss Limit 单被激活并提交到撮合引擎，Limit Maker 单自动撤销。

### Trailing Logic (追踪机制)
`check_price` 函数实现了动态追踪止损：
*   **Trigger Condition**:
    $$ \frac{P_{\text{new\_SL}} - P_{\text{curr\_SL}}}{P_{\text{curr\_SL}}} > \text{Threshold}_{\text{update}} $$
    仅当新的止损位（基于当前最高价计算）比当前止损位高出一定阈值（如 `min_update_threshold_percent` = 0.5%）时，才触发更新。
*   **Cooling Period**: 引入 `min_update_interval_seconds` (如 300s)，防止在剧烈波动中频繁撤单重发 OCO 导致触发 API Rate Limit。

### Volatility Adjustment
*   当 `ATR` (平均真实波幅) 飙升，指示市场进入高波动状态时，系统自动收紧风控参数：
    *   $\text{Stop Loss \%} = \text{Original SL \%} \cdot 0.8$ (收紧 20%)
    *   这能更早地截断亏损，防止在流动性枯竭的暴跌中滑点过大。

## 5. Infrastructure & Resilience (基础设施与韧性)

### Hybrid Connectivity
`BinanceClient` 封装了双层通信架构：
1.  **Primary**: `BinanceWSClient` (WebSocket API)。支持全双工通信，延迟极低。用于高频的行情获取 (`bookTicker`) 和订单操作 (`place_order`)。
2.  **Fallback**: `Spot` (REST API)。当 WebSocket 连接断开、心跳超时或请求失败（非业务逻辑错误）时，`_execute_with_fallback` 捕获异常并无缝切换至 REST API。
3.  **Recovery**: 系统会在后台尝试重连 WebSocket，一旦成功，自动切回 Primary 模式。

### Time Synchronization
时间同步是签名的关键。`tools/diagnosis_time_sync.py` 和 `client.py` 实现了高精度同步：
*   **Algorithm**:
    $$ \text{Offset} = T_{\text{server}} - (T_{\text{local\_req}} + \frac{\text{RTT}}{2}) $$
    系统采集多次样本，取 RTT (Round Trip Time) 最小的一次计算 `time_offset`。
*   **Safety Margin**: 在生成请求 `timestamp` 时，不仅加上 `time_offset`，还会额外减去 500ms-1000ms 的安全边际，确保请求到达服务器时不会因“未来时间”而被拒绝 (`-1021 Timestamp for this request is outside of the recvWindow`)。

### Authentication
*   **Ed25519**: 优先支持 Ed25519 算法，通过 `cryptography` 库进行非对称加密签名，安全性高于 HMAC。
*   **HMAC-SHA256**: 作为兼容方案支持。
*   **Session Logon**: WebSocket API 支持 `session.logon`，一次签名认证后，后续请求无需重复签名，大幅降低 CPU 开销和延迟。

## 6. Data Structures & Configuration (数据结构与配置)

### Key Data Structures
*   **Grid Level**:
    ```python
    {
        'id': int,              # 层级索引
        'price': float,         # 挂单价格
        'side': 'BUY'|'SELL',   # 方向
        'order_id': int|None,   # 关联的订单ID
        'capital': float,       # 该层分配的资金量
        'status': 'ACTIVE'|'FILLED'
    }
    ```
*   **Pending Locks**: `Dict[Asset, Amount]`。记录当前正在处理中但尚未成交的订单所占用的资金，用于精确的余额计算。

### Configuration Parameters (`config.py`)
*   **`ATR_PERIOD` (14)**: 计算波动率的时间窗口。值越小,网格对短期波动越敏感,间距调整越频繁。
*   **`CORE_ZONE_PERCENTAGE` (0.5)**: 定义核心区范围。设置为 0.5 表示价格中心上下 50% 的网格范围为核心区。
*   **`PROFIT_MARGIN_MULTIPLIER` (2.0)**: 最小利润系数。要求 (卖价 - 买价) 至少是交易手续费的 2 倍,防止无效交易 (给交易所打工)。
*   **`ENABLE_COMPOUND_INTEREST` (True)**: 启用复利模式。开启后,系统根据账户总价值动态调整每格资金量,实现自动复利。
*   **`CAPITAL_PERCENTAGE_PER_LEVEL` (0.01)**: 每格资金占总资金的百分比。默认 1% 表示账户总价值的 1% 用于单个网格层级,兼顾稳健性与资金利用率。
