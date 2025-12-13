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
                f"\n{'='*3} 📊 策略运行汇总 ({time_str}) {'='*3}\n"
                f"模式: {self.symbol} (Unified) | {self.mode}\n"
                f"当前净值: {self.equity:.2f} USDC (初始 {self.initial_equity:.2f})\n"
                f"累计盈亏: {current_pnl:+.4f} USDC ({pnl_percent:+.2f}%)\n"
                f"---\n"
                f"成交次数: {self.stats['fill_count']} 次\n"
                f"总成交额: {self.stats['total_quote_vol']:.2f} USDC\n"             
                f"资金磨损: {wear_rate:.4f}%\n"
                f"{'='*5}\n"
            )
            logger.info(msg)
        except Exception as e:
            logger.error(f"Print Stats Error: {e}")

    def _place(self, side, price, qty):
        price = round_to_step(price, self.tick_size)
        qty = floor_to(qty, self.base_precision)
        
        if qty < self.min_qty: return None

        try:
            # 统一使用 Limit 挂单
            # Backpack Unified 模式下，只要净值足够，现货卖单如果没货会自动借币(如果开启了自动借币)
            # 或者直接走联合保证金逻辑
            res = self.rest.execute_order({
                "symbol": self.symbol,
                "side": side,
                "orderType": "Limit",
                "price": str(price),
                "quantity": str(qty),
                "postOnly": True 
            })
            
            if "id" in res:
                return res["id"]
            else:
                # 捕获 "Insufficient funds" 或其他错误，只打印不崩溃
                msg = res.get("message", str(res))
                if "insufficient" in msg.lower():
                    logger.warning(f"⚠️ 资金不足无法下单 [{side}]: {msg[:100]}")
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
        unknown_cost = (self.avg_cost <= 0)

        # A: 多头平仓 (手里有币，要卖)
        if self.held_qty >= self.min_qty:
            if self.active_buy_id: self.cancel_all()
            
            if self.active_sell_id and (timeout or unknown_cost):
                if abs(self.active_sell_price - best_ask) > self.tick_size / 2:
                    self.cancel_all()
                    return 

            if not self.active_sell_id:
                target = best_ask if (timeout or unknown_cost) else max(self.avg_cost + self.tick_size, best_ask)
                qty = abs(self.held_qty)
                self.active_sell_id = self._place("Ask", target, qty)
                if self.active_sell_id:
                    self.active_sell_price = target
                    self.active_sell_qty = qty

        # B: 空头平仓 (手里欠币，要买)
        # 这里的判断 abs(held_qty) 兼容了现货借币卖出的情况(可能是负数也可能是借贷记录)
        # Unified 模式下，净空头通常表现为负数 netQuantity (Perp) 或 负数 assets (Spot Margin?) 
        # 我们这里主要处理 Perp 风格的负数持仓
        elif self.held_qty <= -self.min_qty:
            if self.active_sell_id: self.cancel_all()
            
            if self.active_buy_id and (timeout or unknown_cost):
                if abs(self.active_buy_price - best_bid) > self.tick_size / 2:
                    self.cancel_all()
                    return

            if not self.active_buy_id:
                target = best_bid if (timeout or unknown_cost) else min(self.avg_cost - self.tick_size, best_bid)
                qty = abs(self.held_qty)
                self.active_buy_id = self._place("Bid", target, qty)
                if self.active_buy_id:
                    self.active_buy_price = target
                    self.active_buy_qty = qty
