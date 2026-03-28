import time
import MetaTrader5 as mt5
import config
from logger import log_info, log_error
from strategy import check_signal
from trading_engine import check_open_positions, place_order

def initialize_mt5():
    """Initializes MetaTrader 5 and performs login."""
    if not mt5.initialize():
        log_error(f"MT5 initialization failed: {mt5.last_error()}")
        return False
        
    # If login details are provided in config, attempt login
    if config.ACCOUNT_LOGIN != 0:
        authorized = mt5.login(
            login=config.ACCOUNT_LOGIN,
            password=config.ACCOUNT_PASSWORD,
            server=config.ACCOUNT_SERVER
        )
        if not authorized:
            log_error(f"Failed to login to account {config.ACCOUNT_LOGIN}: {mt5.last_error()}")
            return False
            
    # Check account details
    account_info = mt5.account_info()
    if account_info is None:
        log_error("Failed to get account information.")
        return False
    
    log_info(f"MT5 Connected. Account: {account_info.login}, Balance: {account_info.balance} {account_info.currency}")
    return True

def run_bot():
    """Main execution loop."""
    if not initialize_mt5():
        return

    log_info(f"Bot started. Monitoring symbols: {config.SYMBOLS}")
    log_info(f"Strategy: EMA Crossover ({config.EMA_SHORT}/{config.EMA_LONG}) on timeframe {config.TIMEFRAME}")

    try:
        while True:
            # Cycle through all configured symbols
            for symbol in config.SYMBOLS:
                # Requirement: Only one trade at a time for this bot
                if check_open_positions(symbol):
                    # Skip if symbol already has an open position
                    continue
                
                # Get signal from strategy
                signal, atr = check_signal(symbol, config.TIMEFRAME)
                
                if signal == 'BUY':
                    log_info(f"SIGNAL | {symbol} | BUY cross detected.")
                    place_order(symbol, mt5.ORDER_TYPE_BUY, atr)
                    
                elif signal == 'SELL':
                    log_info(f"SIGNAL | {symbol} | SELL cross detected.")
                    place_order(symbol, mt5.ORDER_TYPE_SELL, atr)
            
            # Wait for the next interval
            time.sleep(config.LOOP_INTERVAL_SECONDS)
            
    except KeyboardInterrupt:
        log_info("Bot shutting down (KeyboardInterrupt)...")
    except Exception as e:
        log_error(f"Unexpected error in main loop: {e}")
    finally:
        mt5.shutdown()
        log_info("MT5 connection closed gracefully.")

if __name__ == "__main__":
    run_bot()
