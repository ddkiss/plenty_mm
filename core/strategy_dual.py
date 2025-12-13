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
        
        # 订单追踪
        self.active_buy_id = None
        self.active_sell_id = None
        
        # 挂单详情
        self.active_buy_qty = 0.0
        self.active_sell_qty = 0.0
        self.active_buy_price = 0.0
        self.active_sell_price = 0.0
        
        # 仓位与资产
        self.held_qty = 0.0
        self.avg_cost = 0.0
        self.equity = 0.0
        
        # 策略状态
        self.mode = "DUAL"  # DUAL / UNWIND
        self.last_fill_time = 0 
        self.unwind_start_time = 0
        
        # 统计数据
        self.start_time = time.time()
        self.initial_equity = 0.0 
        self.stats = {
            'fill_count': 0,
            'total_volume': 0.0,
            'total_quote_vol': 0.0,
            'total_fee': 0.0,
        }
        
        # 标记是否为合约
        self.is_perp = "PERP" in self.symbol.upper()

    def init_market_info(self):
        """初始化市场精度信息"""
        try:
            markets = self.rest.get_markets()
            found = False
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
                    logger.info(f"Market Init: Tick={self.tick_size}, MinQty={self.min_qty}, IsPerp={self.is_perp}")
                    found = True
                    return
            if not found:
                logger.error(f"Symbol {self.symbol} not found in market info!")
                exit(1)
        except Exception as e:
            logger.error(f"Init Error: {e}")
            exit(1)

    def _sync_state(self):
        """
        同步状态核心 (Unified Margin 模式适配):
        1. 使用 get_collateral 获取统一的 Equity 和 现货持仓。
        2. 如果是合约，额外通过 get_positions 获取精确的 entryPrice。
        """
        try:
            # --- 1. 获取联合保证金账户数据 ---
            # Backpack 的 Spot 和 Perp 共享 collateral
            col = self.rest.get_collateral()
            
            if not isinstance(col, dict):
                logger.error(f"获取 Collateral 失败: {col}")
                return

            # 获取净值 (Net Equity) - 这是所有下单金额的基础
            self.equity = float(col.get("netEquity", 0))
            if self.initial_equity == 0 and self.equity > 0:
                self.initial_equity = self.equity

            # --- 2. 获取持仓数量 (Held Qty) ---
            if self.is_perp:
                # === 合约模式 ===
                # 合约持仓推荐使用 get_positions，因为包含 entryPrice 和 leverage 信息
                positions = self.rest.get_positions(self.symbol)
                pos_found = False
                if isinstance(positions, list):
                    for p in positions:
                        if p.get('symbol') == self.symbol:
                            self.held_qty = float(p.get('netQuantity', 0))
                            self.avg_cost = float(p.get('entryPrice', 0))
                            pos_found = True
                            break
                if not pos_found:
                    self.held_qty = 0.0
                    self.avg_cost = 0.0
            else:
                # === 现货模式 (Unified) ===
                # 现货持仓在 collateral 的 'assets' 列表中
                assets = col.get("assets", [])
                base_asset = self.symbol.split('_')[0] # 例如 SOL_USDC -> SOL
                
                found_asset = False
                for asset in assets:
                    if asset.get("symbol") == base_asset:
                        # 现货总持仓 = 可用 + 冻结
                        avail = float(asset.get("available", 0))
                        locked = float(asset.get("locked", 0))
                        # 借贷情况处理：如果有借款，borrow 字段可能会有值，这里取净值
                        borrow = float(asset.get("borrow", 0))
                        
                        self.held_qty = avail + locked - borrow
                        found_asset = True
                        break
                
                if not found_asset:
                    self.held_qty = 0.0
                
                # 现货成本估算：如果没有 avg_cost (API不提供)，则暂时用当前盘口价或上次成交价估算
                if self.avg_cost == 0 and self.active_buy_price > 0:
                    self.avg_cost = self.active_buy_price

            # --- 3. 反推订单状态 ---
            open_orders = self.rest.get_open_orders(self.symbol)
            if not isinstance(open_orders, list):
                open_orders = [] 
            
            active_ids = {str(o['id']) for o in open_orders}
            trade_occurred = False
            
            # 检查买单
            if self.active_buy_id:
                if str(self.active_buy_id) not in active_ids:
                    logger.info(f"🔔 买单已消失(成交/被撤) -> ID: {self.active_buy_id}")
                    self._update_stats("Buy", self.active_buy_price, self.active_buy_qty)
                    self.active_buy_id = None 
                    self.last_fill_time = time.time()
                    trade_occurred = True
            
            # 检查卖单
            if self.active_sell_id:
                if str(self.active_sell_id) not in active_ids:
                    logger.info(f"🔔 卖单已消失(成交/被撤) -> ID: {self.active_sell_id}")
                    self._update_stats("Sell", self.active_sell_price, self.active_sell_qty)
                    self.active_sell_id = None
                    self.last_fill_time = time.time()
                    trade_occurred = True

            if trade_occurred:
                self._print_stats()

        except Exception as e:
            logger.error(f"Sync Error: {e}")

    def _update_stats(self, side, price, qty):
        quote_vol = price * qty
        fee = quote_vol * self.cfg.TAKER_FEE_RATE
        self.stats['fill_count'] += 1
        self.stats['total_volume'] += qty
        self.stats['total_quote_vol'] += quote_vol
        self.stats['total_fee'] += fee

    def _print_stats(self):
        try:
            now = time.time()
            duration = now - self.start_time
            duration_str = str(timedelta(seconds=int(duration)))
            
            current_pnl = 0.0
            pnl_percent = 0.0
            if self.initial_equity > 0:
                current_pnl = self.equity - self.initial_equity
                pnl_percent = (current_pnl / self.initial_equity) * 100

            wear_rate = 0.0
            if self.stats['total_quote_vol'] > 0:
                wear_rate = ((current_pnl) / self.stats['total_quote_vol']) * 100

            beijing_now = datetime.utcnow() + timedelta(hours=8)
            time_str = beijing_now.strftime('%H:%M:%S')

            msg = (
                f"\n{'='*3} 📊 策略运行汇总 {'='*3}\n"
                f"模式: {self.symbol} (Unified) | {self.mode}\n"
                f"初始净值: {self.initial_equity:.2f}\n"
                f"当前净值: {self.equity:.2f} USDC\n"
                f"累计盈亏: {current_pnl:+.4f} USDC ({pnl_percent:+.2f}%)\n"
                f"-------\n"
                f"累计运行:{duration_str}\n"
                f"成交次数: {self.stats['fill_count']} 次\n"
                f"总成交额: {self.stats['total_quote_vol']:.2f} USDC\n"             
                f"资金磨损: {wear_rate:.4f}%\n"
                f"{'='*5} 当前时间:{time_str} **\n "
            )
            logger.info(msg)
        except Exception as e:
            logger.error(f"Print Stats Error: {e}")

    def _place(self, side, price, qty):
        price = round_to_step(price, self.tick_size)
        qty = floor_to(qty, self.base_precision)
        
        if qty < self.min_qty: return None

        try:
            # 基础下单参数
            payload = {
                "symbol": self.symbol,
                "side": side,
                "orderType": "Limit",
                "price": str(price),
                "quantity": str(qty),
                "postOnly": True 
            }

            # === [关键修改] 现货模式必须开启自动借贷参数才能裸卖 ===
            if not self.is_perp:
                # autoBorrow: 允许余额不足时自动借币（用于裸卖空或杠杆买入）
                payload["autoBorrow"] = True
                # autoBorrowRepay: 允许成交后自动偿还之前的借贷（用于平仓）
                payload["autoBorrowRepay"] = True

            res = self.rest.execute_order(payload)
            
            if "id" in res:
                return res["id"]
            else:
                msg = res.get("message", str(res))
                # 过滤掉一些常见的非致命错误日志，避免刷屏
                if "insufficient" in msg.lower():
                    logger.warning(f"⚠️ 资金不足无法下单 (AutoBorrow已开) [{side}]: {msg[:100]}")
                else:
                    logger.warning(f"⚠️ 下单失败 [{side}]: {msg}")
                return None
        except Exception as e:
            logger.error(f"下单异常: {e}")
            return None

    def cancel_all(self):
        try:
            self.rest.cancel_open_orders(self.symbol)
            self.active_buy_id = None
            self.active_sell_id = None
        except Exception as e:
            logger.error(f"Cancel All Error: {e}")

    def run(self):
        self.init_market_info()
        self.cancel_all()
        # 强制同步一次状态以获取初始 Equity
        self._sync_state()
        logger.info(f"🚀 DualMaker V3 (Unified) 启动 | 净值: {self.equity:.2f} | 杠杆: {self.cfg.LEVERAGE}x")
        
        while True:
            time.sleep(4.5) 

            try:
                self._sync_state()

                depth = self.rest.get_depth(self.symbol, limit=5)
                if not depth: continue
                
                bids = sorted(depth.get('bids', []), key=lambda x: float(x[0]), reverse=True)
                asks = sorted(depth.get('asks', []), key=lambda x: float(x[0]))
                
                if len(bids) < 2 or len(asks) < 2: continue
                
                bid_1 = float(bids[0][0])
                ask_1 = float(asks[0][0])

                # --- 风控检查 ---
                calc_price = self.avg_cost if self.avg_cost > 0 else (bid_1 + ask_1) / 2
                exposure = abs(self.held_qty * calc_price)
                
                effective_capital = self.equity * self.cfg.LEVERAGE
                if effective_capital <= 0: effective_capital = 1
                
                ratio = exposure / effective_capital
                
                # 仓位过重 -> 回本模式
                if ratio > self.cfg.MAX_POSITION_PCT:
                    if self.mode == "DUAL":
                        logger.warning(f"⚠️ 仓位过重 ({ratio:.1%}) -> UNWIND 模式")
                        self.mode = "UNWIND"
                        self.cancel_all()
                        self.unwind_start_time = time.time()
                
                # 仓位回归 -> 双向模式
                elif abs(self.held_qty) < self.min_qty and self.mode == "UNWIND":
                    logger.info(f"🎉 仓位已清空 -> DUAL 模式")
                    self.cancel_all()
                    self.mode = "DUAL"

                # 执行逻辑
                if self.mode == "DUAL":
                    self._logic_dual(bid_1, ask_1)
                else:
                    self._logic_unwind(bid_1, ask_1)

            except Exception as e:
                logger.error(f"Loop Error: {e}")
                time.sleep(1)

    def _logic_dual(self, target_bid, target_ask):
        """双向挂单逻辑 (Unified)"""
        
        has_buy = (self.active_buy_id is not None)
        has_sell = (self.active_sell_id is not None)
        
        if has_buy and has_sell: return 

        if has_buy != has_sell:
            self.cancel_all()
            return

        # 计算下单金额：基于 netEquity
        raw_qty = (self.equity * self.cfg.LEVERAGE * self.cfg.GRID_ORDER_PCT) / target_ask
        
        # 数量修正
        if raw_qty < self.min_qty: 
            return # 资金太少不足以开单
            
        if target_bid >= target_ask: return 

        # Unified 模式下，直接尝试双向开单
        # 如果 BP 支持现货裸空 (Unified Margin)，这里卖单会成功
        # 如果资金不足，_place 会捕获错误并打印，不影响下一轮重试
        
        new_buy_id = self._place("Bid", target_bid, raw_qty)
        new_sell_id = self._place("Ask", target_ask, raw_qty)
        
        if new_buy_id:
            self.active_buy_id = new_buy_id
            self.active_buy_price = target_bid
            self.active_buy_qty = raw_qty
            
        if new_sell_id:
            self.active_sell_id = new_sell_id
            self.active_sell_price = target_ask
            self.active_sell_qty = raw_qty
            
        if new_buy_id or new_sell_id:
            logger.info(f"✅ 尝试挂单: 买{raw_qty:.2f}@{target_bid} | 卖{raw_qty:.2f}@{target_ask}")

    def _logic_unwind(self, best_bid, best_ask):
        """回本模式"""
        timeout = (time.time() - self.unwind_start_time > self.cfg.BREAKEVEN_TIMEOUT)
        
        # === 计算基于总净值的回本价格 (Unified) ===
        # 目标: 平仓后 Equity >= Initial Equity
        mid_price = (best_bid + best_ask) / 2
        break_even_price = 0.0
        use_be_price = False

        if self.initial_equity > 0 and abs(self.held_qty) > self.min_qty:
            try:
                # 估算除去当前持仓后的剩余净值 (假设当前持仓价值被剥离)
                current_pos_value = self.held_qty * mid_price
                estimated_balance = self.equity - current_pos_value
                
                if self.held_qty > 0: # 多头
                    # 卖出得到的钱 + 余额 >= 初始净值
                    # Q * P * (1-fee) + Balance = Init
                    # P = (Init - Balance) / (Q * (1-fee))
                    numerator = self.initial_equity - estimated_balance
                    denominator = self.held_qty * (1 - self.cfg.TAKER_FEE_RATE)
                    if denominator != 0:
                        break_even_price = numerator / denominator
                        use_be_price = True
                else: # 空头
                    # 买入花费的钱，使得剩余余额 >= 初始净值
                    # Balance - Q_buy * P * (1+fee) = Init
                    # Q_buy * P * (1+fee) = Balance - Init
                    # P = (Balance - Init) / (abs(Q) * (1+fee))
                    numerator = estimated_balance - self.initial_equity
                    denominator = abs(self.held_qty) * (1 + self.cfg.TAKER_FEE_RATE)
                    if denominator != 0:
                        break_even_price = numerator / denominator
                        use_be_price = True
                
                if use_be_price:
                    logger.info(f"🧐 回本计算: 净值{self.equity:.2f} 初始{self.initial_equity:.2f} 持仓{self.held_qty:.4f} -> 目标价 {break_even_price:.4f}")

            except Exception as e:
                logger.error(f"Calc BE Price Error: {e}")

        # A: 多头平仓 (手里有币，要卖)
        if self.held_qty >= self.min_qty:
            if self.active_buy_id: self.cancel_all()
            
            # 价格策略: 如果有回本价，取 max(回本价, 市场价)；否则 fallback 到市场价或原成本价
            target = best_ask
            if not timeout:
                if use_be_price and break_even_price > 0:
                    target = max(break_even_price, best_ask)
                elif self.avg_cost > 0:
                    target = max(self.avg_cost + self.tick_size, best_ask)

            if self.active_sell_id:
                # 如果价格偏离过大则撤单重挂
                if abs(self.active_sell_price - target) > self.tick_size: # 稍微放宽一点检查阈值
                    self.cancel_all()
                    return

            if not self.active_sell_id:
                qty = abs(self.held_qty)
                self.active_sell_id = self._place("Ask", target, qty)
                if self.active_sell_id:
                    self.active_sell_price = target
                    self.active_sell_qty = qty

        # B: 空头平仓 (手里欠币，要买)
        elif self.held_qty <= -self.min_qty:
            if self.active_sell_id: self.cancel_all()
            
            target = best_bid
            if not timeout:
                if use_be_price and break_even_price > 0:
                    target = min(break_even_price, best_bid)
                elif self.avg_cost > 0:
                    target = min(self.avg_cost - self.tick_size, best_bid)

            if self.active_buy_id:
                if abs(self.active_buy_price - target) > self.tick_size:
                    self.cancel_all()
                    return

            if not self.active_buy_id:
                qty = abs(self.held_qty)
                self.active_buy_id = self._place("Bid", target, qty)
                if self.active_buy_id:
                    self.active_buy_price = target
                    self.active_buy_qty = qty
