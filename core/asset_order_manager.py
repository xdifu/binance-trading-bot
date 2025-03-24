import threading
import time
import logging
from typing import Dict, Tuple, Optional, List, Any
import config
from utils.format_utils import format_price, format_quantity

class AssetOrderManager:
    """统一管理资产和订单的类，协调资金锁定和订单状态"""

    def __init__(self, binance_client):
        """初始化资产订单管理器
        
        Args:
            binance_client: Binance API客户端实例
        """
        self.logger = logging.getLogger(__name__)
        self.binance_client = binance_client
        self.symbol = config.SYMBOL
        self.base_asset = self.symbol.replace('USDT', '')
        self.quote_asset = 'USDT'
        
        # 资产和余额相关
        self.balances = {}  # 存储账户余额
        self.locked_funds = {self.base_asset: 0.0, self.quote_asset: 0.0}  # 锁定的资金
        
        # 价格精度相关
        self._update_precision()
        
        # 订单管理相关
        self.orders = {}  # 常规订单映射: order_id -> order_details
        self.oco_orders = {}  # OCO订单映射: order_list_id -> order_details
        
        # 并发控制
        self._lock = threading.RLock()
        self._cache_expiry = 0
        self._balance_cache_seconds = 2  # 余额缓存时间（秒）
        
        # 初始化余额
        self.refresh_balances(force=True)
        
    def _update_precision(self):
        """更新当前交易对的精度信息"""
        try:
            symbol_info = self.binance_client.get_symbol_info(self.symbol)
            
            # 提取价格精度
            price_filter = next((f for f in symbol_info['filters'] if f['filterType'] == 'PRICE_FILTER'), None)
            if price_filter and 'tickSize' in price_filter:
                tick_size = float(price_filter['tickSize'])
                tick_size_str = f"{tick_size:.10f}".rstrip('0').rstrip('.')
                self.price_precision = len(tick_size_str.split('.')[-1]) if '.' in tick_size_str else 0
                self.tick_size = tick_size
            else:
                self.price_precision = 4
                self.tick_size = 0.0001
                
            # 提取数量精度
            lot_filter = next((f for f in symbol_info['filters'] if f['filterType'] == 'LOT_SIZE'), None)
            if lot_filter and 'stepSize' in lot_filter:
                step_size = float(lot_filter['stepSize'])
                step_size_str = f"{step_size:.10f}".rstrip('0').rstrip('.')
                self.quantity_precision = len(step_size_str.split('.')[-1]) if '.' in step_size_str else 0
                self.step_size = step_size
            else:
                self.quantity_precision = 1
                self.step_size = 0.1
                
            # 提取最小名义价值
            notional_filter = next((f for f in symbol_info['filters'] if f['filterType'] == 'MIN_NOTIONAL'), None)
            self.min_notional = float(notional_filter['minNotional']) if notional_filter else 10.0
                
            # 记录价格过滤器限制
            self.percent_price_filter = next(
                (f for f in symbol_info['filters'] if f['filterType'] == 'PERCENT_PRICE_BY_SIDE'), 
                None
            )
            
            self.logger.info(f"Trading pair {self.symbol} price precision: {self.price_precision}, quantity precision: {self.quantity_precision}")
            self.logger.info(f"Trading pair {self.symbol} tick size: {self.tick_size}, step size: {self.step_size}")
            
        except Exception as e:
            self.logger.error(f"Error updating precision: {e}")
            self.price_precision = 4
            self.quantity_precision = 1
            self.tick_size = 0.0001
            self.step_size = 0.1
            self.min_notional = 10.0
            self.percent_price_filter = None
    
    def update_symbol(self, new_symbol: str) -> Tuple[str, str]:
        """更新交易对，并重置相关状态
        
        Args:
            new_symbol: 新的交易对符号，如 'BTCUSDT'
            
        Returns:
            Tuple[str, str]: 旧交易对和新交易对
        """
        with self._lock:
            old_symbol = self.symbol
            self.symbol = new_symbol
            old_base_asset = self.base_asset
            
            # 更新基础和报价资产
            self.base_asset = new_symbol.replace('USDT', '')
            
            # 清空相关订单
            self.orders = {}
            self.oco_orders = {}
            
            # 转移锁定资金
            new_locked_funds = {self.base_asset: 0.0, self.quote_asset: self.locked_funds.get(self.quote_asset, 0.0)}
            self.locked_funds = new_locked_funds
            
            # 更新价格精度信息
            self._update_precision()
            
            # 刷新余额
            self.refresh_balances(force=True)
            
            self.logger.info(f"Asset manager updated symbol from {old_symbol} to {new_symbol}")
            self.logger.info(f"Base asset updated from {old_base_asset} to {self.base_asset}")
            
            return old_symbol, new_symbol
            
    def refresh_balances(self, force: bool = False) -> Dict[str, float]:
        """刷新账户余额，支持缓存
        
        Args:
            force: 是否强制刷新，忽略缓存
            
        Returns:
            Dict[str, float]: 账户余额
        """
        now = time.time()
        
        # 如果非强制且缓存仍然有效，返回缓存的余额
        if not force and now < self._cache_expiry:
            return self.balances
            
        try:
            account_info = self.binance_client.get_account()
            balances = {}
            
            # 处理API返回的余额
            for balance in account_info['balances']:
                asset = balance['asset']
                free = float(balance['free'])
                locked = float(balance['locked'])
                balances[asset] = {'free': free, 'locked': locked, 'total': free + locked}
                
            # 更新缓存
            self.balances = balances
            self._cache_expiry = now + self._balance_cache_seconds
            
            return balances
            
        except Exception as e:
            self.logger.error(f"Failed to refresh balances: {e}")
            # 缓存失效，但返回旧数据
            self._cache_expiry = 0
            return self.balances
    
    def get_available_balance(self, asset: str) -> float:
        """获取资产的可用余额，考虑已锁定金额
        
        Args:
            asset: 资产名称
            
        Returns:
            float: 可用余额数量
        """
        self.refresh_balances()
        
        asset_balance = self.balances.get(asset, {'free': 0.0})
        locked_amount = self.locked_funds.get(asset, 0.0)
        
        # 可用余额 = 交易所显示的可用余额 - 系统内部锁定的资金
        available = asset_balance.get('free', 0.0) - locked_amount
        return max(0.0, available)  # 确保不返回负数
    
    def get_total_balance(self, asset: str) -> float:
        """获取资产的总余额（可用+交易所锁定）
        
        Args:
            asset: 资产名称
            
        Returns:
            float: 总余额数量
        """
        self.refresh_balances()
        asset_balance = self.balances.get(asset, {'total': 0.0})
        return asset_balance.get('total', 0.0)
    
    def lock_funds(self, asset: str, amount: float, order_id: str = None) -> bool:
        """锁定资金用于下单
        
        Args:
            asset: 资产名称
            amount: 锁定数量
            order_id: 关联的订单ID（可选）
            
        Returns:
            bool: 是否成功锁定
        """
        with self._lock:
            available = self.get_available_balance(asset)
            
            if available < amount:
                self.logger.warning(
                    f"Insufficient {asset} balance for locking: "
                    f"Required: {amount}, Available: {available} "
                    f"(Total: {self.get_total_balance(asset)}, "
                    f"Already locked: {self.locked_funds.get(asset, 0)})"
                )
                return False
                
            # 增加锁定金额
            current_lock = self.locked_funds.get(asset, 0.0)
            self.locked_funds[asset] = current_lock + amount
            
            if order_id:
                self.logger.debug(f"Locked {amount} {asset} for order {order_id}")
            else:
                self.logger.debug(f"Locked {amount} {asset}")
                
            return True
    
    def unlock_funds(self, asset: str, amount: float, order_id: str = None) -> None:
        """解锁之前锁定的资金
        
        Args:
            asset: 资产名称
            amount: 解锁数量
            order_id: 关联的订单ID（可选）
        """
        with self._lock:
            current_lock = self.locked_funds.get(asset, 0.0)
            new_lock = max(0.0, current_lock - amount)  # 确保不会低于0
            
            self.locked_funds[asset] = new_lock
            
            if order_id:
                self.logger.debug(f"Unlocked {amount} {asset} from order {order_id}")
            else:
                self.logger.debug(f"Unlocked {amount} {asset}")
    
    def reset_locks(self) -> None:
        """重置所有资金锁定"""
        with self._lock:
            old_locks = dict(self.locked_funds)
            self.locked_funds = {self.base_asset: 0.0, self.quote_asset: 0.0}
            self.logger.info(f"Reset all fund locks. Previous locks: {old_locks}")
    
    def track_order(self, order_id: str, order_data: Dict) -> None:
        """跟踪常规订单
        
        Args:
            order_id: 订单ID
            order_data: 订单详情
        """
        with self._lock:
            self.orders[order_id] = order_data
    
    def track_oco_order(self, order_list_id: str, order_data: Dict) -> None:
        """跟踪OCO订单
        
        Args:
            order_list_id: OCO订单ID
            order_data: 订单详情
        """
        with self._lock:
            self.oco_orders[order_list_id] = order_data
    
    def remove_order(self, order_id: str) -> Optional[Dict]:
        """移除订单跟踪
        
        Args:
            order_id: 订单ID
            
        Returns:
            Optional[Dict]: 被移除的订单详情，如不存在则返回None
        """
        with self._lock:
            return self.orders.pop(order_id, None)
    
    def remove_oco_order(self, order_list_id: str) -> Optional[Dict]:
        """移除OCO订单跟踪
        
        Args:
            order_list_id: OCO订单ID
            
        Returns:
            Optional[Dict]: 被移除的订单详情，如不存在则返回None
        """
        with self._lock:
            return self.oco_orders.pop(order_list_id, None)
    
    def get_order_price_range(self) -> Tuple[float, float]:
        """获取符合交易所规则的订单价格范围
        
        Returns:
            Tuple[float, float]: (最小价格偏差, 最大价格偏差)比例
        """
        # 默认安全值
        default_min = 0.02  # 2%
        default_max = 0.05  # 5%
        
        if not self.percent_price_filter:
            return default_min, default_max
            
        try:
            # 提取价格过滤器的限制
            bid_mult_down = float(self.percent_price_filter.get('bidMultiplierDown', 0.8))
            bid_mult_up = float(self.percent_price_filter.get('bidMultiplierUp', 1.2))
            ask_mult_down = float(self.percent_price_filter.get('askMultiplierDown', 0.8))
            ask_mult_up = float(self.percent_price_filter.get('askMultiplierUp', 1.2))
            
            # 计算最大安全偏差（考虑买入和卖出订单）
            min_deviation = min(
                1.0 - bid_mult_down,  # 买单下限偏差
                bid_mult_up - 1.0,    # 买单上限偏差
                1.0 - ask_mult_down,  # 卖单下限偏差
                ask_mult_up - 1.0     # 卖单上限偏差
            )
            
            # 使用80%的最大值作为安全值
            safe_deviation = min_deviation * 0.8
            
            # 至少0.5%的波动，最多20%的波动
            return max(0.005, safe_deviation * 0.5), min(0.2, safe_deviation)
            
        except Exception as e:
            self.logger.warning(f"Error calculating price range: {e}, using defaults")
            return default_min, default_max
    
    def calculate_order_quantity(self, side: str, available_funds: float, price: float) -> Tuple[float, str]:
        """计算符合交易所规则的订单数量
        
        Args:
            side: 订单方向 'BUY' 或 'SELL'
            available_funds: 可用资金数量
            price: 价格
            
        Returns:
            Tuple[float, str]: (数量, 格式化后的数量)
        """
        try:
            if side == 'BUY':
                # 买入: 可用USDT / 价格
                raw_quantity = available_funds / price
            else:
                # 卖出: 可用目标资产
                raw_quantity = available_funds
                
            # 确保符合最小名义价值
            notional = raw_quantity * price
            if notional < self.min_notional:
                if side == 'BUY' and available_funds >= self.min_notional:
                    # 有足够USDT，调整到最小名义价值
                    raw_quantity = self.min_notional / price
                else:
                    # 资金不足最小名义价值
                    return 0.0, "0"
                    
            # 按步长调整数量
            steps = int(raw_quantity / self.step_size)
            adjusted_quantity = steps * self.step_size
                
            # 格式化为适当精度的字符串
            formatted_quantity = format_quantity(adjusted_quantity, self.quantity_precision)
            
            return float(formatted_quantity), formatted_quantity
            
        except Exception as e:
            self.logger.error(f"Error calculating order quantity: {e}")
            return 0.0, "0"