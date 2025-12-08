# main.py
import time
from config import Config
from core.utils import logger

# 动态导入
from core.strategy import TickScalper
from core.strategy_dual import DualMaker

def main():
    logger.info("Initializing Backpack Bot...")
    try:
        config = Config()
        
        if config.STRATEGY_TYPE == "DUAL_MAKER":
            logger.info(">>> Mode: Dual Maker (Bid2/Ask2 Grid) <<<")
            bot = DualMaker(config)
        else:
            logger.info(">>> Mode: Scalper V1 (DCA/Tick) <<<")
            bot = TickScalper(config)
            
        bot.run()
        
    except KeyboardInterrupt:
        logger.info("Stopping bot...")
        if 'bot' in locals():
            bot.cancel_all()
    except Exception as e:
        logger.error(f"Critical Startup Error: {e}")
        time.sleep(5)

if __name__ == "__main__":
    main()
