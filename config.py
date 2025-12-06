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
    BALANCE_PCT = float(os.getenv("BALANCE_PCT", "0.3"))
    STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "0.02"))
    STOP_LOSS_TIMEOUT = int(os.getenv("STOP_LOSS_TIMEOUT", "1800"))
    MAX_DCA_COUNT = int(os.getenv("MAX_DCA_COUNT", "2"))
    DCA_DROP_PCT = float(os.getenv("DCA_DROP_PCT", "0.008"))
    DCA_MULTIPLIER = float(os.getenv("DCA_MULTIPLIER", "1.0"))
    LEVERAGE = float(os.getenv("LEVERAGE", "1.0"))
    COOL_DOWN = int(os.getenv("COOL_DOWN", "180"))
    TAKER_FEE_RATE = float(os.getenv("TAKER_FEE_RATE", "0.00018"))

    if not API_KEY or not SECRET_KEY:
        raise ValueError("请在 .env 文件中配置 API_KEY 和 SECRET_KEY")
