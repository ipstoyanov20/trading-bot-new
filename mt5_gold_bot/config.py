import MetaTrader5 as mt5

# --- MetaTrader 5 Account Credentials ---
# Set ACCOUNT_LOGIN = 0 to automatically connect to your currently open MT5 desktop terminal.
# Otherwise, provide details to programmatically log in to a specific account.
ACCOUNT_LOGIN = 0              # Account Number (e.g. 12345678)
ACCOUNT_PASSWORD = ""          # Account Password
ACCOUNT_SERVER = ""            # Broker Server Name (e.g. "MetaQuotes-Demo")

# --- Trading Strategy Settings ---
SYMBOL = "EURUSD"              # EURUSD Symbol
TIMEFRAME = mt5.TIMEFRAME_M15  # Timeframe to analyze candles (15 Minutes)
MAGIC_NUMBER = 20260616        # Unique bot identifier to track its own trades
DEVIATION = 20                 # Max slippage allowed in points

# --- EMA Crossover Configuration ---
EMA_SHORT = 9                  # Fast EMA span
EMA_LONG = 21                  # Slow EMA span

# --- Risk Management ---
RISK_PERCENT = 30.0            # Account equity percentage to risk per trade (highly aggressive for $50 account)
MAX_LOT_SIZE = 2.0             # Absolute upper limit on trade lot size
LOT_SIZE = 0.01                # Flat fallback lot size for maximum risk trading
CLOSE_POSITION_ON_CANDLE_CLOSE = True  # Automatically close position at the end of the 15m candle

# --- Stop Loss (SL) & Take Profit (TP) ---
# ATR-based settings for dynamic targets:
# SL = Entry - (ATR * SL_Multiplier)
# TP = Entry + (ATR * TP_Multiplier)
USE_ATR_FOR_EXIT = False
ATR_PERIOD = 14
ATR_SL_MULT = 2.0
ATR_TP_MULT = 4.0

# Fixed values to fall back on if ATR calculations are disabled or fail:
# Since we close at the end of 15 minutes, SL/TP are safety nets.
FIXED_SL_POINTS = 100          # 10 pips for EURUSD (assuming 5 decimals)
FIXED_TP_POINTS = 200          # 20 pips for EURUSD

# --- General System Settings ---
LOOP_INTERVAL_SECONDS = 1      # 1-second loop interval for ultra-responsive trade execution and trailing profit monitoring

# --- Simultaneous Orders Configuration ---
FIXED_TP_PRICE_DIST = 0.0500   # Take Profit distance from entry price ($50 for 0.01 lot on EURUSD)
FIXED_SL_PRICE_DIST = 0.0200   # Stop Loss distance from entry price ($20 for 0.01 lot on EURUSD)

# --- AI & ML Configuration ---
RETRAIN_AFTER_N_TRADES = 10    # Retrain XGBoost after this many new trades (more frequent learning)
AI_MIN_CONFIDENCE = 0.50       # Minimum confidence score to trade (aggressive entry)
FIREBASE_CREDENTIALS_PATH = "firebase_credentials.json"

