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

### 3.0 Calculation Frequencies (计算频率汇总)

| Metric/Operation | Frequency | Trigger |
|:---|:---|:---|
| **ATR & Trend Strength** | Every 5 min | `_grid_maintenance_thread` |
| **Grid Center & Boundaries** | Every 5 min | `_grid_maintenance_thread` |
| **Order Placement (Opposite)** | Real-time (<50ms) | WebSocket `ExecutionReport` |
| **Balance & Price Updates** | Real-time | WebSocket Stream |
| **Unfilled Order Check** | Every 60 sec | `_grid_maintenance_thread` |
| **OCO Integrity Check** | Every 5 min | `_grid_maintenance_thread` |

### 3.1 Market State Detection (市场状态检测)
在 `core/grid_trader.py` 中，`calculate_market_metrics` 方法负责计算关键指标，这些指标随后被用于判断市场状态。所有指标每 **5分钟** (`GRID_RECALCULATION_INTERVAL`) 重新计算一次。

*   **ATR (Average True Range)**:
    *   **Source**: 15-minute K-lines.
    *   **Period**: 14 (default).
    *   **Purpose**: Measures market volatility to dynamically adjust grid range width.

*   **Trend Strength (趋势强度)**:
    *   **Algorithm**: Multi-Timeframe Weighted Average.
    *   **Formula**:
        $$ \text{Trend} = 0.2 \cdot T_{15m} + 0.5 \cdot T_{1h} + 0.3 \cdot T_{4h} $$
    *   **Components**:
        *   15m Trend (20% weight): Short-term momentum.
        *   1h Trend (50% weight): Mid-term direction (Primary driver).
        *   4h Trend (30% weight): Long-term bias.
    *   **Single Timeframe Calculation** (`_calculate_single_timeframe_trend`):
        1. Extract last `lookback` (default 20) close prices.
        2. Calculate period-to-period changes: $\text{change}_i = \frac{P_{i} - P_{i-1}}{P_{i-1}}$.
        3. Compute **Short Trend**: Sum of last 5 changes (70% weight in final).
        4. Compute **Overall Trend**: Time-weighted sum with weight $w_i = 0.5 + \frac{i}{2 \cdot \text{lookback}}$ (newer candles weighted higher, 30% in final).
        5. Combine: $\text{Combined} = (\text{Short} \times 0.7) + (\text{Overall} \times 0.3)$.
        6. Normalize: $\text{Trend} = \text{clamp}(\text{Combined} \times 50, -1.0, 1.0)$.
    *   **Range**: -1.0 (Strong Crash) to +1.0 (Strong Pump).

*   **State Classification**:
    *   **RANGING (震荡)**: Default state. Weak trend strength and moderate volatility.
    *   **BREAKOUT (突破)**: Strong trend strength + High volume.
    *   **PUMP/CRASH**: Extreme trend values detected by moving average divergence and RSI.

### 3.2 Optimal Grid Calculation (最优网格计算)
`_calculate_grid_levels` 方法每 5 分钟执行一次，采用均值回归思想结合实时趋势跟随：

*   **Grid Center Theory** (`calculate_optimal_grid_center`):
    *   **Formula**:
        $$ P_{\text{center}} = P_{\text{historical}} \times 0.20 + P_{\text{current}} \times 0.80 $$
    *   **Logic**: 
        *   **80% Weight on Current Price**: Ensures the grid always follows the market ("Follow the Price"), preventing the bot from trading against a strong trend.
        *   **20% Weight on Historical Price**: Provides a "mean reversion" gravity, keeping the grid anchored to value zones.
    *   **Historical Data**: Uses **400 candles of 4h K-lines** (~66 days).
        *   **Recent** (Last 7 days): 45-55% weight (Dynamic based on oscillation).
        *   **Medium** (7-21 days ago): 30% weight.
        *   **Long** (21-66 days ago): 15-25% weight.

*   **Grid Boundary Generation (边界生成逻辑)**:
    1.  **Trend Factor**:
        $$ F_{\text{trend}} = 0.5 + (\text{TrendStrength} \times 0.3) $$
        Range: 0.2 (Crash) to 0.8 (Pump). Determines asymmetry.
    2.  **Theoretical Boundaries**:
        *   Upper = Center + (Range × $F_{\text{trend}}$)
        *   Lower = Center - (Range × $(1 - F_{\text{trend}})$)
    3.  **Safety Validation**:
        *   Checks boundaries against `current_price ± 1%` safety buffer.
    4.  **Dynamic Snapping (关键保护机制)**:
        *   **Crash Protection**: If `Theoretical Upper < Min Valid Upper` (Grid submerged), the Center is snapped UP to ensure the grid covers the current price.
        *   **Pump Protection**: If `Theoretical Lower > Max Valid Lower` (Grid floating), the Center is snapped DOWN.
        *   **Result**: The grid automatically "slides" to cover the current price even in extreme trends, ensuring valid order placement.

*   **Asymmetric Distribution (非对称分布)**:
    *   **Core Zone**: $P_{\text{center}} \pm \text{CORE\_ZONE\_PERCENTAGE}$. High density, 1.5x capital weight.
    *   **Edge Zone**: Sparse grid, <1.0x capital weight, for catching knives/wicks.

*   **Dynamic Position Sizing (动态仓位管理)**:
    *   **Solvency Check**: Before placing orders, `_calculate_max_buy_levels` and `_calculate_max_sell_levels` verify asset availability.
    *   **One-Sided Grid Support**: Unlike the strict dual-sided requirement in initial versions, the system now supports **One-Sided Grids** (Buy-Only or Sell-Only) during recovery phases or specific market conditions, provided the "Deadlock Recovery" logic approves it.
    *   **Auto-Adjustment**: If funds are insufficient for the configured 9 levels, the bot automatically reduces the number of levels (e.g., to 5 or 7) to maintain valid order sizes (`MIN_NOTIONAL`).


### 3.3 Order Lifecycle Management (订单生命周期)
*   **Placement**:
    *   调用 `_place_grid_order` 时，首先使用 `format_price` 将价格修正为符合 `PRICE_FILTER` 的精度。
    *   检查 `MIN_NOTIONAL` (如 5 USDT)，若订单价值不足，自动跳过或合并，避免 API 报错 `-1013 Filter failure: MIN_NOTIONAL`。
*   **Execution & Regeneration**:
    *   当收到 `FILLED` 事件（`ExecutionReport`）时，`handle_order_update` 被触发，首先将该层级的 `order_id` 清零，避免重复处理。
    *   **Opposite Order Logic**:
        *   若买单成交 ($P_{\text{buy}}$)，系统在相邻网格层级挂出卖单，价格为： $P_{\text{sell}} = P_{\text{buy}} \times (1 + \text{BUY\_SELL\_SPREAD})$，其中 `BUY_SELL_SPREAD` = 0.0025 (0.25%)。
        *   **Profit Margin Check**: 执行前验证 $\text{effective\_profit} > \text{round\_trip\_fee} \times \text{PROFIT\_MARGIN\_MULTIPLIER}$，其中 `effective_profit = expected_profit - round_trip_fee - slippage`，确保净利润高于双边手续费的 2 倍。

### 3.4 Compound Interest Mechanism (复利机制)
如果启用 `ENABLE_COMPOUND_INTEREST = True`，系统每次重算网格时自动调整单格资金量：

*   **计算公式**:
    $$ \text{Capital\_Per\_Level} = (\text{Free\_USDT} + \text{Free\_Base} \times P_{\text{current}}) \times \text{CAPITAL\_PERCENTAGE\_PER\_LEVEL} $$
    
*   **安全限制**:
    *   **最小值**: $\max(\text{MIN\_NOTIONAL} \times 1.1, \text{Dynamic\_Capital})$，确保满足交易所最小订单要求（加10%安全边际）。
    *   **Pending Locks**: 计算中扣除已挂单锁定的资金 (`pending_locks`)，防止重复计算。
    
*   **效果**: 随着账户盈利增长，单格资金量自动增加，实现加速复利。亏损时自动降低仓位，控制风险暴露。

### 3.5 Grid Recalculation Triggers (网格重算触发条件)
系统通过 `check_grid_recalculation` (每5分钟执行) 监控市场变化，满足以下**任一条件**时触发全网格重算：

*   **时间触发 (Time-Based)**:
    *   距离上次重算 ≥ `recalculation_period` (默认 7 天)。
    
*   **价格突破确认 (Out-of-Bounds Recalc)**:
    *   价格超出网格上界或下界 **持续 2 小时** (`out_of_bounds_confirmation_threshold = 7200s`)。
    *   **防止假突破**: 若价格在2小时内回归网格，计时器重置，不触发重算。
    *   实现逻辑：
        ```python
        if currently_out_of_bounds:
            if out_of_bounds_start_time is None:
                out_of_bounds_start_time = current_time  # 开始计时
            elif current_time - out_of_bounds_start_time >= 7200:
                trigger_recalculation()  # 确认突破
        else:
            out_of_bounds_start_time = None  # 价格回归，重置
        ```

*   **波动率剧变 (Volatility-Based)**:
    *   **Major Change**: ATR 变化 > 20% (`atr_change > 0.2`)，触发完全重算。
    *   **Moderate Change**: ATR 变化 10-20%，仅调整网格间距 (`partial_adjustment`)，不撤销现有订单。
    
*   **极端市场状态 (Extreme Market State)**:
    *   检测到 `MarketState.PUMP` 或 `MarketState.CRASH`，立即触发重算以适应趋势。

### 3.6 Deadlock Recovery Strategy (死锁恢复策略)
为解决资产耗尽导致的“0订单”死锁状态，系统引入了基于趋势和资产状态的自动恢复机制。

*   **Trigger**: 当 `_calculate_grid_levels` 返回 `max_buy_levels == 0` (全USDT) 或 `max_sell_levels == 0` (全币) 时触发。
*   **Minimum Capital**: 仅当总账户价值 ≥ 12 USDT 时启用，否则停止运行并报警。

*   **Strategy Matrix (策略矩阵)**:

| 资产状态 | Trend 区间 | 策略 | 操作 | 市价交易 |
|:---|:---|:---|:---|:---|
| **全 USDT** | **< -0.4** | 装死 (Idle) | 不挂单，避险 | ❌ |
| **全 USDT** | **-0.4 ~ 0.5** | 单边买网格 | 仅挂下方买单 | ❌ |
| **全 USDT** | **> 0.5** | 市价重置 | 买入50%币 → 双边网格 | ✅ |
| **全 币** | **< -0.5** | 全部清仓 | 卖出100%币 → 装死 | ✅ |
| **全 币** | **-0.5 ~ -0.2** | 减仓50% | 卖出50%币 → 单边卖网格 | ✅ |
| **全 币** | **-0.2 ~ 0.5** | 单边卖网格 | 仅挂上方卖单 | ❌ |
| **全 币** | **> 0.5** | 单边卖网格 | 仅挂上方卖单 | ❌ |

*   **Cooldown Mechanism (冷却机制)**:
    *   任何涉及**市价交易**的操作（重置、清仓、减仓）后，触发 **1小时冷却期**。
    *   冷却期内禁止再次执行市价交易，防止震荡磨损。
    *   状态持久化至 `.gemini/cooldown_state.json`，防止重启丢失。

## 4. Risk Management System (风险管理系统)

> [!IMPORTANT]
> 对于小资金账户（<100 USDT），OCO功能默认**禁用**（`ENABLE_OCO = False`）。原因：
> 1. 最小订单金额（5-10 USDT）难以满足。
> 2. OCO占用的资金会降低网格密度。
> 3. 在紧密网格范围内，OCO触发概率极低。

### 4.1 OCO Mechanics (OCO机制)
`RiskManager` 利用 Binance OCO 订单实现原子化风控：
*   **Structure**: 一个 OCO 订单包含一个限价单（Limit Maker，用于止盈）和一个止损限价单（Stop-Loss Limit，用于止损）。
*   **Execution**:
    *   **Take Profit**: 当价格上涨至 $P_{\text{TP}}$，Limit Maker 单成交，Stop-Loss 单自动撤销。
    *   **Stop Loss**: 当价格下跌至 $P_{\text{SL\_Trigger}}$，Stop-Loss Limit 单被激活并提交到撮合引擎，Limit Maker 单自动撤销。

### 4.2 Trailing Stop Loss (追踪止损)
`check_price` 函数实现了动态追踪止损，每次价格更新时调用：

*   **Trigger Condition（触发条件）**:
    $$ \frac{P_{\text{new\_SL}} - P_{\text{curr\_SL}}}{P_{\text{curr\_SL}}} > \text{Threshold}_{\text{update}} $$
    仅当新的止损位（基于当前最高价计算）比当前止损位高出一定阈值（如 `min_update_threshold_percent` = 0.5%）时，才触发更新。
    
*   **Calculation Formula（计算公式）**:
    *   **New Stop Loss**: $P_{\text{new\_SL}} = P_{\text{current}} \times (1 - \text{trailing\_stop\_loss\_percent})$
    *   **New Take Profit**: $P_{\text{new\_TP}} = P_{\text{current}} \times (1 + \text{trailing\_take\_profit\_percent})$
    *   **Ratchet Mechanism（棘轮机制）**: 止损位只能上移，不能下降（`if new_stop_loss > self.stop_loss_price`）。
    
*   **Cooling Period（冷却期）**: 
    *   引入 `min_update_interval_seconds` (默认 300s)。
    *   防止在剧烈波动中频繁撤单重发 OCO 导致触发 API Rate Limit。
    ```python
    if time_since_last_update >= min_update_interval_seconds:
        _cancel_oco_orders()
        _place_oco_orders()
    ```

### 4.3 Volatility-Based Parameter Adjustment (基于波动率的参数调整)

系统每小时检查一次市场波动率（通过 `check_price` 中的 Volatility Block），动态调整风控参数：

*   **Trigger（触发条件）**:
    $$ \text{Volatility\_Percent} = \frac{\text{ATR}_{1h}}{P_{\text{current}}} $$
    当 $\text{Volatility\_Percent} > 1.5\%$ 时，判定为高波动环境。

*   **High Volatility Adjustment（高波环境调整）**:
    ```python
    trailing_stop_loss_percent *= 0.8   # 收紧 20% (e.g., 5% → 4%)
    trailing_take_profit_percent *= 0.8 # 收紧 20% (e.g., 3% → 2.4%)
    ```
    *   **目的**: 在高波动时更早止损/止盈，防止流动性枯竭导致滑点过大。

*   **Normal Volatility Reset（正常环境恢复）**:
    当波动率回落至正常水平时，系统自动恢复为配置文件中的默认参数：
    ```python
    base_stop_loss = config.TRAILING_STOP_LOSS_PERCENT / 100
    base_take_profit = config.TRAILING_TAKE_PROFIT_PERCENT / 100
    # Apply capital size adjustment if needed
    trailing_stop_loss_percent = max(0.015, base_stop_loss * capital_adjustment_factor)
    ```

*   **检查频率**: 每 `volatility_check_interval` (默认 3600s = 1小时) 执行一次。

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
*   **`HISTORICAL_KLINE_LIMIT` (400)**: 用于网格中心计算的历史K线数量。必须 ≥ 360 才能支持60天回溯分析，实际设置为 400 以提供安全缓冲。
