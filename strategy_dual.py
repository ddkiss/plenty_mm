import time
from .utils import logger, round_to_step, floor_to
from .rest_client import BackpackREST

class DualMaker:
    def __init__(self, config):
        self.cfg = config
        self.symbol = config.SYMBOL
        self.rest = BackpackREST(config.API_KEY, config.SECRET_KEY)
        
        # 市场基础参数
        self.tick_size = 0.01
        self.min_qty = 0.1
        self.base_precision = 2
        
        # 订单追踪 (核心状态)
        self.active_buy_id = None
        self.active_sell_id = None
        
        # 仓位与资产
        self.held_qty = 0.0
        self.avg_cost = 0.0
        self.equity = 0.0
        
        # 策略状态
        self.mode = "DUAL"  # DUAL(双向刷量) / UNWIND(回本/止损)
        self.last_fill_time = 0 
        self.unwind_start_time = 0

    def init_market_info(self):
        """初始化市场精度信息"""
        try:
            markets = self.rest.get_markets()
            for m in markets:
                if m['symbol'] == self.symbol:
                    filters = m['filters']
                    self.tick_size = float(filters['price']['tickSize'])
                    self.min_qty = float(filters['quantity']['minQuantity'])
                    step_size = str(filters['quantity']['stepSize'])
                    if '.' in step_size:
                        self.base_precision = len(step_size.split('.')[1])
                    else:
                        self.base_precision = 0
                    logger.info(f"Market Init: Tick={self.tick_size}, MinQty={self.min_qty}")
                    return
        except Exception as e:
            logger.error(f"Init Error: {e}")
            exit(1)

    def _sync_state(self):
        """
        同步状态核心：
        1. 更新资产和持仓。
        2. 检查挂单是否存活（以此判断是否成交）。
        """
        try:
            # 1. 获取净值 (用于计算下单量)
            col = self.rest.get_collateral()
            if isinstance(col, dict):
                self.equity = float(col.get("netEquityAvailable", 0))
            
            # 2. 获取持仓 (Perp)
            positions = self.rest.get_positions(self.symbol)
            found = False
            if isinstance(positions, list):
                for p in positions:
                    if p.get('symbol') == self.symbol:
                        self.held_qty = float(p.get('netQuantity', 0))
                        self.avg_cost = float(p.get('entryPrice', 0))
                        found = True
                        break
            if not found:
                self.held_qty = 0.0
                self.avg_cost = 0.0

            # 3. [关键] 反推订单状态
            open_orders = self.rest.get_open_orders(self.symbol)
            if not isinstance(open_orders, list):
                open_orders = [] 
            
            # 当前交易所实际挂着的订单 ID 集合
            active_ids = {str(o['id']) for o in open_orders}
            
            # 检查买单
            if self.active_buy_id:
                if str(self.active_buy_id) not in active_ids:
                    logger.info(f"🔔 买单已消失(成交/被撤) -> ID: {self.active_buy_id}")
                    self.active_buy_id = None 
                    self.last_fill_time = time.time() # 更新成交时间
            
            # 检查卖单
            if self.active_sell_id:
                if str(self.active_sell_id) not in active_ids:
                    logger.info(f"🔔 卖单已消失(成交/被撤) -> ID: {self.active_sell_id}")
                    self.active_sell_id = None
                    self.last_fill_time = time.time()

        except Exception as e:
            logger.error(f"Sync Error: {e}")

    def _place(self, side, price, qty):
        """下单包装函数：异常不中断，返回 ID 或 None"""
        price = round_to_step(price, self.tick_size)
        qty = floor_to(qty, self.base_precision)
        
        if qty < self.min_qty: return None

        try:
            res = self.rest.execute_order({
                "symbol": self.symbol,
                "side": side,
                "orderType": "Limit",
                "price": str(price),
                "quantity": str(qty),
                "postOnly": True # 必须 Maker
            })
            if "id" in res:
                return res["id"]
            else:
                return None
        except Exception:
            return None

    def cancel_all(self):
        """安全撤销所有订单"""
        try:
            self.rest.cancel_open_orders(self.symbol)
        except Exception:
            pass
        finally:
            # 无论 API 是否成功，本地状态先重置，防止死锁
            self.active_buy_id = None
            self.active_sell_id = None

    def run(self):
        self.init_market_info()
        self.cancel_all()
        logger.info(f"🚀 DualMaker V2 启动 | 资金利用率: {self.cfg.GRID_ORDER_PCT*100}%/单 | 买2卖2静默挂单")
        
        while True:
            time.sleep(0.5) # 轮询间隔

            try:
                # 1. 同步状态
                self._sync_state()

                # 2. 仓位风控检查
                # 计算持仓占用 (持仓价值 / 净值)
                exposure = abs(self.held_qty * self.avg_cost)
                ratio = exposure / self.equity if self.equity > 0 else 0
                
                if ratio > self.cfg.MAX_POSITION_PCT:
                    if self.mode == "DUAL":
                        logger.warning(f"⚠️ 仓位过重 ({ratio:.1%}) -> 切换至 UNWIND 回本模式")
                        self.mode = "UNWIND"
                        self.cancel_all() # 撤双向单
                        self.unwind_start_time = time.time()
                elif self.held_qty == 0 and self.mode == "UNWIND":
                    logger.info("🎉 仓位已清空 -> 恢复 DUAL 模式")
                    self.mode = "DUAL"

                # 3. 获取并清洗深度数据
                depth = self.rest.get_depth(self.symbol, limit=5)
                if not depth: continue
                
                bids = sorted(depth.get('bids', []), key=lambda x: float(x[0]), reverse=True)
                asks = sorted(depth.get('asks', []), key=lambda x: float(x[0]))
                
                if len(bids) < 2 or len(asks) < 2: continue
                
                # 取买2卖2
                # [0]是买1, [1]是买2
                bid_1 = float(bids[0][0])
                ask_1 = float(asks[0][0])
                bid_2 = float(bids[1][0])
                ask_2 = float(asks[1][0])

                # 4. 执行对应模式逻辑
                if self.mode == "DUAL":
                    self._logic_dual(bid_2, ask_2)
                else:
                    self._logic_unwind(bid_1, ask_1)

            except Exception as e:
                logger.error(f"Loop Error: {e}")
                time.sleep(1)

    def _logic_dual(self, target_bid, target_ask):
        """
        双向挂单逻辑 (静默版)
        - 只有在一边成交（导致单腿）或两边都无单时才行动。
        - 忽略价格微小偏离。
        """
        
        # 冷却期 (仅在刚成交完后等待)
        if time.time() - self.last_fill_time < self.cfg.REBALANCE_WAIT:
            return

        # ==========================================
        # 1. 状态检查与异常处理
        # ==========================================
        has_buy = (self.active_buy_id is not None)
        has_sell = (self.active_sell_id is not None)
        
        # 【场景 A】: 双边都有挂单 -> 静止不动 (Stay Put)
        # 即使价格偏离了，只要没成交，我们就不动，等待回调吃单。
        if has_buy and has_sell:
            return 

        # 【场景 B】: 只有一边挂单 (Legging / Partial Fill)
        # 可能是：1. 刚成交了一边 2. 上一轮挂单只成功了一边
        # 动作：撤销剩下那个“孤儿单”，为了在下一轮重新以最新价格成对挂单
        if has_buy != has_sell:
            # logger.info(f"检测到单边挂单 (Buy={has_buy}, Sell={has_sell}) -> 撤单重置")
            self.cancel_all()
            return

        # ==========================================
        # 2. 空仓开单 (Atomic Placement)
        # ==========================================
        # 只有当两边都没单子的时候 (active_buy_id 和 active_sell_id 都是 None)
        
        # 计算下单量
        qty = (self.equity * self.cfg.GRID_ORDER_PCT * self.cfg.LEVERAGE) / target_ask
        
        # 价格保护：防止买2 >= 卖2 (异常盘口)
        if target_bid >= target_ask:
            return 

        # 尝试双向发单
        # 注意：这里我们不判断 drift，直接取当前的 target_bid/ask 挂
        new_buy_id = self._place("Bid", target_bid, qty)
        new_sell_id = self._place("Ask", target_ask, qty)
        
        # ==========================================
        # 3. 结果校验 (All-or-Nothing)
        # ==========================================
        
        if new_buy_id and new_sell_id:
            # 完美成功
            self.active_buy_id = new_buy_id
            self.active_sell_id = new_sell_id
            logger.info(f"✅ 挂: 买{target_bid} / 卖{target_ask}")
            
        elif (new_buy_id and not new_sell_id) or (not new_buy_id and new_sell_id):
            # 只有一边成功 (例如一边PostOnly失败) -> 立即撤销成功的那个，保持空仓，下一轮再试
            logger.warning("⚠️ 挂单不完整 -> 立即回滚撤单")
            self.cancel_all()
            
        else:
            # 两边都失败 (可能余额不足或行情剧烈)
            pass

    def _logic_unwind(self, best_bid, best_ask):
        """回本模式：利用买1卖1尽快离场"""
        
        timeout = (time.time() - self.unwind_start_time > self.cfg.BREAKEVEN_TIMEOUT)
        
        # --- 多头平仓 (卖出) ---
        if self.held_qty > self.min_qty:
            if self.active_buy_id: self.cancel_all()
            
            if not self.active_sell_id:
                # 目标：成本价 + 1个tick (0手续费模式)
                # 兜底：不能低于市场价
                target = max(self.avg_cost + self.tick_size, best_ask)
                
                if timeout:
                    target = best_ask # 超时后贴盘口卖
                
                self.active_sell_id = self._place("Ask", target, abs(self.held_qty))

        # --- 空头平仓 (买入) ---
        elif self.held_qty < -self.min_qty:
            if self.active_sell_id: self.cancel_all()
            
            if not self.active_buy_id:
                target = min(self.avg_cost - self.tick_size, best_bid)
                
                if timeout:
                    target = best_bid
                
                self.active_buy_id = self._place("Bid", target, abs(self.held_qty))
