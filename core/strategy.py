import time
import threading
from .utils import logger, round_to_step, floor_to
from .rest_client import BackpackREST
from .ws_client import BackpackWS

class TickScalper:
    def __init__(self, config):
        self.cfg = config
        self.symbol = config.SYMBOL
        
        # Clients
        self.rest = BackpackREST(config.API_KEY, config.SECRET_KEY)
        self.ws = BackpackWS(config.API_KEY, config.SECRET_KEY, self.symbol, self.on_order_update)
        
        # State
        self.state = "IDLE"  # IDLE, BUYING, SELLING
        self.active_order_id = None
        self.active_order_price = 0.0
        self.held_qty = 0.0
        self.avg_cost = 0.0
        self.hold_start_time = 0
        
        # Market Info
        self.tick_size = 0.01
        self.step_size = 0.1
        self.min_qty = 0.1
        self.base_precision = 2
        self.quote_precision = 2
        
        # Control
        self.last_cool_down = 0
        self.running = False

    def init_market_info(self):
        markets = self.rest.get_markets()
        for m in markets:
            if m['symbol'] == self.symbol:
                filters = m['filters']
                self.tick_size = float(filters['price']['tickSize'])
                self.step_size = float(filters['quantity']['stepSize'])
                self.min_qty = float(filters['quantity']['minQuantity'])
                self.base_precision = len(str(self.step_size).split('.')[1]) if '.' in str(self.step_size) else 0
                self.quote_precision = len(str(self.tick_size).split('.')[1]) if '.' in str(self.tick_size) else 0
                logger.info(f"Market Info Loaded: Tick={self.tick_size}, Step={self.step_size}, MinQty={self.min_qty}")
                return
        logger.error("Symbol not found!")
        exit(1)

    def on_order_update(self, data):
        """ WebSocket 回调: 处理成交 """
        event = data.get('e')
        if event == 'orderFill':
            side = data.get('S')
            price = float(data.get('L'))
            qty = float(data.get('l'))
            logger.info(f"⚡ 成交: {side} {qty} @ {price}")
            
            if side == "Bid":
                self.state = "SELLING"
                self.held_qty = qty
                self.avg_cost = price
                self.hold_start_time = time.time()
                self.active_order_id = None # 买单成交，当前无挂单
            elif side == "Ask":
                profit = (price - self.avg_cost) * qty
                logger.info(f"💰 止盈/损结束 (PnL: {profit:.4f})")
                if profit < 0:
                    self.last_cool_down = time.time()
                    logger.warning(f"🛑 亏损冷却 {self.cfg.COOL_DOWN}s")
                
                self.state = "IDLE"
                self.held_qty = 0
                self.active_order_id = None

    def cancel_all(self):
        self.rest.cancel_open_orders(self.symbol)
        self.active_order_id = None

    def run(self):
        self.init_market_info()
        self.ws.connect()
        self.running = True
        
        # 清理旧单
        self.cancel_all()
        
        logger.info(f"策略启动: {self.symbol} | 余额比例: {self.cfg.BALANCE_PCT} | 止损: {self.cfg.STOP_LOSS_PCT*100}%")

        while self.running:
            time.sleep(0.5) # 控制循环频率
            
            # 1. 冷却检查
            if time.time() - self.last_cool_down < self.cfg.COOL_DOWN:
                continue

            # 2. 等待行情
            bid = self.ws.best_bid
            ask = self.ws.best_ask
            if bid == 0 or ask == 0:
                continue

            # 3. 策略状态机
            if self.state == "IDLE":
                self._logic_buy(bid, ask)
            elif self.state == "BUYING":
                self._logic_chase_buy(bid)
            elif self.state == "SELLING":
                self._logic_sell(bid, ask)

    def _place_order(self, side, price, qty, post_only=True):
        price = round_to_step(price, self.tick_size)
        qty = floor_to(qty, self.base_precision)
        
        if qty < self.min_qty:
            logger.warning(f"数量太小: {qty} < {self.min_qty}")
            return None

        order_data = {
            "symbol": self.symbol,
            "side": side,
            "orderType": "Limit",
            "price": str(price),
            "quantity": str(qty),
            "postOnly": post_only
        }
        res = self.rest.execute_order(order_data)
        if "id" in res:
            self.active_order_id = res["id"]
            self.active_order_price = price
            logger.info(f"挂单成功 [{side}]: {qty} @ {price}")
            return res["id"]
        else:
            logger.error(f"下单失败: {res}")
            return None

    def _logic_buy(self, best_bid, best_ask):
        # 简单判断：如果当前没有挂单，则挂单
        if self.active_order_id:
            return

        # 获取余额
        bal_res = self.rest.get_balance()
        if "USDC" not in bal_res: return
        usdc_available = float(bal_res["USDC"]["available"])
        
        # 计算下单量
        amount_usdc = usdc_available * self.cfg.BALANCE_PCT * self.cfg.LEVERAGE
        qty = amount_usdc / best_bid
        
        # 挂在买一价 (Maker)
        self._place_order("Bid", best_bid, qty, post_only=True)
        self.state = "BUYING"

    def _logic_chase_buy(self, best_bid):
        # 追单逻辑：如果市场买一价超过我的挂单价一定比例，撤单重挂
        if not self.active_order_id: 
            self.state = "IDLE" # 订单可能被手动取消或失效
            return
            
        if best_bid > self.active_order_price * (1 + 0.0001): # 0.01% 阈值
            logger.info(f"🚀 追涨: 市场 {best_bid} > 挂单 {self.active_order_price}")
            self.cancel_all()
            self.state = "IDLE" # 下一轮循环重新挂单

    def _logic_sell(self, best_bid, best_ask):
        # 持仓卖出逻辑 (分级止损)
        
        # 还没有挂卖单，需要决定价格
        if not self.active_order_id:
            duration = time.time() - self.hold_start_time
            pnl_pct = (best_bid - self.avg_cost) / self.avg_cost
            
            target_price = best_ask # 默认挂卖一
            post_only = True
            
            # 场景A: 价格止损 (Taker)
            if pnl_pct < -self.cfg.STOP_LOSS_PCT:
                logger.warning(f"🚨 触发价格止损 ({pnl_pct*100:.2f}%) -> Taker")
                target_price = best_bid
                post_only = False
            
            # 场景B: 超时止损 (Maker)
            elif duration > 135: # 135秒超时
                logger.warning(f"⏰ 触发超时止损 ({duration:.0f}s) -> Maker")
                target_price = best_ask
                
            self._place_order("Ask", target_price, self.held_qty, post_only=post_only)
        
        else:
            # 已有卖单，检查是否需要调整
            # 如果是超时止损模式，随着 Ask 移动
            if self.active_order_price != best_ask and (time.time() - self.hold_start_time > 135):
                 self.cancel_all() # 撤单，下一轮重挂