import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    API_KEY = os.getenv("API_KEY")
    SECRET_KEY = os.getenv("SECRET_KEY")
    SYMBOL = os.getenv("SYMBOL", "SOL_USDC")
    
    # Base URLs
    REST_URL = "https://api.backpack.exchange"
    WS_URL = "wss://ws.backpack.exchange"
    
    # Strategy Settings
    LEVERAGE = float(os.getenv("LEVERAGE", "1.0"))
    BALANCE_PCT = float(os.getenv("BALANCE_PCT", "0.8"))
    STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "0.01"))
    COOL_DOWN = int(os.getenv("COOL_DOWN", "30"))

    if not API_KEY or not SECRET_KEY:
        raise ValueError("请在 .env 文件中配置 API_KEY 和 SECRET_KEY")