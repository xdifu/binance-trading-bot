import logging
import time
from threading import Thread
from datetime import datetime, timedelta
import sys
import os

# 添加项目根目录到系统路径
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 导入自定义模块
from binance_api.client import BinanceClient
from binance_api.websocket_manager import WebsocketManager
from core.grid_trader import GridTrader
from core.risk_manager import RiskManager
# 修改导入语句，使用重命名后的模块
from tg_bot.bot import TelegramBot
import config

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('grid_bot.log')
    ]
)

logger = logging.getLogger(__name__)

class GridTradingBot:
    def __init__(self):
        self.binance_client = BinanceClient()
        self.telegram_bot = None
        self.grid_trader = None
        self.risk_manager = None
        self.ws_manager = None
        self.is_running = False
        
        # 初始化子模块
        self._init_modules()
    
    def _init_modules(self):
        """初始化各个模块"""
        # 初始化Telegram机器人
        try:
            if config.TELEGRAM_TOKEN and config.ALLOWED_TELEGRAM_USERS:
                self.telegram_bot = TelegramBot(
                    token=config.TELEGRAM_TOKEN,
                    allowed_users=config.ALLOWED_TELEGRAM_USERS
                )
                logger.info("Telegram机器人初始化成功")
        except Exception as e:
            logger.error(f"Telegram机器人初始化失败: {e}")
        
        # 初始化网格交易策略
        self.grid_trader = GridTrader(
            binance_client=self.binance_client,
            telegram_bot=self.telegram_bot
        )
        logger.info("网格交易策略初始化成功")
        
        # 初始化风险管理
        self.risk_manager = RiskManager(
            binance_client=self.binance_client,
            telegram_bot=self.telegram_bot
        )
        logger.info("风险管理模块初始化成功")
        
        # 更新Telegram机器人的引用
        if self.telegram_bot:
            self.telegram_bot.grid_trader = self.grid_trader
            self.telegram_bot.risk_manager = self.risk_manager
    
    def _handle_websocket_message(self, message):
        """处理WebSocket消息"""
        try:
            # 处理不同类型的消息
            if 'e' in message:
                if message['e'] == 'kline':
                    # K线更新
                    symbol = message['s']
                    price = float(message['k']['c'])
                    
                    if symbol == config.SYMBOL:
                        # 检查风险管理，只更新追踪逻辑，不再直接执行止盈止损
                        if self.risk_manager and self.risk_manager.is_active:
                            self.risk_manager.check_price(price)
                
                elif message['e'] == 'executionReport':
                    # 订单执行报告
                    self.grid_trader.handle_order_update(message)
                    
                elif message['e'] == 'listStatus':
                    # OCO订单状态更新
                    self._handle_oco_update(message)
        except Exception as e:
            logger.error(f"处理WebSocket消息失败: {e}")
    
    def _handle_oco_update(self, message):
        """处理OCO订单更新"""
        try:
            if message['L'] == 'EXECUTING' or message['L'] == 'ALL_DONE':
                symbol = message['s']
                order_list_id = message['i']
                
                # 检查是否是我们的风险管理OCO订单
                if (self.risk_manager and self.risk_manager.is_active and 
                    self.risk_manager.oco_order_id == order_list_id):
                    
                    if message['L'] == 'ALL_DONE':
                        logger.info(f"风险管理OCO订单已执行: {order_list_id}")
                        
                        # 检查哪一边被执行
                        if message['l'] == 'STOP_LOSS_LIMIT':
                            logger.info("止损订单已执行")
                            if self.telegram_bot:
                                self.telegram_bot.send_message("止损订单已执行，网格交易已停止")
                            # 停止网格交易
                            self.grid_trader.stop()
                        elif message['l'] == 'LIMIT_MAKER':
                            logger.info("止盈订单已执行")
                            if self.telegram_bot:
                                self.telegram_bot.send_message("止盈订单已执行，网格交易已停止")
                            # 停止网格交易
                            self.grid_trader.stop()
                        
                        # 重置OCO订单ID
                        self.risk_manager.oco_order_id = None
        except Exception as e:
            logger.error(f"处理OCO订单更新失败: {e}")

    def _websocket_error_handler(self, error):
        """WebSocket错误处理"""
        logger.error(f"WebSocket错误: {error}")
        
        # 尝试重新连接
        if self.is_running:
            logger.info("尝试重新连接WebSocket...")
            time.sleep(5)
            self._setup_websocket()
    
    def _setup_websocket(self):
        """设置WebSocket连接"""
        try:
            # 停止现有连接
            if self.ws_manager:
                self.ws_manager.stop()
            
            # 创建新连接
            self.ws_manager = WebsocketManager(
                on_message_callback=self._handle_websocket_message,
                on_error_callback=self._websocket_error_handler
            )
            
            # 启动市场数据流
            self.ws_manager.start_market_stream(config.SYMBOL)
            
            # 启动用户数据流
            try:
                # 创建listenKey
                response = self.binance_client.client.new_listen_key()
                listen_key = response['listenKey']
                
                # 启动用户数据流
                self.ws_manager.start_user_stream(listen_key)
                
                # 定期延长listenKey有效期
                def keep_alive_listen_key():
                    while self.is_running:
                        try:
                            self.binance_client.client.renew_listen_key(listen_key)
                            logger.debug(f"已延长listenKey有效期: {listen_key}")
                        except Exception as e:
                            logger.error(f"延长listenKey失败: {e}")
                        
                        # 每30分钟更新一次
                        time.sleep(30 * 60)
                
                # 启动保活线程
                Thread(target=keep_alive_listen_key, daemon=True).start()
                
            except Exception as e:
                logger.error(f"启动用户数据流失败: {e}")
            
        except Exception as e:
            logger.error(f"设置WebSocket失败: {e}")
    
    def _grid_maintenance_thread(self):
        """网格维护线程"""
        last_check = datetime.now()
        
        while self.is_running:
            try:
                now = datetime.now()
                
                # 每天检查一次网格重新计算
                if (now - last_check).total_seconds() > 24 * 60 * 60:
                    self.grid_trader.check_grid_recalculation()
                    last_check = now
                
                # 短暂休眠
                time.sleep(60)
            except Exception as e:
                logger.error(f"网格维护失败: {e}")
                time.sleep(60)
    
    def start(self):
        """启动机器人"""
        if self.is_running:
            logger.info("机器人已经在运行")
            return
        
        self.is_running = True
        logger.info("启动Grid Trading Bot")
        
        # 启动Telegram机器人
        if self.telegram_bot:
            self.telegram_bot.start()
            self.telegram_bot.send_message("网格交易机器人已启动，使用 /help 查看命令")
        
        # 设置WebSocket连接
        self._setup_websocket()
        
        # 启动网格维护线程
        Thread(target=self._grid_maintenance_thread, daemon=True).start()
        
        # 主循环
        try:
            while self.is_running:
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("接收到中断信号，正在停止...")
            self.stop()
    
    def stop(self):
        """停止机器人"""
        if not self.is_running:
            return
        
        self.is_running = False
        logger.info("正在停止Grid Trading Bot")
        
        # 停止网格交易
        if self.grid_trader and self.grid_trader.is_running:
            self.grid_trader.stop()
        
        # 停止风险管理
        if self.risk_manager and self.risk_manager.is_active:
            self.risk_manager.deactivate()
        
        # 停止WebSocket连接
        if self.ws_manager:
            self.ws_manager.stop()
        
        # 停止Telegram机器人
        if self.telegram_bot:
            self.telegram_bot.send_message("网格交易机器人已停止")
            self.telegram_bot.stop()
        
        logger.info("Grid Trading Bot已停止")

if __name__ == "__main__":
    bot = GridTradingBot()
    bot.start()