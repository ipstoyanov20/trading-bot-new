import MetaTrader5 as mt5

# --- MetaTrader 5 Account Credentials ---
# Replace these with your actual broker account details.
ACCOUNT_LOGIN = 0              # Your Account Number (e.g., 12345678)
ACCOUNT_PASSWORD = ""          # Your Account Password
ACCOUNT_SERVER = ""            # Your Broker's Server Name (e.g., "MetaQuotes-Demo")

# --- Trading Strategy Parameters ---
SYMBOLS = ["XAUUSD"]           # All symbols the bot can trade
SYMBOL_GOLD = "XAUUSD"         # Global reference for Gold (check your broker's name)
TIMEFRAME = mt5.TIMEFRAME_M15  # Default analysis timeframe (15 minutes)
MAGIC_NUMBER = 20240328        # Unique bot identifier to track its own trades
DEVIATION = 20                 # Max slippage allowed in points (1 point = 0.01 for XAUUSD)

# --- Telegram Integration Credentials ---
# Required for connecting to the Telegram API (obtain from https://my.telegram.org)
TELEGRAM_API_ID = 32128299        
TELEGRAM_API_HASH = "66fea73e63fe0a63b24e69bf9854358a"
TELEGRAM_PHONE = "+359886611719"

# Channels the bot will monitor for trading signals (Username or numeric ID)
TELEGRAM_CHANNELS = ['forsexfreegroup']

# --- EMA Crossover Strategy Settings ---
EMA_SHORT = 9
EMA_LONG = 21

# --- Risk Management ---
RISK_PERCENT = 1.0  # Percentage of account equity to risk per trade
MAX_LOT_SIZE = 1.0  # Absolute limit for individual trade volume

# --- Stop Loss (SL) and Take Profit (TP) Logic ---
# Set 'USE_ATR_FOR_EXIT' to True to dynamically set SL/TP based on market volatility (ATR).
# Set to False to use fixed point distances from the entry price.
USE_ATR_FOR_EXIT = True

# Fixed Settings (Used if USE_ATR_FOR_EXIT is False)
FIXED_SL_POINTS = 500   # 5.00 points for XAUUSD
FIXED_TP_POINTS = 1000  # 10.00 points for XAUUSD

# ATR-Based Settings (Used if USE_ATR_FOR_EXIT is True)
ATR_PERIOD = 14         # Period for the ATR indicator calculation
ATR_SL_MULT = 2.0       # Multiplier for the SL distance (e.g., SL = entry - 2*ATR)
ATR_TP_MULT = 4.0       # Multiplier for the TP distance (e.g., TP = entry + 4*ATR)

# --- General Scheduler ---
# Interval at which the bot checks for new candles/data (if running a background loop)
LOOP_INTERVAL_SECONDS = 60
