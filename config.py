import MetaTrader5 as mt5

# --- Account Credentials ---
# Replace with your actual MetaTrader 5 demo account details
ACCOUNT_LOGIN = 0          # Example: 12345678
ACCOUNT_PASSWORD = ""      # Example: "MySecretPassword"
ACCOUNT_SERVER = ""        # Example: "MetaQuotes-Demo"

# --- Trading Strategy Parameters ---
SYMBOLS = ["XAUUSD"]
SYMBOL_GOLD = "XAUUSD"       # Adjust if your broker uses a different name (e.g., "XAUUSDm", "GOLD")
TIMEFRAME = mt5.TIMEFRAME_M15
MAGIC_NUMBER = 20240328
DEVIATION = 20  # Slippage in points

# --- Telegram Credentials ---
# Get these from https://my.telegram.org
TELEGRAM_API_ID = 32128299        # Example: 123456
TELEGRAM_API_HASH = "66fea73e63fe0a63b24e69bf9854358a"       # Example: "abcdef1234567890"
TELEGRAM_PHONE = "+359886611719"          # Example: "+1234567890"
TELEGRAM_CHANNELS = ['forsexfreegroup']       # List of channel names or IDs to monitor

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
