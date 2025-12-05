import time
import requests
import json
from .utils import create_signature, logger

class BackpackREST:
    def __init__(self, api_key, secret_key, base_url="https://api.backpack.exchange"):
        self.api_key = api_key
        self.secret_key = secret_key
        self.base_url = base_url
        self.session = requests.Session()

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
                resp = self.session.get(url, headers=headers, params=params, timeout=5)
            else:
                resp = self.session.post(url, headers=headers, json=data, timeout=5) if method == "POST" else \
                       self.session.delete(url, headers=headers, json=data, timeout=5)
            
            if resp.status_code == 200:
                return resp.json()
            else:
                logger.warning(f"API Error [{resp.status_code}]: {resp.text}")
                return {"error": resp.text}
        except Exception as e:
            logger.error(f"Request Exception: {e}")
            return {"error": str(e)}

    def get_balance(self):
        """获取现货余额"""
        return self._request("GET", "/api/v1/capital", "balanceQuery")

    def get_collateral(self):
        """获取合约抵押品余额"""
        return self._request("GET", "/api/v1/capital/collateral", "collateralQuery")

    def get_markets(self):
        return requests.get(f"{self.base_url}/api/v1/markets").json()

    # 获取深度数据
    def get_depth(self, symbol, limit=5):
        """
        获取盘口深度
        limit: 限制返回的档位数量，默认为5 (获取最优买卖价只需看第1档，设为5最节省流量)
        """
        try:
            url = f"{self.base_url}/api/v1/depth"
            params = {
                "symbol": symbol, 
                "limit": str(limit)
            }
            # 深度接口通常是公共的，直接请求即可，不需要签名
            resp = requests.get(url, params=params, timeout=2)
            if resp.status_code == 200:
                return resp.json()
            else:
                logger.warning(f"获取深度失败 [{resp.status_code}]: {resp.text}")
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
        """获取永续合约仓位"""
        params = {}
        if symbol:
            params["symbol"] = symbol

        res = self._request("GET", "/api/v1/position", "positionQuery", params=params)
        
        # 特殊处理 404 错误 - 表示没有仓位，返回空列表
        if isinstance(res, dict) and "error" in res:
            error_msg = str(res["error"])
            # 检查错误信息中是否包含 404 或 not found
            if "404" in error_msg or "not found" in error_msg.lower():
                logger.info(f"仓位查询返回 404，确认无活跃仓位 ({symbol})")
                return []
            # 其他错误原样返回
            return res

        # 兼容性处理：如果 API 返回单个字典对象（非列表），将其包装为列表
        if isinstance(res, dict) and "symbol" in res:
            return [res]
            
        return res
