import MetaTrader5 as mt5

# --- Account Credentials ---
# Replace with your actual MetaTrader 5 demo account details
ACCOUNT_LOGIN = 0          # Example: 12345678
ACCOUNT_PASSWORD = ""      # Example: "MySecretPassword"
ACCOUNT_SERVER = ""        # Example: "MetaQuotes-Demo"

# --- Trading Strategy Parameters ---
SYMBOLS = ["EURUSD", "XAUUSD", "GBPUSD"]
TIMEFRAME = mt5.TIMEFRAME_M15
MAGIC_NUMBER = 20240328
DEVIATION = 20  # Slippage in points

# EMA Crossover Strategy
EMA_SHORT = 9
EMA_LONG = 21

# --- Risk Management ---
RISK_PERCENT = 1.0  # Risk 1% of equity per trade
MAX_LOT_SIZE = 1.0  # Optional safety cap

# SL/TP Settings
# Set to True to use ATR-based SL/TP, False for fixed points
USE_ATR_FOR_EXIT = True

# Fixed Points (if USE_ATR_FOR_EXIT is False)
FIXED_SL_POINTS = 500
FIXED_TP_POINTS = 1000

# ATR Multipliers (if USE_ATR_FOR_EXIT is True)
ATR_PERIOD = 14
ATR_SL_MULT = 2.0
ATR_TP_MULT = 4.0

# --- General ---
LOOP_INTERVAL_SECONDS = 60  # Check for signals every 60 seconds
