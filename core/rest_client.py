import time
import requests
import json
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from .utils import create_signature, logger

class BackpackREST:
    def __init__(self, api_key, secret_key, base_url="https://api.backpack.exchange"):
        self.api_key = api_key
        self.secret_key = secret_key
        self.base_url = base_url
        self.session = requests.Session()
        
        # ==========================================
        # [新增] API 重试机制配置
        # ==========================================
        # total=3: 遇到特定错误最多重试3次
        # backoff_factor=0.3: 重试间隔 (0.3s, 0.6s, 1.2s...)
        # status_forcelist: 遇到 5xx 服务器错误时重试
        # allowed_methods: 仅允许 GET 请求重试 (防止 POST 下单重试导致重复成交)
        # ==========================================
        retries = Retry(
            total=3, 
            backoff_factor=0.3,
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=["GET"],
            raise_on_status=False
        )
        
        # 将重试策略挂载到 https:// 开头的请求
        adapter = HTTPAdapter(max_retries=retries)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def _request(self, method, endpoint, instruction, params=None, data=None):
        url = f"{self.base_url}{endpoint}"
        timestamp = str(int(time.time() * 1000))
        window = "5000"
        
        headers = {
            "Content-Type": "application/json",
            "X-API-KEY": self.api_key,
            "X-TIMESTAMP": timestamp,
            "X-WINDOW": window
        }

        # Sign
        signature_params = params.copy() if params else {}
        if data:
            for k, v in data.items():
                signature_params[k] = str(v).lower() if isinstance(v, bool) else str(v)
        
        signature = create_signature(self.secret_key, instruction, signature_params, timestamp, window)
        if signature:
            headers["X-SIGNATURE"] = signature
        
        try:
            # timeout=(3.05, 5) 表示连接超时3.05秒，读取超时5秒
            # 配合上面的 Retry，如果读取超时或连接失败，会自动重试3次
            if method == "GET":
                resp = self.session.get(url, headers=headers, params=params, timeout=(3.05, 5))
            else:
                # POST/DELETE 不自动重试，避免副作用
                resp = self.session.post(url, headers=headers, json=data, timeout=(3.05, 5)) if method == "POST" else \
                       self.session.delete(url, headers=headers, json=data, timeout=(3.05, 5))
            
            if resp.status_code == 200:
                return resp.json()
            else:
                # 降低日志级别，避免偶尔的 502 刷屏（因为 requests 内部可能已经重试过了）
                logger.warning(f"API Error [{resp.status_code}] {endpoint}: {resp.text[:100]}")
                return {"error": resp.text}
                
        except Exception as e:
            # 只有当重试耗尽(MaxRetryError)或发生其他异常时才会走到这里
            logger.error(f"Request Exception ({endpoint}): {str(e)}")
            return {"error": str(e)}

    # 以下方法保持不变，直接复用
    def get_balance(self):
        """获取现货余额"""
        return self._request("GET", "/api/v1/capital", "balanceQuery")

    def get_collateral(self):
        """获取合约抵押品余额"""
        return self._request("GET", "/api/v1/capital/collateral", "collateralQuery")

    def get_markets(self):
        try:
            return self.session.get(f"{self.base_url}/api/v1/markets", timeout=5).json()
        except:
            return []

    def get_depth(self, symbol, limit=5):
        try:
            url = f"{self.base_url}/api/v1/depth"
            params = {"symbol": symbol, "limit": str(limit)}
            resp = self.session.get(url, params=params, timeout=2) # 深度接口不需要签名，但也享受 Session 的连接池和重试
            if resp.status_code == 200:
                return resp.json()
            return None
        except Exception as e:
            logger.error(f"获取深度网络异常: {e}")
            return None

    def execute_order(self, order_data):
        return self._request("POST", "/api/v1/order", "orderExecute", data=order_data)

    def cancel_open_orders(self, symbol):
        return self._request("DELETE", "/api/v1/orders", "orderCancelAll", data={"symbol": symbol})

    def get_open_orders(self, symbol):
        return self._request("GET", "/api/v1/orders", "orderQueryAll", params={"symbol": symbol})
    
    def get_positions(self, symbol=None):
        params = {}
        if symbol: params["symbol"] = symbol
        res = self._request("GET", "/api/v1/position", "positionQuery", params=params)
        
        if isinstance(res, dict) and "error" in res:
            if "404" in str(res["error"]) or "not found" in str(res["error"]).lower():
                return []
            return res
        if isinstance(res, dict) and "symbol" in res:
            return [res]
        return res