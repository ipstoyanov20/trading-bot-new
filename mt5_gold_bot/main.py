import time
import MetaTrader5 as mt5
import config
from logger import log_info, log_error
from strategy import check_signal
from trading_engine import initialize_mt5, check_open_positions, place_order
from trade_tracker import update_trade_history

def run_bot():
    """
    Main loop orchestrating the MT5 Gold Crossover Bot.
    """
    log_info("Initializing Gold Crossover Bot...")
    if not initialize_mt5():
        log_error("Could not initialize MT5 connection. Exiting.")
        return

    # --- TEST BUY ORDER ON STARTUP ---
    # Removed as per user request
    # ---------------------------------

    log_info(f"Bot successfully started.")
    log_info(f"Symbol: {config.SYMBOL}")
    log_info(f"Timeframe: M15 (15 Minutes)")
    log_info(f"Strategy parameters: EMA{config.EMA_SHORT} / EMA{config.EMA_LONG}")
    log_info(f"Risk per trade: {config.RISK_PERCENT}% of equity")
    log_info(f"Loop interval: {config.LOOP_INTERVAL_SECONDS} seconds")

    last_processed_candle = None

    try:
        while True:
            # Check for closed trades to update AI history
            update_trade_history()
            
            # 1. Fetch signal state
            signal, atr, last_completed_time, confidence, features_dict = check_signal(config.SYMBOL, config.TIMEFRAME)
            
            if last_completed_time is not None:
                # 2. Lock on startup to the current completed candle to prevent historical trade triggers
                if last_processed_candle is None:
                    last_processed_candle = last_completed_time
                    log_info(f"Startup check complete. Signal tracking initialized at candle {last_completed_time}.")
                    log_info("Waiting for the next candle completion to check for crossovers...")
                
                # 3. Detect when a new candle has completed
                elif last_completed_time != last_processed_candle:
                    log_info(f"New candle completed: {last_completed_time}")
                    
                    if signal:
                        log_info(f"SIGNAL DETECTED | {signal} | Confidence: {confidence*100:.1f}%")
                        
                        # Apply AI threshold filter
                        if confidence >= config.AI_MIN_CONFIDENCE:
                            order_type = mt5.ORDER_TYPE_BUY if signal == 'BUY' else mt5.ORDER_TYPE_SELL
                            log_info(f"Executing {signal} trade on {config.SYMBOL}...")
                            
                            from ai_model import log_trade_entry
                            
                            # Execute Order without checking for existing open positions
                            result = place_order(config.SYMBOL, order_type, atr, 
                                        volume=config.LOT_SIZE, 
                                        sl_price_dist=config.FIXED_SL_PRICE_DIST, 
                                        tp_price_dist=config.FIXED_TP_PRICE_DIST)
                                        
                            if result:
                                log_trade_entry(result.order, signal, features_dict)
                        else:
                            log_info(f"Signal ignored. AI confidence {confidence*100:.1f}% is below threshold {config.AI_MIN_CONFIDENCE*100:.1f}%")
                    else:
                        log_info("No crossover detected on this completed candle.")
                    
                    # Lock candle to prevent double processing
                    last_processed_candle = last_completed_time
            
            # Wait before requesting rates again
            time.sleep(config.LOOP_INTERVAL_SECONDS)
            
    except KeyboardInterrupt:
        log_info("Bot execution paused by user (KeyboardInterrupt).")
    except Exception as e:
        log_error(f"Unexpected exception in execution loop: {e}")
    finally:
        mt5.shutdown()
        log_info("MetaTrader 5 connection closed gracefully.")

if __name__ == "__main__":
    run_bot()
