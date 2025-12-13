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
        
        # === [核心修改] 区分 "交易净值" 和 "真实净值" ===
        self.equity = 0.0       # netEquity (含折扣，用于下单风控)
        self.real_equity = 0.0  # Real Value (无折扣，用于计算真实盈亏)
        
        # 策略状态
        self.mode = "DUAL"  # DUAL / UNWIND
        self.last_fill_time = 0 
        self.unwind_start_time = 0
        
        # 统计数据
        self.start_time = time.time()
        self.initial_real_equity = 0.0 # 记录初始的真实净值
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
        同步状态核心 (Unified Margin):
        1. 获取 MarginAccountSummary 对象。
        2. 提取 netEquity 用于交易风控 (Risk-Adjusted)。
        3. 提取 assetsValue, borrowLiability, pnlUnrealized 计算真实净值 (No Haircut)。
        """
        try:
            # --- 1. 获取联合保证金账户数据 ---
            col = self.rest.get_collateral()
            
            if not isinstance(col, dict):
                logger.error(f"获取 Collateral 失败: {col}")
                return

            # A. 获取交易净值 (含折扣) - 用于计算下单量和交易所风控
            self.equity = float(col.get("netEquity", 0))

            # B. 计算真实净值 (无折扣) - 用于显示盈亏
            # 严格使用 MarginAccountSummary 字段，不进行手动计算
            assets_val = float(col.get("assetsValue", 0))       # 现货资产名义价值 (正值)
            borrow_liab = float(col.get("borrowLiability", 0)) # 借贷名义价值 (正值，代表负债)
            unrealized = float(col.get("pnlUnrealized", 0))    # 合约未实现盈亏 (可正可负)
            
            # Real Equity = 资产总值 - 负债总值 + 未实现盈亏
            self.real_equity = assets_val - borrow_liab + unrealized

            # 记录初始资金 (只记录一次，且必须大于0)
            if self.initial_real_equity == 0 and self.real_equity > 0:
                self.initial_real_equity = self.real_equity
                logger.info(f"💰 初始真实本金记录: {self.initial_real_equity:.2f} USDC (无折扣市值)")

            # --- 2. 获取持仓数量 (Held Qty) ---
            if self.is_perp:
                # === 合约模式 ===
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
                collateral_list = col.get("collateral", [])
                base_asset = self.symbol.split('_')[0] 
                
                found_asset = False
                for asset in collateral_list:
                    if asset.get("symbol") == base_asset:
                        # 现货持仓 = totalQuantity (API文档显示这是未打折的总量)
                        # totalQuantity = available + locked + staked
                        self.held_qty = float(asset.get("totalQuantity", 0))
                        found_asset = True
                        break
                
                if not found_asset:
                    self.held_qty = 0.0
                
                # 现货成本估算
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
        """打印统计信息，使用真实净值(Real Equity)计算盈亏"""
        try:
            now = time.time()
            duration = now - self.start_time
            duration_str = str(timedelta(seconds=int(duration)))
            
            current_pnl = 0.0
            pnl_percent = 0.0
            
            # 使用无折扣的 Real Equity 计算盈亏
            if self.initial_real_equity > 0:
                current_pnl = self.real_equity - self.initial_real_equity
                pnl_percent = (current_pnl / self.initial_real_equity) * 100

            wear_rate = 0.0
            if self.stats['total_quote_vol'] > 0:
                wear_rate = ((current_pnl) / self.stats['total_quote_vol']) * 100

            beijing_now = datetime.utcnow() + timedelta(hours=8)
            time_str = beijing_now.strftime('%H:%M:%S')

            msg = (
                f"\n{'='*3} 📊 策略运行汇总 \n"
                f"模式: {self.symbol} (Unified) | {self.mode}\n"
                f"初始本金: {self.initial_real_equity:.2f} USDC\n"
                f"真实净值: {self.real_equity:.2f} USDC (准确盈亏)\n"
                f"交易净值: {self.equity:.2f} USDC (风控/下单)\n"
                f"累计盈亏: {current_pnl:+.4f} USDC ({pnl_percent:+.2f}%)\n"
                f"-------\n"
                f"累计运行:   {duration_str}\n"
                f"成交次数: {self.stats['fill_count']} 次\n"
                f"总成交额: {self.stats['total_quote_vol']:.2f} USDC\n"             
                f"资金磨损: {wear_rate:.4f}%\n"
                f"{'='*5} ({time_str}) {'='*3}\n "
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

            # 现货模式必须开启自动借贷参数才能裸卖 (Auto Borrow)
            if not self.is_perp:
                payload["autoBorrow"] = True
                payload["autoBorrowRepay"] = True

            res = self.rest.execute_order(payload)
            
            if "id" in res:
                return res["id"]
            else:
                msg = res.get("message", str(res))
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
        logger.info(f"🚀 DualMaker V3 (Unified) 启动 | 真实净值: {self.real_equity:.2f} | 杠杆: {self.cfg.LEVERAGE}x")
        
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
                
                # [注意] 风控比例计算依然使用 self.equity (netEquity)，因为交易所也是按这个来爆仓的
                effective_capital = self.equity * self.cfg.LEVERAGE
                if effective_capital <= 0: effective_capital = 1
                
                ratio = exposure / effective_capital
                
                # 仓位过重 -> 回本模式
                if ratio > self.cfg.MAX_POSITION_PCT:
                    if self.mode == "DUAL":
                        logger.warning(f"⚠️ 仓位过重 ({ratio:.1%} 风险权益) -> UNWIND 模式")
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

        # 计算下单金额：基于 netEquity (交易所认可的保证金)
        raw_qty = (self.equity * self.cfg.LEVERAGE * self.cfg.GRID_ORDER_PCT) / target_ask
        
        if raw_qty < self.min_qty: 
            return 
            
        if target_bid >= target_ask: return 
        
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
        
        # === 计算回本价格 (基于真实净值) ===
        # 目标: 平仓后 Real Equity >= Initial Real Equity
        break_even_price = 0.0
        use_be_price = False

        if self.initial_real_equity > 0 and abs(self.held_qty) > self.min_qty:
            try:
                # 估算除去当前持仓后的剩余真实余额
                mid_price = (best_bid + best_ask) / 2
                current_pos_value = self.held_qty * mid_price
                estimated_balance = self.real_equity - current_pos_value
                
                if self.held_qty > 0: # 多头持仓，计算卖出价格
                    # 目标: estimated_balance + (Qty * Price * (1-fee)) = Initial
                    numerator = self.initial_real_equity - estimated_balance
                    denominator = self.held_qty * (1 - self.cfg.TAKER_FEE_RATE)
                    if denominator != 0:
                        break_even_price = numerator / denominator
                        use_be_price = True
                else: # 空头持仓，计算买入价格
                    # 目标: estimated_balance - (abs(Qty) * Price * (1+fee)) = Initial
                    numerator = estimated_balance - self.initial_real_equity
                    denominator = abs(self.held_qty) * (1 + self.cfg.TAKER_FEE_RATE)
                    if denominator != 0:
                        break_even_price = numerator / denominator
                        use_be_price = True
                
            except Exception as e:
                logger.error(f"Calc BE Price Error: {e}")

        # A: 多头平仓
        if self.held_qty >= self.min_qty:
            if self.active_buy_id: self.cancel_all()
            
            target = best_ask
            if not timeout:
                if use_be_price and break_even_price > 0:
                    target = max(break_even_price, best_ask)
                elif self.avg_cost > 0:
                    target = max(self.avg_cost + self.tick_size, best_ask)

            if self.active_sell_id:
                if abs(self.active_sell_price - target) > self.tick_size:
                    self.cancel_all()
                    return

            if not self.active_sell_id:
                qty = abs(self.held_qty)
                self.active_sell_id = self._place("Ask", target, qty)
                if self.active_sell_id:
                    self.active_sell_price = target
                    self.active_sell_qty = qty

        # B: 空头平仓
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