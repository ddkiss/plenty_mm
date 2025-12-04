import time
import threading
from datetime import timedelta
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

    def get_usdc_balance(self):
        """获取用于交易的可用余额"""
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
        
        return 0.0

    def on_order_update(self, data):
        """ WebSocket 回调: 核心状态管理 """
        event = data.get('e')
        if event == 'orderFill':
            side = data.get('S') # Bid/Ask
            price = float(data.get('L')) # Fill Price
            qty = float(data.get('l'))   # Fill Qty
            is_maker = data.get('m', False) # Maker Flag
            fee = float(data.get('n', 0))   # Fee Amount
            
            logger.info(f"⚡ 成交: {side} {qty} @ {price} | Maker: {is_maker}")
            
            # --- 更新统计数据 ---
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
                    logger.info("部分成交 -> 撤销剩余买单以锁定仓位")
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

                if self.held_qty < self.min_qty:
                    # 全部卖完
                    self.state = "IDLE"
                    self.active_order_id = None
                    self.active_order_side = None
                    self.held_qty = 0
                    
                    if trade_pnl < 0:
                        self.last_cool_down = time.time()
                        logger.warning(f"🛑 亏损冷却 {self.cfg.COOL_DOWN}s")
                        
                    # [打印触发点] 卖出结束时打印完整统计
                    self._print_stats()
                else:
                    logger.info(f"⏳ 部分卖出，剩余 {self.held_qty:.4f} 等待成交...")

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
        
        msg = (
            f"\n{'='*15} 统计汇总 {'='*15}\n"
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
            f"{'='*38}\n"
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

    def run(self):
        self.init_market_info()
        self.ws.connect()
        self.running = True
        
        self.cancel_all()
        logger.info(f"策略启动: {self.symbol} | 余额比例: {self.cfg.BALANCE_PCT} | 止损: {self.cfg.STOP_LOSS_PCT*100}%")

        while self.running:
            time.sleep(0.5)
            
            # [已移除] 定时打印逻辑
            # if time.time() - self.last_stats_print > 300: ...
            
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
        self._place_order("Bid", best_bid, qty, post_only=True)
        self.state = "BUYING"

    def _logic_chase_buy(self, best_bid):
        if not self.active_order_id: 
            self.state = "IDLE"
            return
            
        if best_bid > self.active_order_price * (1 + 0.0001):
            logger.info(f"🚀 追涨: 市场 {best_bid} > 挂单 {self.active_order_price}")
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

            if (time.time() - self.hold_start_time > self.cfg.STOP_LOSS_TIMEOUT):
                 if abs(self.active_order_price - best_ask) > self.tick_size / 2:
                    logger.info("超时追单调整...")
                    self.cancel_all()