import time
from config import Config
from core.strategy import TickScalper
from core.utils import logger

def main():
    logger.info("Starting Backpack Tick Scalper V1 (Lite)...")
    try:
        config = Config()
        bot = TickScalper(config)
        bot.run()
    except KeyboardInterrupt:
        logger.info("Stopping bot...")
        if 'bot' in locals():
            bot.cancel_all()
    except Exception as e:
        logger.error(f"Critical Error: {e}")
        time.sleep(5)

if __name__ == "__main__":
    main()