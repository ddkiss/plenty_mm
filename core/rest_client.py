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

    def execute_order(self, order_data):
        return self._request("POST", "/api/v1/order", "orderExecute", data=order_data)

    def cancel_open_orders(self, symbol):
        return self._request("DELETE", "/api/v1/orders", "orderCancelAll", data={"symbol": symbol})

    def get_open_orders(self, symbol):
        return self._request("GET", "/api/v1/orders", "orderQueryAll", params={"symbol": symbol})
    
    def get_positions(self):
        return self._request("GET", "/api/v1/positions", "positionQueryAll")
