import MetaTrader5 as mt5
import time
import logging
from datetime import datetime

import config
from funded_rules_100k import FundedAccountRules100k
from strategy import check_signal
from trading_engine import check_open_positions, place_order

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# User Configurable Settings (Defaults)
SYMBOL = "BTCUSD"          # Default pair to trade
TIMEFRAME = mt5.TIMEFRAME_M15 # Default timeframe (15 minutes)
SLEEP_INTERVAL = 60        # How often to check for signals (in seconds)

def run_bot():
    """Main loop for the $100K Funded Account Crossover Bot"""
    
    # 1. Initialize MT5
    if not mt5.initialize():
        logger.error(f"MT5 initialization failed: {mt5.last_error()}")
        return

    logger.info(f"MT5 initialized successfully. Started $100K Crossover Bot on {SYMBOL} ({TIMEFRAME})")
    
    # 2. Instantiate rules checker
    rules_checker = FundedAccountRules100k()
    
    # Override config RISK_PERCENT to use the strict 3% max risk rule
    config.RISK_PERCENT = rules_checker.MAX_RISK_PERCENT
    logger.info(f"Risk configured to strict max {config.RISK_PERCENT}% per trade.")

    try:
        while True:
            logger.info("--- New Check Cycle ---")
            
            # 3. Check Funded Rules Limits
            status = rules_checker.check_all_rules()
            
            if not status["can_trade"]:
                logger.warning("TRADING HALTED by Rules Engine. Waiting 5 minutes before next check...")
                time.sleep(300) # Sleep longer if we're halted
                continue
                
            if status["profit_target_reached"]:
                logger.info("Profit Target Reached! Bot will stand down and not take new trades.")
                # We could exit here, but we might want to just hold until withdrawal.
                time.sleep(3600)
                continue
                
            # 4. Check if we already have open positions for this symbol
            if check_open_positions(SYMBOL):
                logger.info(f"Open position exists for {SYMBOL}. Waiting for it to close...")
                time.sleep(SLEEP_INTERVAL)
                continue

            # 5. Check for Crossover Signals
            signal, atr = check_signal(SYMBOL, TIMEFRAME)
            
            if signal == 'BUY':
                logger.info(f"🟢 BUY SIGNAL detected for {SYMBOL}!")
                place_order(SYMBOL, mt5.ORDER_TYPE_BUY, atr)
            elif signal == 'SELL':
                logger.info(f"🔴 SELL SIGNAL detected for {SYMBOL}!")
                place_order(SYMBOL, mt5.ORDER_TYPE_SELL, atr)
            else:
                logger.info(f"No actionable signal for {SYMBOL}.")
                
            # 6. Sleep until next check
            time.sleep(SLEEP_INTERVAL)

    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")
    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}")
    finally:
        mt5.shutdown()
        logger.info("MT5 connection closed.")

if __name__ == "__main__":
    run_bot()
