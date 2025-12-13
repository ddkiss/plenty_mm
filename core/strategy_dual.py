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
        self.active_buy_price = 0.0
        self.active_buy_qty = 0.0
        self.active_sell_price = 0.0
        self.active_sell_qty = 0.0
        
        # 仓位与资产
        self.held_qty = 0.0
        self.avg_cost = 0.0
        self.equity = 0.0       # 交易净值
        self.real_equity = 0.0  # 真实净值
        
        # 策略状态
        self.mode = "DUAL"  
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

    # ============================================================
    # 阶段 1: 检查成交与状态 (轻量级)
    # ============================================================
    def _check_and_update_fills(self, open_orders):
        """
        基于传入的 open_orders 快照判断是否有成交。
        如果有成交，更新统计数据和成本。
        Returns: True (有成交) / False (无成交)
        """
        trade_occurred = False
        
        if not self.active_buy_id and not self.active_sell_id:
            return False

        try:
            # 提取当前存活的订单 ID 集合
            active_ids = {str(o['id']) for o in open_orders}
            
            # 1. 检查买单
            if self.active_buy_id:
                if str(self.active_buy_id) not in active_ids:
                    # 订单消失 -> 视为成交
                    logger.info(f"🔔 买单已成交 (ID: {self.active_buy_id})")
                    trade_occurred = True
                    
                    # 现货成本更新 (加权平均)
                    if not self.is_perp:
                        prev_qty = max(0, self.held_qty) 
                        fill_qty = self.active_buy_qty
                        fill_price = self.active_buy_price
                        
                        total_qty = prev_qty + fill_qty
                        if total_qty > 0:
                            new_avg = ((prev_qty * self.avg_cost) + (fill_qty * fill_price)) / total_qty
                            logger.info(f"📊 成本更新: {self.avg_cost:.4f} -> {new_avg:.4f}")
                            self.avg_cost = new_avg
                        else:
                            self.avg_cost = fill_price

                    self._update_stats("Buy", self.active_buy_price, self.active_buy_qty)
                    self.active_buy_id = None 
            
            # 2. 检查卖单
            if self.active_sell_id:
                if str(self.active_sell_id) not in active_ids:
                    logger.info(f"🔔 卖单已成交 (ID: {self.active_sell_id})")
                    trade_occurred = True
                    self._update_stats("Sell", self.active_sell_price, self.active_sell_qty)
                    self.active_sell_id = None

        except Exception as e:
            logger.error(f"Check Order Error: {e}")
            
        return trade_occurred

    # ============================================================
    # 阶段 2: 同步账户数据 (在撤单后执行，确保干净)
    # ============================================================
    def _sync_clean_state(self):
        """
        获取'无挂单状态下'的真实净值和持仓。
        """
        try:
            # 1. 获取 Collateral
            col = self.rest.get_collateral()
            if not isinstance(col, dict):
                return

            self.equity = float(col.get("netEquity", 0))

            # 2. 计算真实净值
            collateral_list = col.get("collateral", [])
            total_assets_notional = 0.0
            
            for asset in collateral_list:
                total_assets_notional += float(asset.get("balanceNotional", 0))

            borrow_liab = float(col.get("borrowLiability", 0)) 
            unrealized = float(col.get("pnlUnrealized", 0))    
            
            self.real_equity = total_assets_notional - borrow_liab + unrealized

            # 3. 获取准确持仓
            base_asset = self.symbol.split('_')[0].upper()
            found_qty = False
            new_held_qty = 0.0

            if self.is_perp:
                positions = self.rest.get_positions(self.symbol)
                if isinstance(positions, list):
                    for p in positions:
                        if p.get('symbol') == self.symbol:
                            new_held_qty = float(p.get('netQuantity', 0))
                            self.avg_cost = float(p.get('entryPrice', 0))
                            found_qty = True
                            break
            else:
                # 现货: 使用 borrowLend 获取净持仓
                bl_positions = self.rest.get_borrow_lend_positions()
                if isinstance(bl_positions, list):
                    for p in bl_positions:
                        if p.get('symbol', '').upper() == base_asset:
                            new_held_qty = float(p.get('netQuantity', 0))
                            found_qty = True
                            break
                
                # Fallback
                if not found_qty:
                    for asset in collateral_list:
                        if asset.get("symbol", "").upper() == base_asset:
                            new_held_qty = float(asset.get("totalQuantity", 0))
                            found_qty = True
                            break

            # 现货清仓检测
            if not self.is_perp and abs(new_held_qty) < self.min_qty and abs(self.held_qty) >= self.min_qty:
                self.avg_cost = 0.0
                logger.info("🧹 现货已彻底清空，成本重置为 0")

            if abs(new_held_qty - self.held_qty) > self.min_qty:
                logger.info(f"📦 持仓校准: {self.held_qty:.4f} -> {new_held_qty:.4f}")
            
            self.held_qty = new_held_qty

            # 初始化资金记录
            if self.initial_real_equity == 0 and self.real_equity > 0:
                self.initial_real_equity = self.real_equity
                logger.info(f"💰 初始本金锁定: {self.initial_real_equity:.2f} USDC")

        except Exception as e:
            logger.error(f"Sync State Error: {e}")

    # ============================================================
    # 辅助与执行
    # ============================================================
    def _update_stats(self, side, price, qty):
        quote_vol = price * qty
        fee = quote_vol * self.cfg.TAKER_FEE_RATE
        self.stats['fill_count'] += 1
        self.stats['total_volume'] += qty
        self.stats['total_quote_vol'] += quote_vol
        self.stats['total_fee'] += fee

    def _print_stats(self):
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
            wear_rate = (current_pnl / self.stats['total_quote_vol']) * 100

        beijing_now = datetime.utcnow() + timedelta(hours=8)
        time_str = beijing_now.strftime('%H:%M:%S')

        msg = (
            f"\n{'='*3} 📊 策略运行汇总 ({time_str}) {'='*3}\n"
            f"模式: {self.symbol} | {self.mode}\n"
            f"初始: {self.initial_real_equity:.2f}\n"
            f"当前: {self.real_equity:.2f}\n"
            f"持仓: {self.held_qty:.4f} (均价: {self.avg_cost:.4f})\n"
            f"盈亏: {current_pnl:+.4f} USDC ({pnl_percent:+.2f}%)\n"
            f"成交: {self.stats['fill_count']}次 \n"
            f"成交: {self.stats['total_quote_vol']:.1f} USDC\n"
            f"磨损: {wear_rate:.5f}%\n"
            f"运行时间: {duration_str}\n"
            f"{'='*5} {time_str} {'='*3}\n"
        )
        logger.info(msg)

    def cancel_all(self):
        try:
            self.rest.cancel_open_orders(self.symbol)
            self.active_buy_id = None
            self.active_sell_id = None
        except Exception as e:
            logger.error(f"Cancel Error: {e}")

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
                if "insufficient" not in msg.lower():
                    logger.warning(f"⚠️ 下单失败: {msg}")
                return None
        except Exception:
            return None

    # ============================================================
    # 主循环逻辑
    # ============================================================
    def run(self):
        self.init_market_info()
        
        # 启动前先清理并同步一次
        self.cancel_all()
        time.sleep(1)
        self._sync_clean_state()
        
        if not self.is_perp and self.held_qty > self.min_qty and self.avg_cost == 0:
            depth = self.rest.get_depth(self.symbol, limit=1)
            if depth: self.avg_cost = float(depth['bids'][0][0])

        logger.info("🚀 策略已启动 (Smart Rebalance 模式)")

        while True:
            try:
                # 1. 获取行情 (用于判断是否需要调价)
                depth = self.rest.get_depth(self.symbol, limit=5)
                if not depth: 
                    time.sleep(1)
                    continue
                
                bids = sorted(depth.get('bids', []), key=lambda x: float(x[0]), reverse=True)
                asks = sorted(depth.get('asks', []), key=lambda x: float(x[0]))
                if len(bids) < 2 or len(asks) < 2: continue
                bid_1, ask_1 = float(bids[0][0]), float(asks[0][0])

                # 2. 获取当前挂单 (Snapshot)
                open_orders = self.rest.get_open_orders(self.symbol)
                if not isinstance(open_orders, list): open_orders = []

                # 3. 检查成交 (Order Check)
                trade_happened = self._check_and_update_fills(open_orders)

                # 4. 决策: 是否需要重置订单? (Rebalance Check)
                needs_rebalance = False
                
                # A: 发生成交 -> 必须重置
                if trade_happened:
                    needs_rebalance = True
                
                # B: 挂单缺失 -> 必须补单
                elif self.mode == "DUAL" and (not self.active_buy_id or not self.active_sell_id):
                    needs_rebalance = True
                elif self.mode == "UNWIND" and (not self.active_buy_id and not self.active_sell_id):
                    # Unwind 模式下至少要有一个反向单
                    needs_rebalance = True
                
                # C: 价格偏离 (Price Drift)
                # === [修改点] UNWIND 模式下，除非超时，否则忽略价格偏离，避免反复撤单 ===
                else:
                    is_timeout = False
                    if self.mode == "UNWIND":
                        is_timeout = (time.time() - self.unwind_start_time > self.cfg.BREAKEVEN_TIMEOUT)
                    
                    # 只有在 DUAL 模式 或 UNWIND超时(追单) 模式下，才检查盘口偏离
                    if self.mode == "DUAL" or (self.mode == "UNWIND" and is_timeout):
                        if self.active_buy_id and abs(self.active_buy_price - bid_1) > self.tick_size * 3:
                            needs_rebalance = True
                        if self.active_sell_id and abs(self.active_sell_price - ask_1) > self.tick_size * 3:
                            needs_rebalance = True

                # 5. 执行逻辑
                if not needs_rebalance:
                    # 静默待机
                    time.sleep(1)
                    continue
                
                # --- 进入重置流程 (Cancel -> Sync -> Place) ---
                
                self.cancel_all()     
                time.sleep(1)       
                self._sync_clean_state() 
                
                if trade_happened:
                    self._print_stats()

                # 风控检查
                mid_price = (bid_1 + ask_1) / 2
                exposure = abs(self.held_qty * mid_price)
                effective_capital = self.equity * self.cfg.LEVERAGE 
                if effective_capital <= 0: effective_capital = 1
                
                ratio = exposure / effective_capital
                
                if ratio > self.cfg.MAX_POSITION_PCT:
                    if self.mode == "DUAL":
                        logger.warning(f"⚠️ 仓位过重 ({ratio:.1%}) -> 切换 UNWIND")
                        self.mode = "UNWIND"
                        self.unwind_start_time = time.time()
                elif abs(self.held_qty) < self.min_qty and self.mode == "UNWIND":
                    logger.info("🎉 仓位回归 -> 切换 DUAL")
                    self.mode = "DUAL"

                # 计算并挂单
                if self.mode == "DUAL":
                    self._logic_dual(bid_1, ask_1)
                else:
                    self._logic_unwind(bid_1, ask_1)

                time.sleep(self.cfg.REBALANCE_WAIT)

            except Exception as e:
                logger.error(f"Main Loop Error: {e}")
                time.sleep(1)

    def _logic_dual(self, target_bid, target_ask):
        raw_qty = (self.equity * self.cfg.LEVERAGE * self.cfg.GRID_ORDER_PCT) / target_ask
        if raw_qty < self.min_qty: return 
        if target_bid >= target_ask: return 
        
        buy_id = self._place("Bid", target_bid, raw_qty)
        sell_id = self._place("Ask", target_ask, raw_qty)
        
        if buy_id:
            self.active_buy_id = buy_id
            self.active_buy_price = target_bid
            self.active_buy_qty = raw_qty
        if sell_id:
            self.active_sell_id = sell_id
            self.active_sell_price = target_ask
            self.active_sell_qty = raw_qty
            
        if buy_id or sell_id:
            logger.info(f"✅ DUAL: 买{target_bid} | 卖{target_ask} (Qty: {raw_qty:.2f})")

    def _logic_unwind(self, best_bid, best_ask):
        deficit = max(0.0, self.initial_real_equity - self.real_equity)
        duration = time.time() - self.unwind_start_time
        is_timeout = duration > self.cfg.BREAKEVEN_TIMEOUT
        
        mid_price = (best_bid + best_ask) / 2
        qty_abs = abs(self.held_qty)
        
        markup_per_unit = 0.0
        if qty_abs > self.min_qty:
            markup_per_unit = deficit / qty_abs
        
        # A: 多头平仓
        if self.held_qty >= self.min_qty:
            target = mid_price + markup_per_unit
            
            if is_timeout:
                decay = min(1.0, (duration - self.cfg.BREAKEVEN_TIMEOUT) / 600)
                target = target * (1 - decay) + best_ask * decay
                if decay > 0.1: logger.warning(f"⏰ Unwind衰减: {target:.4f}")

            final_price = max(target, best_ask)
            
            logger.info(f"🛡️ Unwind(Long): 目标{final_price:.3f} (Deficit: {deficit:.2f})")
            self.active_sell_id = self._place("Ask", final_price, qty_abs)
            if self.active_sell_id:
                self.active_sell_price = final_price
                self.active_sell_qty = qty_abs

        # B: 空头平仓
        elif self.held_qty <= -self.min_qty:
            target = mid_price - markup_per_unit
            if target <= 0: target = best_bid * 0.5
            
            if is_timeout:
                decay = min(1.0, (duration - self.cfg.BREAKEVEN_TIMEOUT) / 600)
                target = target * (1 - decay) + best_bid * decay
                if decay > 0.1: logger.warning(f"⏰ Unwind衰减: {target:.4f}")

            final_price = min(target, best_bid)
            
            logger.info(f"🛡️ Unwind(Short): 目标{final_price:.3f} (Deficit: {deficit:.2f})")
            self.active_buy_id = self._place("Bid", final_price, qty_abs)
            if self.active_buy_id:
                self.active_buy_price = final_price
                self.active_buy_qty = qty_abs
