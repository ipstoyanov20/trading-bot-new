import time
import MetaTrader5 as mt5
import config
from logger import log_info, log_error
from strategy import check_signal
from trading_engine import initialize_mt5, check_open_positions, place_order, close_all_bot_positions
from trade_tracker import update_trade_history

def run_bot():
    """
    Main loop orchestrating the MT5 Gold Candle Predictor Bot.
    """
    log_info("Initializing Gold Candle Predictor Bot...")
    if not initialize_mt5():
        log_error("Could not initialize MT5 connection. Exiting.")
        return

    # --- STARTUP CHECK ---
    log_info("Performing startup check...")
    if check_open_positions(config.SYMBOL):
        log_info("Active trade detected on startup. Skipping startup test trade.")
    else:
        log_info("Executing startup AI-driven test trade...")
        startup_signal, startup_atr, _, startup_confidence, startup_features = check_signal(config.SYMBOL, config.TIMEFRAME)
        if startup_signal:
            if startup_confidence < config.AI_MIN_CONFIDENCE:
                log_info(f"Startup trade skipped: AI Confidence ({startup_confidence*100:.1f}%) is below minimum threshold ({config.AI_MIN_CONFIDENCE*100:.1f}%).")
            else:
                log_info(f"STARTUP SIGNAL DETECTED | {startup_signal} | Confidence: {startup_confidence*100:.1f}%")
                startup_order_type = mt5.ORDER_TYPE_BUY if startup_signal == 'BUY' else mt5.ORDER_TYPE_SELL
                
                from firebase_logger import save_trade_entry
                result = place_order(config.SYMBOL, startup_order_type, startup_atr, 
                            volume=config.LOT_SIZE, 
                            sl_price_dist=config.FIXED_SL_PRICE_DIST, 
                            tp_price_dist=config.FIXED_TP_PRICE_DIST)
                            
                if result:
                    save_trade_entry(result.order, startup_signal, result.price, result.volume, startup_confidence, startup_features)
                    log_info("Startup test trade executed successfully.")
                else:
                    log_error("Startup test trade failed to execute.")
    # ---------------------------------
    log_info(f"Bot successfully started.")
    log_info(f"Symbol: {config.SYMBOL}")
    log_info(f"Timeframe: M30 (30 Minutes)")
    log_info(f"Strategy parameters: EMA{config.EMA_SHORT} / EMA{config.EMA_LONG}")
    log_info(f"Fixed Lot Size: {config.LOT_SIZE}")
    log_info(f"Fixed SL Dist: {config.FIXED_SL_PRICE_DIST}, Fixed TP Dist: {config.FIXED_TP_PRICE_DIST}")
    log_info(f"Loop interval: {config.LOOP_INTERVAL_SECONDS} seconds")

    last_processed_candle = None

    try:
        while True:
            # 1. Fetch signal state to monitor the timeline
            signal, atr, last_completed_time, confidence, features_dict = check_signal(config.SYMBOL, config.TIMEFRAME)
            
            if last_completed_time is not None:
                # 2. Lock on startup to the current completed candle to prevent historical trade triggers
                if last_processed_candle is None:
                    last_processed_candle = last_completed_time
                    log_info(f"Startup check complete. Signal tracking initialized at candle {last_completed_time}.")
                    log_info("Waiting for the next candle completion to check for predictions...")
                
                # 3. Detect when a new candle has completed (runs every 30 minutes)
                elif last_completed_time != last_processed_candle:
                    log_info(f"New 30m candle completed at: {last_completed_time}")
                    
                    # A. Close active trades if config is set
                    if config.CLOSE_POSITION_ON_CANDLE_CLOSE:
                        log_info("Closing previous 30m positions...")
                        close_all_bot_positions(config.SYMBOL)
                    
                    # B. Check for closed trades to update AI history/Firestore
                    update_trade_history()
                    
                    # C. Place new trade based on current signal
                    if signal:
                        if confidence < config.AI_MIN_CONFIDENCE:
                            log_info(f"Trade skipped: AI Confidence ({confidence*100:.1f}%) is below minimum threshold ({config.AI_MIN_CONFIDENCE*100:.1f}%).")
                        else:
                            log_info(f"SIGNAL DETECTED | {signal} | Confidence: {confidence*100:.1f}%")
                            
                            order_type = mt5.ORDER_TYPE_BUY if signal == 'BUY' else mt5.ORDER_TYPE_SELL
                            log_info(f"Executing {signal} trade on {config.SYMBOL}...")
                            
                            from firebase_logger import save_trade_entry
                            
                            result = place_order(config.SYMBOL, order_type, atr, 
                                        volume=config.LOT_SIZE, 
                                        sl_price_dist=config.FIXED_SL_PRICE_DIST, 
                                        tp_price_dist=config.FIXED_TP_PRICE_DIST)
                                        
                            if result:
                                save_trade_entry(result.order, signal, result.price, result.volume, confidence, features_dict)
                    
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
