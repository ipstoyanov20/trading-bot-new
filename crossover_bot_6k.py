import MetaTrader5 as mt5
import time
import logging
from datetime import datetime

import config
from funded_rules_6k import FundedAccountRules6k
from strategy import check_signal
from trading_engine import check_open_positions, place_order

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# User Configurable Settings (Defaults)
SYMBOL = "XAUUSD"          # Default pair to trade
TIMEFRAME = mt5.TIMEFRAME_M15 # Default timeframe (15 minutes)
SLEEP_INTERVAL = 60        # How often to check for signals (in seconds)

def run_bot():
    """Main loop for the $6K Funded Account Crossover Bot"""
    
    # 1. Initialize MT5
    if not mt5.initialize():
        logger.error(f"MT5 initialization failed: {mt5.last_error()}")
        return

    logger.info(f"MT5 initialized successfully. Started $6K Crossover Bot on {SYMBOL} ({TIMEFRAME})")
    
    # 2. Instantiate rules checker
    rules_checker = FundedAccountRules6k()
    
    # Override config RISK_PERCENT to use the strict 3% max risk rule
    config.RISK_PERCENT = rules_checker.MAX_RISK_PERCENT
    logger.info(f"Risk configured to strict max {config.RISK_PERCENT}% per trade.")

    # --- TEST ORDER ---
    logger.info("--- ATTEMPTING IMMEDIATE TEST ORDER ---")
    try:
        test_result = place_order(SYMBOL, mt5.ORDER_TYPE_BUY, atr=2.0)
        logger.info(f"Test order result object: {test_result}")
        if test_result is None:
            err = mt5.last_error()
            logger.error(f"❌ Test order failed to send! MT5 Last Error Code: {err}")
            if err[0] == 4756:
                logger.error("Error 4756: Trade request sending failed (Check if Algo Trading is ON or Market is open)")
            elif err[0] == 10015:
                logger.error("Error 10015: Invalid price / Invalid Stops (SL/TP too close or wrong format)")
            elif err[0] == 10014:
                logger.error("Error 10014: Invalid volume (Check lot size limits)")
            elif err[0] == 10016:
                logger.error("Error 10016: Invalid Stops (SL or TP distance is too small)")
        else:
            logger.info("✅ Test order executed successfully.")
    except Exception as e:
        logger.error(f"❌ A Python error occurred during the test order: {e}", exc_info=True)
    logger.info("---------------------------------------")

    try:
        while True:
            logger.info("--- New Check Cycle ---")
            
            # 3. Check Funded Rules Limits
            status = rules_checker.check_all_rules()
            
            if not status["can_trade"]:
                logger.warning("TRADING HALTED by Rules Engine... BUT IGNORED FOR TESTING. Proceeding with trade.")
                # time.sleep(300) # Sleep longer if we're halted
                # continue
                
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
