import time
import threading
from datetime import datetime, timedelta
from .utils import logger, round_to_step, floor_to
from .rest_client import BackpackREST
from .ws_client import BackpackWS

class TickScalper:
    def __init__(self, config):
        self.cfg = config
        self.symbol = config.SYMBOL
        # [新增] 记录挂单产生的时间
        self.active_order_time = 0
        
        # Clients
        self.rest = BackpackREST(config.API_KEY, config.SECRET_KEY)
        self.ws = BackpackWS(config.API_KEY, config.SECRET_KEY, self.symbol, self.on_order_update)
        
        # State
        self.state = "IDLE"  # IDLE, BUYING, SELLING
        # 策略激活状态标记，用于过滤启动时的清仓数据
        self.strategy_active = False
        # 连续亏损计数器
        self.consecutive_loss_count = 0
        
        # Order Tracking
        self.active_order_id = None
        self.active_order_price = 0.0
        self.active_order_side = None 
        
        # Position Tracking
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
        
        # --- 统计数据 ---
        self.start_time = time.time()
        self.stats = {
            'total_buy_qty': 0.0,
            'total_sell_qty': 0.0,
            'total_quote_vol': 0.0,  # 总成交额 (USDC)
            'maker_buy_qty': 0.0,
            'maker_sell_qty': 0.0,
            'taker_buy_qty': 0.0,
            'taker_sell_qty': 0.0,
            'total_pnl': 0.0,        # 累计盈亏 (扣除手续费前)
            'total_fee': 0.0,        # 累计手续费
            'trade_count': 0         # 成交次数
        }

    def init_market_info(self):
        try:
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
        except Exception as e:
            logger.error(f"Init Market Info Failed: {e}")
            exit(1)

    def get_usdc_balance(self):
        """获取用于交易的可用余额"""
        try:
            # 1. 合约交易 (PERP)
            if "PERP" in self.symbol:
                col_res = self.rest.get_collateral()
                if isinstance(col_res, dict):
                    if "netEquityAvailable" in col_res:
                        return float(col_res["netEquityAvailable"])
                    
                    # Fallback logic
                    total_col = 0.0
                    assets = col_res.get("collateral", []) or col_res.get("assets", [])
                    for asset in assets:
                        if asset.get("symbol") == "USDC":
                            total_col += float(asset.get("availableQuantity", 0))
                            total_col += float(asset.get("lendQuantity", 0))
                    return total_col

            # 2. 现货交易 (Spot)
            spot_res = self.rest.get_balance()
            if isinstance(spot_res, dict) and "USDC" in spot_res:
                data = spot_res["USDC"]
                if isinstance(data, dict):
                    return float(data.get("available", 0))
                else:
                    return float(data)
        except Exception as e:
            logger.error(f"Get Balance Error: {e}")
        
        return 0.0

    def on_order_update(self, data):
        # 如果策略未正式激活（处于清仓阶段），忽略所有订单推送
        if not self.strategy_active:
            return
        try:
            event = data.get('e')
            
            # --- [修复] 处理订单取消/过期事件 ---
            if event in ['orderCancel', 'orderExpire']:
                order_id = data.get('i')
                # 如果被取消的是当前活跃订单，必须立即重置 ID，防止策略死锁
                if order_id == self.active_order_id:
                    logger.warning(f"⚠️ 订单 {order_id} 已取消/过期，重置状态")
                    self.active_order_id = None
                    self.active_order_side = None
                    # 如果是在买入阶段被取消，重置回 IDLE 重新开始
                    if self.state == "BUYING":
                        self.state = "IDLE"
                    # 如果是在卖出阶段被取消，保持 SELLING 状态，主循环会自动补单
                return

            # --- 处理成交事件 ---
            if event == 'orderFill':
                side = data.get('S') # Bid/Ask
                price = float(data.get('L')) # Fill Price
                qty = float(data.get('l'))   # Fill Qty
                is_maker = data.get('m', False) # Maker Flag
                fee = float(data.get('n', 0))   # Fee Amount
                status = data.get('X')

                logger.info(f"⚡ 成交: {side} {qty} @ {price} | Maker: {is_maker} | Status: {status}")
                
                # 更新统计数据
                self.stats['trade_count'] += 1
                quote_val = price * qty
                self.stats['total_quote_vol'] += quote_val
                self.stats['total_fee'] += fee
                
                # --- 买入逻辑 (Bid) ---
                if side == "Bid":
                    # 更新统计
                    self.stats['total_buy_qty'] += qty
                    if is_maker: self.stats['maker_buy_qty'] += qty
                    else: self.stats['taker_buy_qty'] += qty
                    
                    # 累加持仓
                    if self.held_qty > 0:
                        total_val = (self.held_qty * self.avg_cost) + (qty * price)
                        self.held_qty += qty
                        self.avg_cost = total_val / self.held_qty
                    else:
                        self.held_qty = qty
                        self.avg_cost = price
                        self.hold_start_time = time.time()

                    self.state = "SELLING"
                    
                    # 截断式处理：防止幽灵买单
                    if self.active_order_id and self.active_order_side == 'Bid':
                        # 只有在非完全成交时才撤单
                        if status == 'Filled':
                            logger.info("✅ 买单完全成交，准备卖出")
                            self.active_order_id = None
                            self.active_order_side = None
                        else:
                            # 确实是部分成交，执行截断策略（防止剩余部分在高位成交）
                            logger.info("✂️ 部分成交 -> 撤销剩余买单以锁定仓位")
                            self.cancel_all()

                # --- 卖出逻辑 (Ask) ---
                elif side == "Ask":
                    # 更新统计
                    self.stats['total_sell_qty'] += qty
                    if is_maker: self.stats['maker_sell_qty'] += qty
                    else: self.stats['taker_sell_qty'] += qty
                    
                    # 计算盈亏 (Gross PnL)
                    trade_pnl = (price - self.avg_cost) * qty
                    self.stats['total_pnl'] += trade_pnl
                    
                    # 扣减持仓
                    self.held_qty -= qty
                    if self.held_qty < 0: self.held_qty = 0

                    logger.info(f"💰 卖出反馈 (PnL: {trade_pnl:.4f}) | 剩余持仓: {self.held_qty:.4f}")

                    if status == 'Filled':    
                        # 全部卖完
                        self.state = "IDLE"
                        self.active_order_id = None
                        self.active_order_side = None
                        self.held_qty = 0
                        
                        # ============连续止损冷却机制 =================
                        if trade_pnl < 0:
                            # 记录亏损次数
                            self.consecutive_loss_count += 1
                            logger.warning(f"📉 本次交易亏损，当前连续亏损次数: {self.consecutive_loss_count}")
                            
                            # 检查是否达到连续2次
                            if self.consecutive_loss_count >= 2:
                                self.last_cool_down = time.time()
                                logger.warning(f"🛑 连续止损达标(2次)，触发冷却 {self.cfg.COOL_DOWN}s")
                                # 触发冷却后重置计数，准备下个周期
                                self.consecutive_loss_count = 0 
                        else:
                            # 如果本次是盈利的，直接打断连续亏损记录，重置为0
                            if self.consecutive_loss_count > 0:
                                logger.info("✅ 本次交易盈利，连续亏损计数已重置")
                            self.consecutive_loss_count = 0
                            
                        # 卖出结束时打印完整统计
                        self._print_stats()
                    else:
                        logger.info(f"⏳ 部分卖出，剩余 {self.held_qty:.4f} 等待成交...")
        except Exception as e:
            logger.error(f"Order Update Error: {e}")

    def _print_stats(self):
        """打印详细的统计报表"""
        now = time.time()
        duration = now - self.start_time
        
        # 计算净利润 (盈亏 - 手续费)
        net_pnl = self.stats['total_pnl'] - self.stats['total_fee']
        
        # 计算磨损率 (净盈亏 / 总成交额)
        wear_rate = 0.0
        if self.stats['total_quote_vol'] > 0:
            wear_rate = (net_pnl / self.stats['total_quote_vol']) * 100
            
        # 计算 Maker 占比
        total_vol = self.stats['total_buy_qty'] + self.stats['total_sell_qty']
        maker_vol = self.stats['maker_buy_qty'] + self.stats['maker_sell_qty']
        maker_ratio = (maker_vol / total_vol * 100) if total_vol > 0 else 0
        
        run_time_str = str(timedelta(seconds=int(duration)))

        # 获取东八区时间 (UTC时间 + 8小时)
        beijing_now = datetime.utcnow() + timedelta(hours=8)
        current_time_str = beijing_now.strftime('%m-%d %H:%M:%S')
        
        msg = (
            f"\n{'='*3} {self.symbol} 统计汇总 {'='*3}\n"
            f"运行时间: {run_time_str}\n"
            f"总成交量: {total_vol:.4f} (买 {self.stats['total_buy_qty']:.4f} | 卖 {self.stats['total_sell_qty']:.4f})\n"
            f"总成交额: {self.stats['total_quote_vol']:.2f} USDC\n"
            f"Maker总量: {maker_vol:.4f} ({maker_ratio:.1f}%)\n"
            f"Taker总量: {(total_vol - maker_vol):.4f}\n"
            f"----------------------------------------\n"
            f"累计毛利: {self.stats['total_pnl']:.4f} USDC\n"
            f"累计手续费: {self.stats['total_fee']:.4f} USDC\n"
            f"净利润:   {net_pnl:.4f} USDC\n"
            f"磨损率:   {wear_rate:.5f}%\n"
            f"{'='*5} {current_time_str} (UTC+8) {'='*3} \n"
        )
        logger.info(msg)

    def cancel_all(self):
        """撤销所有订单并重置跟踪 ID"""
        if self.active_order_id:
            try:
                self.rest.cancel_open_orders(self.symbol)
            except Exception as e:
                logger.error(f"撤单失败: {e}")
        self.active_order_id = None
        self.active_order_side = None
        
    def _place_market_order(self, side, qty):
        """执行市价单"""
        # 按照步长修正数量精度
        qty = floor_to(qty, self.base_precision)
        if qty < self.min_qty: 
            return

        logger.info(f"🧹 执行市价清仓 [{side}]: {qty}")
        order_data = {
            "symbol": self.symbol,
            "side": side,
            "orderType": "Market", # 市价单
            "quantity": str(qty)
        }
        # 注意：市价单不能使用 postOnly
        self.rest.execute_order(order_data)

    def clear_open_positions(self):
        """识别现货或合约并清空所有持仓"""
        logger.info("检查并清理现有持仓...")
        try:
            # --- 合约 (PERP) 清仓逻辑 ---
            if "PERP" in self.symbol:
                # [修改] 调用更新后的 get_positions，传入 symbol
                positions = self.rest.get_positions(self.symbol)
                
                if isinstance(positions, list):
                    for pos in positions:
                        # 再次确认 symbol (双重保险)
                        if pos.get('symbol') == self.symbol:
                            net_qty = float(pos.get('netQuantity', 0))
                            if abs(net_qty) > self.min_qty:
                                side = "Ask" if net_qty > 0 else "Bid"
                                logger.info(f"🔍 发现持仓 {net_qty}，执行市价平仓...")
                                self._place_market_order(side, abs(net_qty))
                            else:
                                logger.info(f"当前无 {self.symbol} 持仓 (NetQty={net_qty})")
                else:
                    # 如果返回的不是列表且不是空列表（404已处理为空列表），打印错误
                    if positions: 
                        logger.error(f"获取持仓异常: {positions}")

            # --- 现货 (Spot) 清仓逻辑 ---
            else:
                # ... (现货逻辑保持不变)
                base_asset = self.symbol.split('_')[0]
                balances = self.rest.get_balance()
                
                if base_asset in balances:
                    data = balances[base_asset]
                    available = float(data['available']) if isinstance(data, dict) else float(data)
                    
                    if available > self.min_qty:
                        self._place_market_order("Ask", available)

        except Exception as e:
            logger.error(f"清仓失败 (非致命错误): {e}")

    def run(self):
        self.init_market_info()
        self.ws.connect()
        self.running = True
        
        self.cancel_all()
        self.clear_open_positions() #  市价清仓

        # [新增] 等待清仓订单的成交回报处理完毕，避免计入统计
        logger.info("等待清仓完成...")
        time.sleep(2)
        # [新增] 标记策略正式激活，开始记录统计
        self.strategy_active = True
        
        logger.info(f"策略启动: {self.symbol} | 资金利用比例: {self.cfg.BALANCE_PCT} | 止损: {self.cfg.STOP_LOSS_PCT*100}%")

        while self.running:
            time.sleep(0.5)
            
            # 1. 冷却
            if time.time() - self.last_cool_down < self.cfg.COOL_DOWN:
                continue

            # 2. 行情
            bid = self.ws.best_bid
            ask = self.ws.best_ask
            if bid == 0 or ask == 0: continue

            # 3. 状态机
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
            self.active_order_side = side
            # [新增] 记录挂单时间
            self.active_order_time = time.time()
            logger.info(f"挂单成功 [{side}]: {qty} @ {price}")
            return res["id"]
        else:
            logger.error(f"下单失败: {res}")
            return None

    def _logic_buy(self, best_bid, best_ask):
        if self.active_order_id: return

        usdc_available = self.get_usdc_balance()
        if usdc_available <= 0: return
        
        qty = (usdc_available * self.cfg.BALANCE_PCT * self.cfg.LEVERAGE) / best_bid
        
        # [修复] 只有下单成功才切换状态
        order_id = self._place_order("Bid", best_bid, qty, post_only=True)
        if order_id:
            self.state = "BUYING"

    def _logic_chase_buy(self, best_bid):
        if not self.active_order_id: 
            self.state = "IDLE"
            return
        # 1. 计算挂单存活时间
        order_duration = time.time() - self.active_order_time
        
        # 2. 计算触发价格阈值 (当前挂单价 + 3个最小跳动单位)
        chase_threshold = self.active_order_price + (4 * self.tick_size)
        
        # 3. 判断核心逻辑：同时满足 [时间超过5秒] 且 [价格偏离超过5tick]
        if (order_duration > 10) and (best_bid > chase_threshold):
            logger.info(f"🚀 追涨触发: 挂单已持续 {order_duration:.1f}s 且 市场价{best_bid} > 阈值{chase_threshold:.5f}")
            self.cancel_all()
            self.state = "IDLE"

    def _logic_sell(self, best_bid, best_ask):
        # 1. 如果没有挂单
        if not self.active_order_id:
            if self.avg_cost == 0: self.avg_cost = best_bid
            if self.held_qty < self.min_qty: 
                self.state = "IDLE"
                return

            duration = time.time() - self.hold_start_time
            pnl_pct = (best_bid - self.avg_cost) / self.avg_cost
            
            # 默认：最小利润保护
            min_profit_price = self.avg_cost + self.tick_size
            target_price = max(best_ask, min_profit_price)
            post_only = True
            
            # 止损逻辑
            if pnl_pct < -self.cfg.STOP_LOSS_PCT:
                target_price = best_bid
                post_only = False
                logger.warning(f"🚨 止损 -> Taker")
            elif duration > self.cfg.STOP_LOSS_TIMEOUT:
                target_price = best_ask
                logger.warning(f"⏰ 超时 -> Maker")
                
            self._place_order("Ask", target_price, self.held_qty, post_only=post_only)
        
        # 2. 如果已有挂单
        else:
            if self.active_order_side != 'Ask':
                self.cancel_all()
                return

            # 计算当前浮动盈亏
            current_pnl_pct = (best_bid - self.avg_cost) / self.avg_cost            
            # 检查是否在挂单期间跌破止损线
            if current_pnl_pct < -self.cfg.STOP_LOSS_PCT:
                logger.warning(f"🚨 挂单期间触发价格止损 (PnL: {current_pnl_pct*100:.2f}%) -> 撤单准备止损")
                self.cancel_all()
                return
            
            if (time.time() - self.hold_start_time > self.cfg.STOP_LOSS_TIMEOUT):
                 if abs(self.active_order_price - best_ask) > self.tick_size / 2:
                    logger.info("超时追单调整...")
                    self.cancel_all()
