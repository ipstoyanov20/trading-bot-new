import os
import pandas as pd
import MetaTrader5 as mt5
from datetime import datetime, timedelta
import config
from logger import log_info, log_error
from ai_model import HISTORY_PATH, train_model

ACTIVE_TRADES_PATH = "active_trades.csv"

def update_trade_history():
    """
    Checks the status of active trades in MT5.
    If a trade is closed, logs its outcome to trade_history.csv for AI training.
    """
    if not os.path.exists(ACTIVE_TRADES_PATH):
        return
        
    try:
        active_df = pd.read_csv(ACTIVE_TRADES_PATH)
    except Exception as e:
        log_error(f"Failed to read {ACTIVE_TRADES_PATH}: {e}")
        return
        
    if active_df.empty:
        return
        
    # Get MT5 history for the last 30 days
    from_date = datetime.now() - timedelta(days=30)
    to_date = datetime.now() + timedelta(days=1)
    
    history_deals = mt5.history_deals_get(from_date, to_date)
    if history_deals is None:
        log_error("Failed to retrieve MT5 history deals.")
        return
        
    closed_tickets = []
    new_history_rows = []
    
    for index, row in active_df.iterrows():
        ticket = int(row['ticket'])
        
        # Find deals matching the position ID (ticket)
        # MT5 positions have an entry deal and an exit deal. The exit deal has the profit.
        deals_for_position = [d for d in history_deals if d.position_id == ticket]
        
        # If there are at least 2 deals for the position, it is fully closed
        # (Entry deal and Exit deal)
        if len(deals_for_position) >= 2:
            exit_deal = deals_for_position[-1]
            profit = exit_deal.profit
            
            # Determine win/loss
            win = 1 if profit > 0 else 0
            
            # Create history row
            history_row = row.copy()
            history_row['win'] = win
            history_row['profit'] = profit
            new_history_rows.append(history_row)
            
            closed_tickets.append(ticket)
            log_info(f"Trade {ticket} closed. Profit: {profit}. Win: {win}")

    if not closed_tickets:
        return
        
    # Append to history
    history_df = pd.DataFrame(new_history_rows)
    if os.path.exists(HISTORY_PATH):
        history_df.to_csv(HISTORY_PATH, mode='a', header=False, index=False)
    else:
        history_df.to_csv(HISTORY_PATH, mode='w', header=True, index=False)
        
    # Remove closed trades from active_trades
    active_df = active_df[~active_df['ticket'].isin(closed_tickets)]
    active_df.to_csv(ACTIVE_TRADES_PATH, index=False)
    
    # Check if we should retrain
    full_history = pd.read_csv(HISTORY_PATH)
    if len(full_history) > 0 and len(full_history) % config.RETRAIN_AFTER_N_TRADES == 0:
        log_info(f"Reached multiple of {config.RETRAIN_AFTER_N_TRADES} closed trades. Triggering AI retraining...")
        train_model()
