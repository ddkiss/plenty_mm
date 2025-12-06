import time
import threading
from datetime import datetime, timedelta
from .utils import logger, round_to_step, floor_to
from .rest_client import BackpackREST

class TickScalper:
    def __init__(self, config):
        self.cfg = config
        self.symbol = config.SYMBOL
        # 记录挂单产生的时间
        self.active_order_time = 0
        
        # Clients
        self.rest = BackpackREST(config.API_KEY, config.SECRET_KEY)
        
        
        # State
        self.state = "IDLE"  # IDLE, BUYING, SELLING
        # 策略激活状态标记，用于过滤启动时的清仓数据
        self.strategy_active = False
        # 连续亏损计数器
        self.consecutive_loss_count = 0
        # 当前补仓次数计数器
        self.dca_count = 0
        
        # Order Tracking
        self.active_order_id = None
        self.active_order_price = 0.0
        self.active_order_side = None 
        # 标记当前挂单是否为 Maker
        self.active_order_is_maker = False
        
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
        self.current_cool_down_time = 0  # 动态记录当前需要的冷却时长
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
            'taker_quote_vol': 0.0,  # [新增] Taker 总成交额 (USDC)
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

    def _get_real_position(self):
        """[新增] 通过 REST API 获取当前真实的持仓数量"""
        try:
            # 1. 合约逻辑
            if "PERP" in self.symbol:
                positions = self.rest.get_positions(self.symbol)
                if isinstance(positions, list):
                    for p in positions:
                        if p.get('symbol') == self.symbol:
                            return abs(float(p.get('netQuantity', 0)))
                elif isinstance(positions, dict) and positions.get('symbol') == self.symbol:
                    return abs(float(positions.get('netQuantity', 0)))
                return 0.0
            
            # 2. 现货逻辑
            else:
                base_asset = self.symbol.split('_')[0]
                balances = self.rest.get_balance()
                if base_asset in balances:
                    data = balances[base_asset]
                    # 兼容不同格式
                    return float(data.get('available', 0)) if isinstance(data, dict) else float(data)
                return 0.0
        except Exception as e:
            logger.error(f"查询持仓失败: {e}")
            return self.held_qty # 如果查询失败，暂时返回旧值

    def _check_order_via_rest(self):
        """[新增] 使用 REST API 检查当前挂单状态"""
        if not self.active_order_id:
            return

        try:
            # 获取当前所有挂单
            open_orders = self.rest.get_open_orders(self.symbol)
            
            # 检查我们的 active_order_id 是否在挂单列表中
            is_open = False
            if isinstance(open_orders, list):
                for o in open_orders:
                    if str(o.get('id')) == str(self.active_order_id):
                        is_open = True
                        break
            
            if is_open:
                # 订单还在挂着
                pass
            else:
                # 订单不见了！说明要么成交了，要么被取消了
                logger.info(f"🔍 订单 {self.active_order_id} 已不在挂单列表，更新状态...")
                
                # 1. 立即同步真实持仓
                real_qty = self._get_real_position()

                #  统一计算成交数据
                filled_qty = abs(real_qty - self.held_qty)
                
                if filled_qty > 0:
                    trade_val = filled_qty * self.active_order_price # 成交额
                    
                    # --- [修改开始] 完善统计逻辑 ---
                    self.stats['total_quote_vol'] += trade_val
                    
                    if self.active_order_side == 'Bid':
                        self.stats['total_buy_qty'] += filled_qty
                        if self.active_order_is_maker:
                            self.stats['maker_buy_qty'] += filled_qty
                        else:
                            self.stats['taker_buy_qty'] += filled_qty
                    else:
                        self.stats['total_sell_qty'] += filled_qty
                        if self.active_order_is_maker:
                            self.stats['maker_sell_qty'] += filled_qty
                        else:
                            self.stats['taker_sell_qty'] += filled_qty
                    
                    if not self.active_order_is_maker:
                        self.stats['taker_quote_vol'] += trade_val
                
                # 2. 判断发生了什么
                if self.active_order_side == 'Bid':
                    if real_qty > self.held_qty:
                        logger.info(f"✅ 买单成交 (持仓 {self.held_qty} -> {real_qty})")
                        self.held_qty = real_qty
                        # 简单估算成本
                        self.avg_cost = self.active_order_price 
                        self.hold_start_time = time.time()
                        self.state = "SELLING"
                    else:
                        logger.info("❌ 买单被取消 (持仓未增加)")
                        self.state = "IDLE"

                elif self.active_order_side == 'Ask':
                    if real_qty < self.held_qty:
                        logger.info(f"✅ 卖单成交 (持仓 {self.held_qty} -> {real_qty})")
                        
                        trade_pnl = (self.active_order_price - self.avg_cost) * (self.held_qty - real_qty)
                        self.stats['trade_count'] += 1
                        self.stats['total_pnl'] += trade_pnl
                        
                        # [新增修复] 计算净利润用于止损判断
                        trade_val_sell = self.active_order_price * (self.held_qty - real_qty)
                        # 如果是 Maker 假定0费率，否则使用配置的 Taker 费率
                        fee_rate = 0 if self.active_order_is_maker else self.cfg.TAKER_FEE_RATE
                        net_pnl = trade_pnl - (trade_val_sell * fee_rate)

                        self.held_qty = real_qty
                        if self.held_qty < self.min_qty:
                            self.state = "IDLE"
                            self.held_qty = 0
                            
                            # [修改] 使用净利润 net_pnl 判断是否亏损
                            if net_pnl < 0:
                                self.consecutive_loss_count += 1
                                logger.warning(f"📉 本次净亏损(含费)，连续亏损计数: {self.consecutive_loss_count}")
                            
                            if self.consecutive_loss_count == 1:
                                self.last_cool_down = time.time()
                                self.current_cool_down_time = 5 
                                logger.warning(f"🛑 首次止损，触发短冷却 5s")
                                
                            elif self.consecutive_loss_count >= 2:
                                self.last_cool_down = time.time()
                                self.current_cool_down_time = self.cfg.COOL_DOWN
                                logger.warning(f"🛑 连续止损达标(2次)，触发长冷却 {self.cfg.COOL_DOWN}s")
                                self.consecutive_loss_count = 0 
                            else:
                                if self.consecutive_loss_count > 0:
                                    logger.info("✅ 本次盈利，连续亏损计数重置")
                                self.consecutive_loss_count = 0
                                
                            self._print_stats()

                    else:
                        logger.info("❌ 卖单被取消 (持仓未减少)")
                
                # 3. 清理 ID
                self.active_order_id = None
                self.active_order_side = None

        except Exception as e:
            logger.error(f"REST 检查订单失败: {e}")
            

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

        # [新增] 估算总手续费 (Taker总额 * 费率)
        self.stats['total_fee'] = self.stats['taker_quote_vol'] * self.cfg.TAKER_FEE_RATE
        
        # 计算净利润 (盈亏 - 手续费)
        net_pnl = self.stats['total_pnl'] - self.stats['total_fee']
        
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
                # [新增] 记录撤单前的持仓，用于计算部分成交
                old_qty = self.held_qty
                # 同步余额
                self._sync_position_state()
                #  补算撤单期间产生的成交量
                filled_qty = abs(self.held_qty - old_qty)
                
                if filled_qty > 0:
                    trade_val = filled_qty * self.active_order_price
                    
                    # --- [修改开始] 完善统计逻辑 ---
                    self.stats['total_quote_vol'] += trade_val
                    
                    # 区分买卖方向
                    if self.active_order_side == 'Bid':
                        self.stats['total_buy_qty'] += filled_qty
                        if self.active_order_is_maker:
                            self.stats['maker_buy_qty'] += filled_qty
                        else:
                            self.stats['taker_buy_qty'] += filled_qty
                        # ==========================================
                        # ✅ 这里必须记录买入成本！
                        # ==========================================
                        if self.held_qty > self.min_qty:
                            self.avg_cost = self.active_order_price
                            logger.info(f"✅ 撤买单发现成交，更新持仓成本: {self.avg_cost}")
                        # ==========================================

                    else:
                        # 卖单撤单成交：需要计算盈亏 [修复重点]
                        self.stats['total_sell_qty'] += filled_qty
                        if self.active_order_is_maker:
                            self.stats['maker_sell_qty'] += filled_qty
                        else:
                            self.stats['taker_sell_qty'] += filled_qty
                        
                        # [新增修复] 计算这部分成交的盈亏
                        trade_pnl = (self.active_order_price - self.avg_cost) * filled_qty
                        self.stats['total_pnl'] += trade_pnl
                        
                        # [新增修复] 计算净利并更新连续亏损计数
                        # 估算手续费 (保守按 Taker 算，或者根据 active_order_is_maker 判断)
                        fee_rate = 0 if self.active_order_is_maker else self.cfg.TAKER_FEE_RATE
                        net_pnl = trade_pnl - (trade_val * fee_rate)
                        
                        if net_pnl < 0:
                            self.consecutive_loss_count += 1
                            logger.warning(f"📉 撤单发现亏损成交，连续亏损计数: {self.consecutive_loss_count}")
                        else:
                            self.consecutive_loss_count = 0
                    
                    # 累加 Taker 成交额 (用于算费率)
                    if not self.active_order_is_maker:
                        self.stats['taker_quote_vol'] += trade_val
                    # --- [修改结束] ---
                    
                    logger.info(f"📉 撤单发现部分成交: {filled_qty}")
            except Exception as e:
                logger.error(f"撤单失败: {e}")
        self.active_order_id = None
        self.active_order_side = None

    def _sync_position_state(self):
        """[复用] 强制同步持仓状态，用于撤单后或定期校准"""
        try:
            real_qty = self._get_real_position() # 调用新的通用查询方法
            
            # 只有当数量发生变化时才打印日志，减少刷屏
            if real_qty != self.held_qty:
                logger.info(f"🔄 持仓校准: 本地{self.held_qty} -> 链上{real_qty}")
                self.held_qty = real_qty
                
            # 过滤粉尘
            if self.held_qty < self.min_qty:
                self.held_qty = 0.0
                
        except Exception as e:
            logger.error(f"持仓同步失败: {e}")

    
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
        self.running = True
        
        self.cancel_all()
        # 初始同步一次持仓
        self._sync_position_state() # <--- 这里直接调用同步方法
        
        if self.held_qty > self.min_qty:
            logger.info(f"发现初始持仓: {self.held_qty}，进入卖出模式")
            self.state = "SELLING"
            self.avg_cost = 0.0
            self.hold_start_time = time.time()
            
        self.strategy_active = True
        logger.info(f"策略启动: {self.symbol} | 资金利用比例: {self.cfg.BALANCE_PCT} | 止损: {self.cfg.STOP_LOSS_PCT*100}%")

        while self.running:
            time.sleep(0.5)

            try:
                self._check_order_via_rest()
                
                if time.time() - self.last_cool_down < self.current_cool_down_time:
                    continue

                # 获取深度 (limit=5)
                depth = self.rest.get_depth(self.symbol, limit=5)
                if not depth: continue
                
                # 数据源是字符串列表: [['20.12', '1.5'], ...]
                bids = depth.get("bids", [])
                asks = depth.get("asks", [])

                if not bids or not asks:
                    logger.warning("盘口数据为空")
                    continue
                
                # --- [修正开始] 稳健的 BBO 获取逻辑 ---
                
                # 1. 获取最优买价 (Best Bid): 买单中价格最高的
                # key=lambda x: float(x[0]) 表示按价格数值大小比较
                best_bid_order = max(bids, key=lambda x: float(x[0]))
                best_bid = float(best_bid_order[0])

                # 2. 获取最优卖价 (Best Ask): 卖单中价格最低的
                best_ask_order = min(asks, key=lambda x: float(x[0]))
                best_ask = float(best_ask_order[0])
                
                # --- [修正结束] ---

                # 如果是 SELLING 状态且成本未初始化，用当前买一价初始化
                if self.state == "SELLING" and self.avg_cost == 0:
                    logger.warning(f"⚠️ 警告：检测到无成本持仓 (可能是重启或异常导致)！强制将成本重置为当前 Bid: {best_bid}")
                    self.avg_cost = best_bid

                # 执行策略
                if self.state == "IDLE":
                    self._logic_buy(best_bid, best_ask)
                elif self.state == "BUYING":
                    self._logic_chase_buy(best_bid)
                elif self.state == "SELLING":
                    self._logic_sell(best_bid, best_ask)

            except Exception as e:
                logger.error(f"主循环发生错误: {e}")
                time.sleep(1)

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
            # [新增] 记录这笔单子是不是 Maker
            self.active_order_is_maker = post_only
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
        
        # 2. 计算触发价格阈值 (当前挂单价 + 4个最小跳动单位)
        chase_threshold = self.active_order_price + (4 * self.tick_size)
        
        # 3. 判断核心逻辑：同时满足 [时间超过10秒] 且 [价格偏离超过阈值]
        if (order_duration > 10) and (best_bid > chase_threshold):
            logger.info(f"🚀 追涨触发: 挂单已持续 {order_duration:.1f}s 且 市场价{best_bid} > 阈值{chase_threshold:.5f}")
            self.cancel_all()
            
            # [新增修复] 撤单后检查是否持有仓位
            if self.held_qty > self.min_qty:
                logger.info(f"🔄 追单撤销后持有 {self.held_qty}，转为卖出状态")
                self.state = "SELLING"
                # 如果还没初始化成本，暂时用刚才的挂单价作为成本
                if self.avg_cost == 0:
                    self.avg_cost = self.active_order_price
            else:
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

    # --- [新增] DCA 核心逻辑方法 ---

    def _check_dca_condition(self, current_price):
        """检查是否满足补仓条件"""
        # 1. 基础检查：有挂单、余额不足、成本未初始化则不补
        if self.active_order_id: return False
        if self.avg_cost == 0: return False
        
        # 2. 计算当前跌幅
        drop_pct = (self.avg_cost - current_price) / self.avg_cost
        
        # 3. 判断：跌幅达标 且 次数未用完
        if (drop_pct > self.cfg.DCA_DROP_PCT) and (self.dca_count < self.cfg.MAX_DCA_COUNT):
             # 简单的余额检查 (确保够买至少 1 个最小单位)
             if self.get_usdc_balance() > (self.min_qty * current_price):
                 return True
        return False

    def _logic_dca_buy(self, best_bid):
        """执行补仓下单"""
        # 计算补仓数量：持仓量 * 倍率 (这里简化为按数量倍投)
        # 如果你想按固定金额补仓，可以用 (USDC余额 * PCT) / price
        # 这里演示按持仓倍率补：
        qty = self.held_qty * self.cfg.DCA_MULTIPLIER
        
        # 再次检查余额是否足够，不够就用全部余额
        usdc_balance = self.get_usdc_balance()
        if (qty * best_bid) > usdc_balance:
            qty = usdc_balance / best_bid
            
        qty = floor_to(qty, self.base_precision)
        if qty < self.min_qty:
            logger.warning("余额不足以执行 DCA 补仓")
            return

        logger.info(f"📉 触发第 {self.dca_count + 1} 次补仓: 现价{best_bid} < 成本{self.avg_cost}")
        
        # 下单 (PostOnly=True 尽量挂单，如果急于补仓可以设为 False)
        # 注意：这里我们复用 _place_order，它会更新 active_order_id
        # 下单成功后，我们在 check_order 里处理成交和成本更新
        self._place_order("Bid", best_bid, qty, post_only=True)
