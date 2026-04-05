import re
import config
from logger import log_info

def parse_signal(message_text):
    """
    Parses a trading signal from Telegram message text using Regular Expressions.
    
    Expected Signal Format (Example):
        XAUUSD BUY
        ENTRY 2350-2355
        SL 2345
        TP 2360
        TP 2365
        TP 2370
        
    Args:
        message_text (str): The raw text of the Telegram message.
        
    Returns:
        dict: A dictionary containing 'symbol', 'type', 'entry_range', 'sl', and 'tps'.
        None: If the message is not a valid trading signal.
    """
    # Standardize text for processing (Upper case, strip whitespace)
    text = message_text.upper()
    
    # Use our configured symbol for the broker as default
    symbol = config.SYMBOL_GOLD
    
    # 1. Extract Entry Range
    # Matches patterns like "ENTRY 4726-4716" or "ENTRY: 4726 - 4716"
    entry_match = re.search(r"ENTRY\s*:?\s*([\d.]+)\s*-\s*([\d.]+)", text)
    if not entry_match:
        # Fallback: Try to find a single entry price
        entry_match = re.search(r"ENTRY\s*:?\s*([\d.]+)", text)
        if not entry_match:
            # If no entry price is found, the signal is invalid
            return None
        e1 = float(entry_match.group(1))
        # For a single price, provide a tiny 0.1% tolerance range
        entry_range = (e1 * 0.999, e1 * 1.001)
        avg_entry = e1
    else:
        # Convert captured groups to floats and determine min/max for the range
        e1 = float(entry_match.group(1))
        e2 = float(entry_match.group(2))
        entry_range = (min(e1, e2), max(e1, e2))
        avg_entry = (e1 + e2) / 2.0
    
    # 2. Extract Stop Loss (SL)
    sl_match = re.search(r"SL\s*:?\s*([\d.]+)", text)
    if not sl_match:
        # Stop Loss is mandatory
        return None
    sl = float(sl_match.group(1))
    
    # 3. Extract Take Profits (TPs)
    tps_matches = re.findall(r"TP\s*:?\s*([\d.]+)", text)
    if not tps_matches:
        # At least one Take Profit level is required
        return None
    tps = [float(tp) for tp in tps_matches]
    
    # 4. Extract Order Type (BUY or SELL)
    order_type_str = None
    if "BUY" in text:
        order_type_str = "BUY"
    elif "SELL" in text:
        order_type_str = "SELL"
    else:
        # INFER ORDER TYPE FROM SL/TP IF MISSING
        # If SL is above entry, it's a SELL. If SL is below entry, it's a BUY.
        if sl > avg_entry:
            order_type_str = "SELL"
            log_info(f"Inferred SELL signal (SL {sl} > Entry {avg_entry})")
        elif sl < avg_entry:
            order_type_str = "BUY"
            log_info(f"Inferred BUY signal (SL {sl} < Entry {avg_entry})")
        else:
            # If SL == Entry, we can't infer
            return None

    # 5. Determine Symbol (Default to Gold if not found)
    symbol = config.SYMBOL_GOLD
    if "XAUUSD" in text:
        symbol = "XAUUSD"
    elif "GOLD" in text:
        symbol = config.SYMBOL_GOLD
    # Add other symbols if needed, but defaulting to Gold as it's the primary use case
    
    # Final Signal Data object
    signal = {
        "symbol": symbol,
        "type": order_type_str,
        "entry_range": entry_range,
        "sl": sl,
        "tps": tps
    }
    
    log_info(f"SIGNAL PARSED | {signal}")
    return signal
