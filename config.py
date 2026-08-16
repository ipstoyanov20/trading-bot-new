import MetaTrader5 as mt5

# --- MetaTrader 5 Account Credentials ---
ACCOUNT_LOGIN = 0              
ACCOUNT_PASSWORD = ""          
ACCOUNT_SERVER = ""            

# --- Trading Strategy Parameters ---
SYMBOLS = ["XAUUSD"]           
SYMBOL_GOLD = "XAUUSD"         
TIMEFRAME = mt5.TIMEFRAME_M15  
MAGIC_NUMBER = 20240328        
DEVIATION = 20                 

# --- Telegram Integration Credentials ---
TELEGRAM_API_ID = 32128299        
TELEGRAM_API_HASH = "66fea73e63fe0a63b24e69bf9854358a"
TELEGRAM_PHONE = "+359886611719"
TELEGRAM_BOT_TOKEN = "8748248083:AAGva0e9L0TL1HHsKXwIk_2Fte5M2pocmmQ" 

TELEGRAM_CHANNELS = [-1003857703703]

# --- EMA Crossover Strategy Settings ---
EMA_SHORT = 9
EMA_LONG = 21

# --- Risk Management ---
RISK_PERCENT = 1.0  
MAX_LOT_SIZE = 1.0  

# --- Investment & Profit Targets ---
INVESTMENT_AMOUNT = 50.0  
TP_PERCENT = 0.15        
SL_PERCENT = 0.10        
TP_BUFFER_USD = 10.0     
USE_PERCENTAGE_EXIT = True 

# --- Stop Loss (SL) and Take Profit (TP) Logic ---
USE_ATR_FOR_EXIT = True

# Fixed Settings (Used if USE_ATR_FOR_EXIT is False or if ATR is missing)
FIXED_SL_POINTS = 10000   
FIXED_TP_POINTS = 1000  

# ATR-Based Settings (Used if USE_ATR_FOR_EXIT is True)
ATR_PERIOD = 14         
ATR_SL_MULT = 10.0      # Wide emergency crash protection SL (to pass broker limits)
ATR_TP_MULT = 1.0       # (Unused in portfolio mode, but kept for fallback)

# --- Revolut X (Crypto Exchange) Configuration ---
REVX_API_KEY = "xXHbY5r2S5Shza7wZl4oxpLM7oSmB5E6N13Sc7CNf0zOm1VUPIi5ECnP5TNigQt1"
REVX_PRIVATE_KEY_PATH = "C:/Users/Gamer/Desktop/tradingbot/revolut_private.pem"
REVX_BASE_URL = "https://revx.revolut.com"

# --- General Scheduler ---
LOOP_INTERVAL_SECONDS = 60

# --- Portfolio Batch Trading Settings ---
PORTFOLIO_TP_USD = 1.0
PORTFOLIO_SL_USD = 1.0
FIXED_LOT_SIZE = 0.01
MAX_BATCH_POSITIONS = 5
