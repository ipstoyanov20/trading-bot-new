# --- Revolut X Cloud Bot Configuration ---

# Required for connecting to the Telegram API (from https://my.telegram.org)
TELEGRAM_API_ID = 32128299        
TELEGRAM_API_HASH = "66fea73e63fe0a63b24e69bf9854358a"
TELEGRAM_PHONE = "+359886611719"

# The channel ID where the bot will send signals
TELEGRAM_CHANNELS = [-1003857703703]

# --- Revolut X (Crypto Exchange) Configuration ---
# Generate these in the Revolut X web interface
REVX_API_KEY = "xXHbY5r2S5Shza7wZl4oxpLM7oSmB5E6N13Sc7CNf0zOm1VUPIi5ECnP5TNigQt1"
REVX_PRIVATE_KEY_PATH = "revolut_private.pem" # On PythonAnywhere, just use the filename
REVX_BASE_URL = "https://revx.revolut.com"

# --- Strategy Parameters ---
EMA_SHORT = 9
EMA_LONG = 21
TIMEFRAME = 5 # minutes
