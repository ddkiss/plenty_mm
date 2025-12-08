# config.py
import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    API_KEY = os.getenv("API_KEY")
    SECRET_KEY = os.getenv("SECRET_KEY")
    SYMBOL = os.getenv("SYMBOL", "SOL_USDC_PERP")
    
    # 基础配置
    REST_URL = "https://api.backpack.exchange"
    WS_URL = "wss://ws.backpack.exchange" # 虽然不用，保留配置
    
    # --- 策略选择 ---
    # 可选值: 'SCALPER' (原DCA策略) 或 'DUAL_MAKER' (新双向策略)
    STRATEGY_TYPE = os.getenv("STRATEGY_TYPE", "DUAL_MAKER") 

    # --- 原策略参数 (TickScalper) ---
    BALANCE_PCT = float(os.getenv("BALANCE_PCT", "0.3"))
    STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "0.02"))
    STOP_LOSS_TIMEOUT = int(os.getenv("STOP_LOSS_TIMEOUT", "1800"))
    MAX_DCA_COUNT = int(os.getenv("MAX_DCA_COUNT", "2"))
    DCA_DROP_PCT = float(os.getenv("DCA_DROP_PCT", "0.008"))
    DCA_MULTIPLIER = float(os.getenv("DCA_MULTIPLIER", "1.0"))
    
    # --- [新增] Dual Maker 策略参数 ---
    # 每次挂单金额占总资金的比例 (1/20 = 0.05)
    GRID_ORDER_PCT = float(os.getenv("GRID_ORDER_PCT", "0.05"))
    
    # 资金利用率上限。当 持仓价值 / 总余额 超过此值时，停止双向，进入回本模式
    # 建议设为 0.45 (45%) 左右，留出空间防止爆仓
    MAX_POSITION_PCT = float(os.getenv("MAX_POSITION_PCT", "0.45"))
    
    # 成交后的冷却时间 (秒)
    REBALANCE_WAIT = int(os.getenv("REBALANCE_WAIT", "2"))
    
    # 回本单超时时间 (秒)，超过后强制止损
    BREAKEVEN_TIMEOUT = int(os.getenv("BREAKEVEN_TIMEOUT", "1200"))

    # 通用参数
    LEVERAGE = float(os.getenv("LEVERAGE", "1.0"))
    COOL_DOWN = int(os.getenv("COOL_DOWN", "180"))
    TAKER_FEE_RATE = float(os.getenv("TAKER_FEE_RATE", "0.00018"))

    if not API_KEY or not SECRET_KEY:
        raise ValueError("请在 .env 文件中配置 API_KEY 和 SECRET_KEY")
