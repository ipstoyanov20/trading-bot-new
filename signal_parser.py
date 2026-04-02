import re
import config
from logger import log_info

def parse_signal(message_text):
    """
    Parses a trading signal from Telegram message text.
    Format: XAUUSD BUY
            ENTRY 4726-4716
            SL 4711
            TP 4730
            TP 4735
            TP 4733
    """
    # Clean up the text
    text = message_text.upper()
    
    # Check if it's for gold (XAUUSD)
    if "XAUUSD" not in text and "GOLD" not in text:
        return None
    
    symbol = config.SYMBOL_GOLD
    
    # Check for order type
    if "BUY" in text:
        order_type_str = "BUY"
    elif "SELL" in text:
        order_type_str = "SELL"
    else:
        return None
        
    # Extract Entry Range
    # Matches ENTRY 4726-4716 or ENTRY: 4726 - 4716 or 4638.8-4648.8
    entry_match = re.search(r"ENTRY\s*:?\s*(\d+\.?\d*)\s*-\s*(\d+\.?\d*)", text)
    if not entry_match:
        # Try single entry price
        entry_match = re.search(r"ENTRY\s*:?\s*(\d+\.?\d*)", text)
        if not entry_match:
            return None
        e1 = float(entry_match.group(1))
        entry_range = (e1 * 0.999, e1 * 1.001) # Small tolerance if single price
    else:
        e1 = float(entry_match.group(1))
        e2 = float(entry_match.group(2))
        entry_range = (min(e1, e2), max(e1, e2))
    
    # Extract SL
    sl_match = re.search(r"SL\s*:?\s*([\d.]+)", text)
    if not sl_match:
        return None
    sl = float(sl_match.group(1))
    
    # Extract TPs
    # Looks for all TP X or TP: X or TPX
    tps_matches = re.findall(r"TP\s*:?\s*([\d.]+)", text)
    if not tps_matches:
        return None
    tps = [float(tp) for tp in tps_matches]
    
    # Ensure they are sorted for the trades (TP1 should be closest)
    # Actually, keep the order provided as it usually goes from TP1 to TP3
    # tps.sort() # Don't sort, keep original order
    
    signal = {
        "symbol": symbol,
        "type": order_type_str,
        "entry_range": entry_range,
        "sl": sl,
        "tps": tps
    }
    
    log_info(f"SIGNAL PARSED | {signal}")
    return signal
