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
        self.equity = 0.0       
        self.real_equity = 0.0  
        
        # 策略状态
        self.mode = "DUAL"  
        self.last_fill_time = 0 
        self.unwind_start_time = 0
        
        # 统计数据
        self.start_time = time.time()
        self.initial_real_equity = 0.0 
        self.stats = {
            'fill_count': 0,
            'total_volume': 0.0,
            'total_quote_vol': 0.0,
            'total_fee': 0.0,
        }
        
        self.is_perp = "PERP" in self.symbol.upper()

    def init_market_info(self):
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
        1. 获取 netEquity 用于交易风控。
        2. 遍历 collateral 累加 balanceNotional 计算真实净值。
        3. 检测成交并更新现货成本 (Weighted Avg)。
        """
        try:
            # --- 1. 获取联合保证金账户数据 ---
            col = self.rest.get_collateral()
            if not isinstance(col, dict):
                logger.error(f"获取 Collateral 失败: {col}")
                return

            # A. 获取交易净值 (含折扣)
            self.equity = float(col.get("netEquity", 0))

            # B. 计算真实净值 (无折扣)
            collateral_list = col.get("collateral", [])
            total_assets_notional = 0.0
            
            # [修复] 强制转换为大写，确保匹配准确
            base_asset = self.symbol.split('_')[0].upper()
            found_asset = False
            
            # [调试] 如果持仓一直不动，开启下面这行注释查看 API 返回了什么
            # logger.info(f"🔍 寻找资产: {base_asset} | 当前列表: {[a.get('symbol') for a in collateral_list]}")

            for asset in collateral_list:
                # 累加每个资产的真实名义价值
                total_assets_notional += float(asset.get("balanceNotional", 0))
                
                # 获取当前交易对的持仓
                # [修复] 这里的 asset.get('symbol') 也要对比大写
                asset_symbol = asset.get("symbol", "").upper()
                
                if asset_symbol == base_asset:
                    # 净持仓 = 总资产 - 借贷
                    qty_total = float(asset.get("totalQuantity", 0))
                    qty_borrow = float(asset.get("borrowedQuantity", 0))
                    
                    new_held_qty = qty_total - qty_borrow
                    
                    # 仅当数量发生显著变化时才打印日志，避免刷屏
                    if abs(new_held_qty - self.held_qty) > self.min_qty:
                        logger.info(f"📦 持仓更新 ({base_asset}): {self.held_qty:.4f} -> {new_held_qty:.4f}")
                        
                    self.held_qty = new_held_qty
                    found_asset = True

            borrow_liab = float(col.get("borrowLiability", 0)) 
            unrealized = float(col.get("pnlUnrealized", 0))    
            
            # Real Equity = 真实资产总值 - 负债总值 + 未实现盈亏
            self.real_equity = total_assets_notional - borrow_liab + unrealized

            # 如果没找到持仓，且非合约，置0
            if not found_asset and not self.is_perp:
                if self.held_qty != 0:
                    logger.info(f"🧹 资产 {base_asset} 不在抵押品列表中，持仓归零")
                self.held_qty = 0.0
            
            # 合约持仓单独获取 (补充 entryPrice)
            if self.is_perp:
                positions = self.rest.get_positions(self.symbol)
                found_pos = False
                if isinstance(positions, list):
                    for p in positions:
                        if p.get('symbol') == self.symbol:
                            self.held_qty = float(p.get('netQuantity', 0))
                            self.avg_cost = float(p.get('entryPrice', 0))
                            found_pos = True
                            break
                if not found_pos:
                    self.held_qty = 0.0
                    self.avg_cost = 0.0

            # 记录初始资金
            if self.initial_real_equity == 0 and self.real_equity > 0:
                self.initial_real_equity = self.real_equity
                logger.info(f"💰 初始真实本金记录: {self.initial_real_equity:.2f} USDC (无折扣市值)")

            # --- 3. 反推订单状态与更新成本 (现货) ---
            open_orders = self.rest.get_open_orders(self.symbol)
            if not isinstance(open_orders, list):
                open_orders = [] 
            
            active_ids = {str(o['id']) for o in open_orders}
            
            # 检查买单成交 (更新现货成本)
            if self.active_buy_id and str(self.active_buy_id) not in active_ids:
                fill_qty = self.active_buy_qty
                fill_price = self.active_buy_price
                logger.info(f"🔔 买单结束/成交 (ID: {self.active_buy_id})")
                
                # 现货成本加权平均
                if not self.is_perp:
                    # 注意：此时 self.held_qty 已经在上面通过 API 更新为最新值了
                    # 我们需要反推成交前的数量来计算加权
                    prev_qty = max(0, self.held_qty - fill_qty)
                    
                    if self.held_qty > 0:
                        new_avg = ((prev_qty * self.avg_cost) + (fill_qty * fill_price)) / self.held_qty
                        logger.info(f"📊 现货成本更新: {self.avg_cost:.4f} -> {new_avg:.4f}")
                        self.avg_cost = new_avg
                    else:
                        self.avg_cost = fill_price

                self._update_stats("Buy", fill_price, fill_qty)
                self.active_buy_id = None 
                self.last_fill_time = time.time()
                self._print_stats() # 成交后立即打印一次状态

            # 检查卖单成交
            if self.active_sell_id and str(self.active_sell_id) not in active_ids:
                logger.info(f"🔔 卖单结束/成交 (ID: {self.active_sell_id})")
                
                # 现货清仓重置
                if not self.is_perp and abs(self.held_qty) < self.min_qty:
                    self.avg_cost = 0.0
                    logger.info("🧹 现货已清仓，成本重置为 0")

                self._update_stats("Sell", self.active_sell_price, self.active_sell_qty)
                self.active_sell_id = None
                self.last_fill_time = time.time()
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
            
            if self.initial_real_equity > 0:
                current_pnl = self.real_equity - self.initial_real_equity
                pnl_percent = (current_pnl / self.initial_real_equity) * 100

            wear_rate = 0.0
            if self.stats['total_quote_vol'] > 0:
                wear_rate = ((current_pnl) / self.stats['total_quote_vol']) * 100

            beijing_now = datetime.utcnow() + timedelta(hours=8)
            time_str = beijing_now.strftime('%H:%M:%S')

            msg = (
                f"\n{'='*3} 📊 策略运行汇总 {'='*3}\n"
                f"模式: {self.symbol} | {self.mode}\n"
                f"初始本金: {self.initial_real_equity:.2f} USDC\n"
                f"真实净值: {self.real_equity:.2f} USDC\n"
                f"当前持仓: {self.held_qty:.4f}\n"
                f"累计盈亏: {current_pnl:+.4f} USDC ({pnl_percent:+.2f}%)\n"
                f"-------\n"
                f"累计运行: {duration_str}\n"
                f"成交次数: {self.stats['fill_count']} 次\n"
                f"总成交额: {self.stats['total_quote_vol']:.2f} USDC\n"             
                f"资金磨损: {wear_rate:.4f}%\n"
                f"{'='*5} ({time_str}) {'='*3} \n "
            )
            logger.info(msg)
        except Exception as e:
            logger.error(f"Print Stats Error: {e}")

    def _place(self, side, price, qty):
        price = round_to_step(price, self.tick_size)
        qty = floor_to(qty, self.base_precision)
        
        if qty < self.min_qty: return None

        try:
            payload = {
                "symbol": self.symbol,
                "side": side,
                "orderType": "Limit",
                "price": str(price),
                "quantity": str(qty),
                "postOnly": True 
            }

            if not self.is_perp:
                payload["autoBorrow"] = True
                payload["autoBorrowRepay"] = True

            res = self.rest.execute_order(payload)
            
            if "id" in res:
                return res["id"]
            else:
                msg = res.get("message", str(res))
                if "insufficient" in msg.lower():
                    logger.warning(f"⚠️ 资金不足(AutoBorrow): {msg[:50]}")
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
        self._sync_state()
        logger.info(f"🚀 DualMaker V3 启动 | 真实净值: {self.real_equity:.2f} | 杠杆: {self.cfg.LEVERAGE}x")
        
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
                # 持仓价值计算 (使用市价)
                mid_price = (bid_1 + ask_1) / 2
                exposure = abs(self.held_qty * mid_price)
                
                # 杠杆限制基于 netEquity
                effective_capital = self.equity * self.cfg.LEVERAGE
                if effective_capital <= 0: effective_capital = 1
                
                ratio = exposure / effective_capital
                
                # 仓位过重 -> UNWIND
                if ratio > self.cfg.MAX_POSITION_PCT:
                    if self.mode == "DUAL":
                        logger.warning(f"⚠️ 仓位过重 ({ratio:.1%}) -> UNWIND 模式")
                        self.mode = "UNWIND"
                        self.cancel_all()
                        self.unwind_start_time = time.time()
                
                # 仓位回归 -> DUAL
                elif abs(self.held_qty) < self.min_qty and self.mode == "UNWIND":
                    logger.info(f"🎉 仓位已清空 -> DUAL 模式")
                    self.cancel_all()
                    self.mode = "DUAL"

                # 执行逻辑
                if self.mode == "DUAL":
                    self._logic_dual(bid_1, ask_1)
                elif self.mode == "UNWIND":
                    self._logic_unwind(bid_1, ask_1)

            except Exception as e:
                logger.error(f"Loop Error: {e}")
                time.sleep(1)

    def _logic_dual(self, target_bid, target_ask):
        """双向挂单逻辑"""
        has_buy = (self.active_buy_id is not None)
        has_sell = (self.active_sell_id is not None)
        
        if has_buy and has_sell: return 
        if has_buy != has_sell:
            self.cancel_all()
            return

        raw_qty = (self.equity * self.cfg.LEVERAGE * self.cfg.GRID_ORDER_PCT) / target_ask
        if raw_qty < self.min_qty: return 
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
            logger.info(f"✅ DUAL挂单: 买{raw_qty:.2f}@{target_bid} | 卖{raw_qty:.2f}@{target_ask}")

    def _logic_unwind(self, best_bid, best_ask):
        """
        统一回本模式 (Unified Unwind):
        目标: 让 RealEquity 回到 InitialEquity。
        方法: 将亏损平摊到当前持仓上，叠加在当前市价上。
        公式: Target = CurrentPrice +/- (Deficit / Quantity)
        """
        # 1. 计算总亏损 (Deficit)
        deficit = max(0.0, self.initial_real_equity - self.real_equity)
        
        # 2. 超时检测
        duration = time.time() - self.unwind_start_time
        is_timeout = duration > self.cfg.BREAKEVEN_TIMEOUT
        
        # 3. 基础价格: 使用当前盘口均价 (Mark-to-Market 逻辑)
        mid_price = (best_bid + best_ask) / 2
        
        # 4. 计算每个持仓单位需要承担的亏损 (Markup)
        qty_abs = abs(self.held_qty)
        markup_per_unit = 0.0
        if qty_abs > self.min_qty:
            markup_per_unit = deficit / qty_abs
        
        # ==========================================
        # 场景 A: 多头 (Long) -> 卖出
        # ==========================================
        if self.held_qty >= self.min_qty:
            if self.active_buy_id: self.cancel_all()
            
            # 目标卖出价 = 当前市价 + 平摊亏损
            # 我们希望以比当前市价高出 markup 的价格卖出，从而收回 deficit
            target_price = mid_price + markup_per_unit
            
            # 超时衰减: 逐渐放弃回本，贴近市场价
            if is_timeout:
                decay = min(1.0, (duration - self.cfg.BREAKEVEN_TIMEOUT) / 600)
                # 目标价向 Best Ask 靠拢
                target_price = target_price * (1 - decay) + best_ask * decay
                if decay > 0.1: logger.warning(f"⏰ Unwind衰减(Long): {target_price:.4f}")

            # 挂单价不能低于 Best Ask (保证是 Maker 且不亏损太多)
            final_ask = max(target_price, best_ask)
            
            if self.active_sell_id:
                # 价格差异过大才改单
                if abs(self.active_sell_price - final_ask) > self.tick_size:
                    self.cancel_all()
                    return

            if not self.active_sell_id:
                logger.info(f"🛡️ 清仓(Long): 市价{mid_price:.2f} + 填坑{markup_per_unit:.4f} -> 挂{final_ask:.2f}")
                self.active_sell_id = self._place("Ask", final_ask, qty_abs)
                if self.active_sell_id:
                    self.active_sell_price = final_ask
                    self.active_sell_qty = qty_abs

        # ==========================================
        # 场景 B: 空头 (Short) -> 买入
        # ==========================================
        elif self.held_qty <= -self.min_qty:
            if self.active_sell_id: self.cancel_all()
            
            # 目标买入价 = 当前市价 - 平摊亏损
            # 我们希望以比当前市价低 markup 的价格买入
            target_price = mid_price - markup_per_unit
            
            # 价格安全保护
            if target_price <= 0: target_price = best_bid * 0.5
            
            # 超时衰减
            if is_timeout:
                decay = min(1.0, (duration - self.cfg.BREAKEVEN_TIMEOUT) / 600)
                target_price = target_price * (1 - decay) + best_bid * decay
                if decay > 0.1: logger.warning(f"⏰ Unwind衰减(Short): {target_price:.4f}")

            # 挂单价不能高于 Best Bid
            final_bid = min(target_price, best_bid)
            
            if self.active_buy_id:
                if abs(self.active_buy_price - final_bid) > self.tick_size:
                    self.cancel_all()
                    return

            if not self.active_buy_id:
                logger.info(f"🛡️ 平空(Short): 市价{mid_price:.2f} - 填坑{markup_per_unit:.4f} -> 挂{final_bid:.2f}")
                self.active_buy_id = self._place("Bid", final_bid, qty_abs)
                if self.active_buy_id:
                    self.active_buy_price = final_bid
                    self.active_buy_qty = qty_abs
