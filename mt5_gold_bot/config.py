import MetaTrader5 as mt5

# --- MetaTrader 5 Account Credentials ---
# Set ACCOUNT_LOGIN = 0 to automatically connect to your currently open MT5 desktop terminal.
# Otherwise, provide details to programmatically log in to a specific account.
ACCOUNT_LOGIN = 0              # Account Number (e.g. 12345678)
ACCOUNT_PASSWORD = ""          # Account Password
ACCOUNT_SERVER = ""            # Broker Server Name (e.g. "MetaQuotes-Demo")

# --- Trading Strategy Settings ---
SYMBOL = "XAUUSD"              # Gold Symbol (check if your broker uses XAUUSDm, XAUUSD.r, etc.)
TIMEFRAME = mt5.TIMEFRAME_M15  # Timeframe to analyze candles (15 Minutes)
MAGIC_NUMBER = 20260616        # Unique bot identifier to track its own trades
DEVIATION = 20                 # Max slippage allowed in points

# --- EMA Crossover Configuration ---
EMA_SHORT = 9                  # Fast EMA span
EMA_LONG = 21                  # Slow EMA span

# --- Risk Management ---
RISK_PERCENT = 1.0             # Account equity percentage to risk per trade (e.g. 1.0%)
MAX_LOT_SIZE = 2.0             # Absolute upper limit on trade lot size

# --- Stop Loss (SL) & Take Profit (TP) ---
# ATR-based settings for dynamic targets:
# SL = Entry - (ATR * SL_Multiplier)
# TP = Entry + (ATR * TP_Multiplier)
USE_ATR_FOR_EXIT = True
ATR_PERIOD = 14
ATR_SL_MULT = 2.0
ATR_TP_MULT = 4.0

# Fixed values to fall back on if ATR calculations are disabled or fail:
FIXED_SL_POINTS = 500          # 5.00 points for XAUUSD (assuming 2 decimals)
FIXED_TP_POINTS = 1000         # 10.00 points for XAUUSD

# --- General System Settings ---
LOOP_INTERVAL_SECONDS = 15     # Interval to query MT5 for new candle updates

# --- Simultaneous Orders Configuration ---
LOT_SIZE = 0.02                # Lot size for simultaneous orders
FIXED_TP_PRICE_DIST = 3.0      # Take Profit distance from entry price
FIXED_SL_PRICE_DIST = 2.0      # Stop Loss distance from entry price
