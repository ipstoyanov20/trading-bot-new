import MetaTrader5 as mt5
import time
import logging
from datetime import datetime

import config
from funded_rules_100k import FundedAccountRules100k
from strategy import check_signal
from trading_engine import get_open_positions_count, get_portfolio_pnl, close_all_positions, place_order

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# User Configurable Settings
SYMBOL = "XAUUSD"          
TIMEFRAME = mt5.TIMEFRAME_M5  # Using 5m timeframe for momentum as requested
TRADE_CYCLE_SECONDS = 900  # 15 minutes between signal checks
MONITOR_INTERVAL = 5       # 5 seconds for continuous PnL monitoring

def run_bot():
    """Main loop for the $100K Funded Account Batch Bot"""
    if not mt5.initialize():
        logger.error(f"MT5 initialization failed: {mt5.last_error()}")
        return

    logger.info(f"MT5 initialized successfully. Started $100K Batch Bot on {SYMBOL}")
    rules_checker = FundedAccountRules100k()
    
    last_trade_time = 0
    max_pnl = 0.0

    try:
        while True:
            # 1. Continuous Portfolio Monitoring
            open_count = get_open_positions_count(SYMBOL)
            
            if open_count > 0:
                pnl = get_portfolio_pnl(SYMBOL)
                
                if pnl > max_pnl:
                    max_pnl = pnl

                if pnl >= 10.0:
                    logger.info(f"💰 Double-digit profit reached! Total PnL: ${pnl:.2f}. Closing all {open_count} positions.")
                    close_all_positions(SYMBOL)
                    max_pnl = 0.0
                elif max_pnl >= 2.0 and pnl <= max_pnl * 0.5:
                    logger.info(f"📉 Profit dropping (Trailing Stop)! Max was ${max_pnl:.2f}, now ${pnl:.2f}. Closing to secure profit.")
                    close_all_positions(SYMBOL)
                    max_pnl = 0.0
                elif max_pnl >= 0.5 and pnl <= 0.1:
                    logger.info(f"⚠️ Profit vanished! Closing before loss. Max was ${max_pnl:.2f}, now ${pnl:.2f}.")
                    close_all_positions(SYMBOL)
                    max_pnl = 0.0
            else:
                max_pnl = 0.0
            
            # 2. 15-Minute Trading Cycle
            current_time = time.time()
            if current_time - last_trade_time >= TRADE_CYCLE_SECONDS:
                logger.info("--- New 15-Minute Cycle Check ---")
                
                # Check Funded Rules Limits
                status = rules_checker.check_all_rules()
                if not status["can_trade"]:
                    logger.warning("TRADING HALTED by Rules Engine. Waiting 5 minutes...")
                    time.sleep(300)
                    continue
                    
                if status["profit_target_reached"]:
                    logger.info("Profit Target Reached! Bot will stand down.")
                    time.sleep(3600)
                    continue

                open_count = get_open_positions_count(SYMBOL)
                if open_count == 0:  # Only open a new batch if we are flat
                    rates = mt5.copy_rates_from_pos(SYMBOL, TIMEFRAME, 0, 5)
                    if rates is not None and len(rates) > 0:
                        is_bullish = rates[-1]['close'] >= rates[0]['open']
                        order_type = mt5.ORDER_TYPE_BUY if is_bullish else mt5.ORDER_TYPE_SELL
                        action_str = "BUY" if is_bullish else "SELL"
                        
                        logger.info(f"🟢 ZERO OPEN POSITIONS! Brief history trend is {action_str}. Opening {config.MAX_BATCH_POSITIONS} {action_str} positions.")
                        for _ in range(config.MAX_BATCH_POSITIONS):
                            place_order(SYMBOL, order_type, atr=None)
                    else:
                        logger.warning("Failed to fetch rates for brief history check.")
                else:
                    logger.info(f"Batch currently active ({open_count} positions). Waiting for portfolio to hit profit.")
                    
                last_trade_time = current_time

            # Sleep for continuous monitoring
            time.sleep(MONITOR_INTERVAL)

    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")
    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}")
    finally:
        mt5.shutdown()
        logger.info("MT5 connection closed.")

if __name__ == "__main__":
    run_bot()
