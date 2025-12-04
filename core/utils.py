import logging
import sys
import math
import base64
import nacl.signing
import time

# --- Logger Setup ---
def setup_logger(name="scalper"):
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    
    # Console Handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(formatter)
    logger.addHandler(ch)
    
    # File Handler (Optional)
    fh = logging.FileHandler("scalper.log", encoding='utf-8')
    fh.setFormatter(formatter)
    logger.addHandler(fh)
    return logger

logger = setup_logger()

# --- Auth/Signature ---
def create_signature(secret_key: str, instruction: str, params: dict = None, timestamp: str = None, window: str = "5000") -> str:
    try:
        if params:
            sorted_params = sorted(params.items())
            query_string = "&".join([f"{k}={v}" for k, v in sorted_params])
            message = f"instruction={instruction}&{query_string}&timestamp={timestamp}&window={window}"
        else:
            message = f"instruction={instruction}&timestamp={timestamp}&window={window}"
            
        decoded_key = base64.b64decode(secret_key)
        signing_key = nacl.signing.SigningKey(decoded_key)
        signature = signing_key.sign(message.encode('utf-8')).signature
        return base64.b64encode(signature).decode('utf-8')
    except Exception as e:
        logger.error(f"签名生成失败: {e}")
        return None

# --- Math Helpers ---
def round_to_step(value: float, step: float) -> float:
    if step <= 0: return value
    step_val = float(step)
    val = round(value / step_val) * step_val
    precision = 0
    if '.' in str(step_val):
        precision = len(str(step_val).split('.')[1].rstrip('0'))
    return round(val, precision)

def floor_to(value: float, precision: int) -> float:
    factor = 10 ** precision
    return math.floor(value * factor) / factor