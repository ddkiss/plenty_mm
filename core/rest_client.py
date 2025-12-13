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
        # [配置] API 重试机制
        # ==========================================
        retries = Retry(
            total=3, 
            backoff_factor=0.3,
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=["GET"],
            raise_on_status=False
        )
        
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
            if method == "GET":
                # timeout=(连接超时, 读取超时)
                resp = self.session.get(url, headers=headers, params=params, timeout=(3.05, 5))
            else:
                resp = self.session.post(url, headers=headers, json=data, timeout=(3.05, 5)) if method == "POST" else \
                       self.session.delete(url, headers=headers, json=data, timeout=(3.05, 5))
            
            if resp.status_code == 200:
                return resp.json()
            else:
                # === [补回] 特殊处理：如果是 404 且是查询持仓，直接忽略，不打印警告 ===
                if resp.status_code == 404 and "position" in endpoint:
                    return {"error": resp.text} 
                # ==============================================================

                # 其他错误才打印日志
                logger.warning(f"API Error [{resp.status_code}] {endpoint}: {resp.text[:100]}")
                return {"error": resp.text}
                
        except Exception as e:
            logger.error(f"Request Exception ({endpoint}): {str(e)}")
            return {"error": str(e)}

    # 以下方法保持不变
    def get_balance(self):
        return self._request("GET", "/api/v1/capital", "balanceQuery")

    def get_collateral(self):
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
            resp = self.session.get(url, params=params, timeout=2)
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
    
    # === [新增] 获取现货借贷持仓 ===
    def get_borrow_lend_positions(self):
        """获取现货杠杆持仓 (Unified Spot)"""
        return self._request("GET", "/api/v1/borrowLend/positions", "borrowLendPositionQuery")
        
    def get_positions(self, symbol=None):
        params = {}
        if symbol: params["symbol"] = symbol
        res = self._request("GET", "/api/v1/position", "positionQuery", params=params)
        
        # 兼容处理
        if isinstance(res, dict) and "error" in res:
            # 这里的 404 已经被 _request 静默处理了，会返回包含 error 的 dict
            # 我们直接返回空列表，让策略认为无持仓
            if "404" in str(res.get("code", "")) or "not found" in str(res.get("message", "")).lower():
                return []
            return res
            
        if isinstance(res, dict) and "symbol" in res:
            return [res]
        return res
