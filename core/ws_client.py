import websocket
import threading
import time
import json
from .utils import logger, create_signature

class BackpackWS:
    def __init__(self, api_key, secret_key, symbol, on_update_callback, ws_url="wss://ws.backpack.exchange"):
        self.ws_url = ws_url
        self.api_key = api_key
        self.secret_key = secret_key
        self.symbol = symbol
        self.callback = on_update_callback
        self.ws = None
        self.running = False
        
        # BBO (Best Bid/Offer) - 策略核心数据
        self.best_bid = 0.0
        self.best_ask = 0.0
        
    def connect(self):
        """启动 WebSocket 连接"""
        self.running = True
        # 禁用 trace 以减少日志噪音
        websocket.enableTrace(False)
        
        self.ws = websocket.WebSocketApp(
            self.ws_url,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close
        )
        
        # 在独立线程中运行，避免阻塞主策略循环
        t = threading.Thread(target=self.ws.run_forever)
        t.daemon = True
        t.start()
        
        # 等待连接建立（简单的阻塞检查）
        timeout = 0
        while not self.ws.sock or not self.ws.sock.connected:
            time.sleep(0.1)
            timeout += 0.1
            if timeout > 10:
                logger.error("WebSocket 连接超时，请检查网络")
                break

    def _on_open(self, ws):
        logger.info("WebSocket 已连接")
        
        # 1. 订阅 bookTicker (Tick Scalper 核心优化)
        # 直接获取 BBO，比维护 Depth Diff 更快、更轻量
        ws.send(json.dumps({
            "method": "SUBSCRIBE", 
            "params": [f"bookTicker.{self.symbol}"]
        }))
        logger.info(f"已订阅行情: bookTicker.{self.symbol}")
        
        # 2. 订阅私有订单更新流 (用于捕捉成交)
        timestamp = str(int(time.time() * 1000))
        window = "5000"
        
        # 生成签名
        # 注意：Backpack WS 订阅 instruction 固定为 "subscribe"
        signature = create_signature(self.secret_key, "subscribe", {}, timestamp, window)
        
        if signature:
            ws.send(json.dumps({
                "method": "SUBSCRIBE", 
                "params": [f"account.orderUpdate.{self.symbol}"],
                "signature": [self.api_key, signature, timestamp, window]
            }))
            logger.info("已订阅私有订单流")
        else:
            logger.error("签名生成失败，无法订阅私有流")

    def _on_message(self, ws, message):
        try:
            data = json.loads(message)
            stream = data.get("stream", "")
            payload = data.get("data", {})
            
            # [优化] 处理 bookTicker 推送
            if stream.startswith("bookTicker"):
                # 直接更新最优买卖价，无需复杂计算
                self.best_bid = float(payload.get('b', 0))
                self.best_ask = float(payload.get('a', 0))
                
            # 处理订单/成交更新
            elif stream.startswith("account.orderUpdate"):
                self.callback(payload)
                
        except Exception as e:
            logger.error(f"WS 消息处理错误: {e}")

    def _on_error(self, ws, error):
        logger.error(f"WebSocket 错误: {error}")

    def _on_close(self, ws, status_code, msg):
        logger.warning(f"WebSocket 连接断开: {msg}")
        self.best_bid = 0.0
        self.best_ask = 0.0
        
        # 简单的自动重连机制
        if self.running:
            logger.info("3秒后尝试重连...")
            time.sleep(3)
            self.connect()
            
    def close(self):
        """主动关闭连接"""
        self.running = False
        if self.ws:
            self.ws.close()