import time
from datetime import datetime, timedelta
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
        
        # [新增] 记录挂单详情用于统计
        self.active_buy_qty = 0.0
        self.active_sell_qty = 0.0
        self.active_buy_price = 0.0
        self.active_sell_price = 0.0
        
        # 仓位与资产
        self.held_qty = 0.0
        self.avg_cost = 0.0
        self.equity = 0.0
        
        # 策略状态
        self.mode = "DUAL"  # DUAL(双向刷量) / UNWIND(回本/止损)
        self.last_fill_time = 0 
        self.unwind_start_time = 0
        
        # [新增] 统计数据
        self.start_time = time.time()
        self.initial_equity = 0.0 # 初始净值
        self.stats = {
            'fill_count': 0,        # 成交次数
            'total_volume': 0.0,    # 总交易量 (Base Asset)
            'total_quote_vol': 0.0, # 总交易额 (Quote Asset)
            'total_fee': 0.0,       # 估算手续费
        }

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
        3. [新增] 触发统计打印
        """
        try:
            # 1. 获取净值 (用于计算下单量)
            col = self.rest.get_collateral()
            if isinstance(col, dict):
                # [修改] 使用 netEquity (账户总权益) 替代 netEquityAvailable
                # netEquity = 可用余额 + 挂单冻结 + 未实现盈亏，数据更稳定
                current_equity = float(col.get("netEquity", 0))
                
                # 记录初始资金
                if self.initial_equity == 0 and current_equity > 0:
                    self.initial_equity = current_equity
                
                # 更新当前净值
                self.equity = current_equity
            
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

            # 3. 反推订单状态与统计成交
            open_orders = self.rest.get_open_orders(self.symbol)
            if not isinstance(open_orders, list):
                open_orders = [] 
            
            active_ids = {str(o['id']) for o in open_orders}
            
            trade_occurred = False
            
            # --- 检查买单 ---
            if self.active_buy_id:
                if str(self.active_buy_id) not in active_ids:
                    logger.info(f"🔔 买单已消失(成交/被撤) -> ID: {self.active_buy_id}")
                    # 更新统计
                    self._update_stats("Buy", self.active_buy_price, self.active_buy_qty)
                    self.active_buy_id = None 
                    self.last_fill_time = time.time()
                    trade_occurred = True
            
            # --- 检查卖单 ---
            if self.active_sell_id:
                if str(self.active_sell_id) not in active_ids:
                    logger.info(f"🔔 卖单已消失(成交/被撤) -> ID: {self.active_sell_id}")
                    # 更新统计
                    self._update_stats("Sell", self.active_sell_price, self.active_sell_qty)
                    self.active_sell_id = None
                    self.last_fill_time = time.time()
                    trade_occurred = True

            # 如果发生了成交，打印一次汇总
            if trade_occurred:
                self._print_stats()

        except Exception as e:
            logger.error(f"Sync Error: {e}")

    def _update_stats(self, side, price, qty):
        """更新内部统计数据"""
        quote_vol = price * qty
        fee = quote_vol * self.cfg.TAKER_FEE_RATE # 仅作估算参考
        
        self.stats['fill_count'] += 1
        self.stats['total_volume'] += qty
        self.stats['total_quote_vol'] += quote_vol
        self.stats['total_fee'] += fee

    def _print_stats(self):
        """[新增] 打印策略运行汇总面板"""
        try:
            now = time.time()
            duration = now - self.start_time
            run_time_str = str(timedelta(seconds=int(duration)))
            
            # 动态计算 PnL
            current_pnl = 0.0
            pnl_percent = 0.0
            if self.initial_equity > 0:
                current_pnl = self.equity - self.initial_equity
                pnl_percent = (current_pnl / self.initial_equity) * 100

            # 估算磨损率 (PnL / 成交额)
            wear_rate = 0.0
            if self.stats['total_quote_vol'] > 0:
                wear_rate = (abs(current_pnl) / self.stats['total_quote_vol']) * 100

            beijing_now = datetime.utcnow() + timedelta(hours=8)
            time_str = beijing_now.strftime('%H:%M:%S')

            msg = (
                f"\n{'='*3} 📊 策略运行汇总 ({time_str}) {'='*3}\n"
                f"运行时间: {run_time_str}\n"
                f"当前模式: {self.symbol} ｜ {self.mode}\n"
                f"初始净值: {self.initial_equity:.2f} USDC\n"
                f"当前净值: {self.equity:.2f} USDC\n"
                f"累计盈亏: {current_pnl:+.4f} USDC ({pnl_percent:+.2f}%)\n"
                f"---\n"
                f"成交次数: {self.stats['fill_count']} 次\n"
                f"总成交量: {self.stats['total_volume']:.4f}\n"
                f"总成交额: {self.stats['total_quote_vol']:.2f} USDC\n"             
                f"资金磨损率: {wear_rate:.4f}%\n"
                f"{'='*5}\n"
            )
            logger.info(msg)
        except Exception as e:
            logger.error(f"Print Stats Error: {e}")

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
            self.active_buy_id = None
            self.active_sell_id = None
            # 撤单后不重置 qty/price，防止 _sync_state 在撤单后无法统计到刚结束的订单
            # (虽然大概率 _sync_state 是下一轮才跑，但保留无害)

    def run(self):
        self.init_market_info()
        self.cancel_all()
        # 更新日志：明确显示当前杠杆和总有效资金估算
        logger.info(f"🚀 DualMaker V3 启动 | 杠杆: {self.cfg.LEVERAGE}x | 有效资金利用率: {self.cfg.GRID_ORDER_PCT*100}%/单")
        
        while True:
            time.sleep(0.5) # 轮询间隔

            try:
                # 1. 同步状态 (内含成交检测与 Stats 打印)
                self._sync_state()

                # 2. 仓位风控检查
                exposure = abs(self.held_qty * self.avg_cost)
                effective_capital = self.equity * self.cfg.LEVERAGE
                ratio = exposure / effective_capital if effective_capital > 0 else 0
                
                if ratio > self.cfg.MAX_POSITION_PCT:
                    if self.mode == "DUAL":
                        logger.warning(f"⚠️ 仓位过重 (占比{ratio:.1%} > {self.cfg.MAX_POSITION_PCT*100}%) -> 切换至 UNWIND 回本模式")
                        self.mode = "UNWIND"
                        self.cancel_all()
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
        """双向挂单逻辑 (静默版)"""
        
        # 冷却期
        if time.time() - self.last_fill_time < self.cfg.REBALANCE_WAIT:
            return

        # 1. 状态检查
        has_buy = (self.active_buy_id is not None)
        has_sell = (self.active_sell_id is not None)
        
        # 场景 A: 双边都有挂单 -> 不动
        if has_buy and has_sell:
            return 

        # 场景 B: 单边挂单 -> 撤单重置
        if has_buy != has_sell:
            self.cancel_all()
            return

        # 2. 空仓开单
        qty = (self.equity * self.cfg.LEVERAGE * self.cfg.GRID_ORDER_PCT) / target_ask
        
        if target_bid >= target_ask: return 

        new_buy_id = self._place("Bid", target_bid, qty)
        new_sell_id = self._place("Ask", target_ask, qty)
        
        # 3. 结果校验
        if new_buy_id and new_sell_id:
            self.active_buy_id = new_buy_id
            self.active_sell_id = new_sell_id
            
            # [新增] 记录挂单详情用于统计
            self.active_buy_price = target_bid
            self.active_sell_price = target_ask
            self.active_buy_qty = qty
            self.active_sell_qty = qty
            
            logger.info(f"✅ 挂: 买{target_bid} / 卖{target_ask} ({qty:.3f})")
            
        elif (new_buy_id and not new_sell_id) or (not new_buy_id and new_sell_id):
            logger.warning("⚠️ 挂单不完整 -> 立即回滚撤单")
            self.cancel_all()
            
        else:
            pass

    def _logic_unwind(self, best_bid, best_ask):
        """回本模式 (修正版：超时自动调整挂单紧贴盘口，纯 Maker)"""
        # 计算是否超时
        timeout = (time.time() - self.unwind_start_time > self.cfg.BREAKEVEN_TIMEOUT)
        
        # ==========================================
        # 场景 A: 多头平仓 (手里有币，要卖)
        # ==========================================
        if self.held_qty > self.min_qty:
            # 1. 必须先撤销反向单 (买单)
            if self.active_buy_id: self.cancel_all()
            
            # 2. [新增] 超时活跃检查
            # 如果处于超时状态，且当前有挂单，检查挂单价格是否还是“卖一价”
            if self.active_sell_id and timeout:
                # 如果挂单价与当前卖一价偏差超过半个 tick，说明价格跑了
                if abs(self.active_sell_price - best_ask) > self.tick_size / 2:
                    logger.info(f"⏰ 回本超时 -> 价格偏离，撤单重挂紧贴卖一: {best_ask}")
                    self.cancel_all()
                    return # 撤单后直接返回，等下一轮循环重新挂

            # 3. 挂单逻辑
            if not self.active_sell_id:
                # 正常模式：保本出 (成本价+1跳) 和 卖一价，取较大值 (不想亏本)
                # 超时模式：不看成本了，直接挂 卖一价 (best_ask)，只求成交
                target = best_ask if timeout else max(self.avg_cost + self.tick_size, best_ask)
                
                qty = abs(self.held_qty)
                # 依然保持默认的 Maker 属性 (postOnly=True)
                self.active_sell_id = self._place("Ask", target, qty)
                
                if self.active_sell_id:
                    self.active_sell_price = target
                    self.active_sell_qty = qty

        # ==========================================
        # 场景 B: 空头平仓 (手里欠币，要买)
        # ==========================================
        elif self.held_qty < -self.min_qty:
            if self.active_sell_id: self.cancel_all()
            
            # 2. [新增] 超时活跃检查
            if self.active_buy_id and timeout:
                # 如果挂单价与当前买一价不同，撤单重追
                if abs(self.active_buy_price - best_bid) > self.tick_size / 2:
                    logger.info(f"⏰ 回本超时 -> 价格偏离，撤单重挂紧贴买一: {best_bid}")
                    self.cancel_all()
                    return

            # 3. 挂单逻辑
            if not self.active_buy_id:
                # 正常模式：保本回 (成本价-1跳) 和 买一价，取较小值
                # 超时模式：不看成本了，直接挂 买一价 (best_bid)
                target = best_bid if timeout else min(self.avg_cost - self.tick_size, best_bid)
                
                qty = abs(self.held_qty)
                self.active_buy_id = self._place("Bid", target, qty)
                
                if self.active_buy_id:
                    self.active_buy_price = target
                    self.active_buy_qty = qty
