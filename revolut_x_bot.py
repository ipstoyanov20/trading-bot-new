import time
import base64
import json
import requests
import pandas as pd
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives import serialization
import config
import os
import MetaTrader5 as mt5
import asyncio
from telethon import TelegramClient
import logger

class RevolutXAuth:
    """
    Handles authentication for Revolut X API using Ed25519 signing.
    """
    def __init__(self, api_key, private_key_path):
        self.api_key = api_key
        if not os.path.exists(private_key_path):
            self._generate_keys(private_key_path)
        
        with open(private_key_path, "rb") as f:
            self.private_key = serialization.load_pem_private_key(f.read(), password=None)

    def _generate_keys(self, path):
        """
        Helper to generate Ed25519 keys if they don't exist.
        """
        print(f"Key file not found. Generating new Ed25519 key pair at {path}...")
        private_key = ed25519.Ed25519PrivateKey.generate()
        
        # Save Private Key
        with open(path, "wb") as f:
            f.write(private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption()
            ))
        
        # Save Public Key (for Revolut X portal)
        public_key = private_key.public_key()
        pub_path = path.replace(".pem", "_public.pem")
        with open(pub_path, "wb") as f:
            f.write(public_key.public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo
            ))
        
        print(f"--- IMPORTANT ---")
        print(f"Please upload the public key to your Revolut X Developer Portal:")
        print(f"Public Key Path: {os.path.abspath(pub_path)}")
        print(f"------------------")

    def get_headers(self, method, path, query_string="", body=""):
        """
        Generates the required headers for a Revolut X API request.
        Format: Timestamp + Method + Path + QueryString + Body
        """
        timestamp = str(int(time.time() * 1000))
        
        # Ensure path starts with /
        if not path.startswith("/"):
            path = "/" + path
            
        # Message for signing (timestamp + method + path + query_string + body)
        # Query string should NOT include the '?'
        message = f"{timestamp}{method.upper()}{path}{query_string}{body}"
        
        # Sign the message
        signature_bytes = self.private_key.sign(message.encode('utf-8'))
        signature_b64 = base64.b64encode(signature_bytes).decode('utf-8')

        return {
            "X-Revx-API-Key": self.api_key,
            "X-Revx-Timestamp": timestamp,
            "X-Revx-Signature": signature_b64,
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

class RevolutXBot:
    def __init__(self):
        try:
            self.auth = RevolutXAuth(config.REVX_API_KEY, config.REVX_PRIVATE_KEY_PATH)
        except Exception as e:
            print(f"Initialization Error: {e}")
            self.auth = None
            
        self.base_url = config.REVX_BASE_URL
        
        # Mapping Revolut X symbols to MT5 symbols
        self.symbol_map = {"BTC-USD": "BTCUSD", "ETH-USD": "ETHUSD", "SOL-USD": "SOLUSD"}
        self.revx_symbol = "BTC-USD"
        self.mt5_symbol = self.symbol_map.get(self.revx_symbol, self.revx_symbol)
        
        self.interval = 5 
        self.fast_ma = 9
        self.slow_ma = 21
        self.lot_size = 0.1 

        # Telegram Setup - Use a different session file if logging in as a Bot
        session_name = 'revolut_bot_token_session' if hasattr(config, 'TELEGRAM_BOT_TOKEN') and config.TELEGRAM_BOT_TOKEN else 'revolut_bot_session'
        self.tg_client = TelegramClient(session_name, config.TELEGRAM_API_ID, config.TELEGRAM_API_HASH)

    def init_mt5(self):
        if not mt5.initialize():
            print(f"MT5 initialization failed: {mt5.last_error()}")
            return False
        return True

    async def send_telegram_signal(self, signal, price):
        """Sends a signal notification to the configured Telegram channel."""
        try:
            if not self.tg_client.is_connected():
                # Prefer Bot Token for login if available
                if hasattr(config, 'TELEGRAM_BOT_TOKEN') and config.TELEGRAM_BOT_TOKEN:
                    await self.tg_client.start(bot_token=config.TELEGRAM_BOT_TOKEN)
                else:
                    await self.tg_client.start(phone=config.TELEGRAM_PHONE)
            
            message = (
                f"🚨 **NEW TRADING SIGNAL** 🚨\n\n"
                f"**Exchange:** Revolut X\n"
                f"**Pair:** {self.revx_symbol}\n"
                f"**Action:** {signal} NOW\n"
                f"**Price:** ${price:,.2f}\n"
                f"**Timeframe:** {self.interval}m\n"
                f"**Strategy:** EMA {self.fast_ma}/{self.slow_ma} Crossover"
            )
            
            if config.TELEGRAM_CHANNELS:
                await self.tg_client.send_message(config.TELEGRAM_CHANNELS[0], message)
                logger.log_info(f"Telegram signal alert sent to {config.TELEGRAM_CHANNELS[0]}")
        except Exception as e:
            logger.log_error(f"Failed to send Telegram signal: {e}")

    def fetch_candles(self):
        """Fetches historical candles from Revolut X."""
        if not self.auth: 
            print("Fetch Error: No Auth")
            return None
            
        path = f"/api/1.0/candles/{self.revx_symbol}"
        query_string = f"interval={self.interval}"
        url = f"{self.base_url}{path}?{query_string}"
        
        try:
            headers = self.auth.get_headers("GET", path, query_string=query_string)
            response = requests.get(url, headers=headers)
            if response.status_code == 200:
                json_data = response.json()
                
                # Handle nested 'data' key if it exists
                if isinstance(json_data, dict) and 'data' in json_data:
                    candles = json_data['data']
                else:
                    candles = json_data
                    
                if not candles: 
                    print(f"Fetch Warning: No data returned from {url}")
                    return None
                    
                df = pd.DataFrame(candles)
                if 'start' in df.columns: df.rename(columns={'start': 'timestamp'}, inplace=True)
                
                if 'close' in df.columns:
                    df['close'] = pd.to_numeric(df['close'])
                    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
                    return df
                else:
                    print(f"Fetch Error: Missing 'close' column. Columns: {df.columns}")
            else:
                print(f"Fetch Error: {response.status_code} - {response.text}")
        except Exception as e: 
            print(f"Fetch Exception: {e}")
            
        return None

    def generate_signal(self, df):
        """Generates signals and prints current status."""
        if df is None or len(df) < self.slow_ma + 1: return None, None
        df['ema_fast'] = df['close'].ewm(span=self.fast_ma, adjust=False).mean()
        df['ema_slow'] = df['close'].ewm(span=self.slow_ma, adjust=False).mean()
        prev_row = df.iloc[-2]; curr_row = df.iloc[-1]
        last_price = curr_row['close']
        fast_val = round(curr_row['ema_fast'], 2); slow_val = round(curr_row['ema_slow'], 2)
        
        status = "Monitoring..."
        signal_found = None
        
        if prev_row['ema_fast'] <= prev_row['ema_slow'] and curr_row['ema_fast'] > curr_row['ema_slow']:
            status = "🚀 SIGNAL! (BUY)"
            signal_found = "BUY"
        elif prev_row['ema_fast'] >= prev_row['ema_slow'] and curr_row['ema_fast'] < curr_row['ema_slow']:
            status = "🔻 SIGNAL! (SELL)"
            signal_found = "SELL"

        print(f"[{pd.Timestamp.now().strftime('%H:%M:%S')}] {self.revx_symbol}: ${last_price:,.2f} | EMA9: {fast_val} | EMA21: {slow_val} | {status}")
        return signal_found, last_price

    def execute_trade_mt5(self, signal):
        """Executes the trade on MetaTrader 5 with detailed logging."""
        if not self.init_mt5(): 
            logger.log_error("MT5 initialization failed for trade execution.")
            return
            
        try:
            tick = mt5.symbol_info_tick(self.mt5_symbol)
            if tick is None: 
                logger.log_error(f"Could not get tick info for {self.mt5_symbol}")
                return
                
            order_type = mt5.ORDER_TYPE_BUY if signal == "BUY" else mt5.ORDER_TYPE_SELL
            price = tick.ask if signal == "BUY" else tick.bid
            
            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": self.mt5_symbol,
                "volume": float(self.lot_size),
                "type": order_type,
                "price": price,
                "magic": 999999,
                "comment": "Revolut X Signal",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }
            
            logger.log_info(f"Sending MT5 Order: {json.dumps(request, indent=2)}")
            
            result = mt5.order_send(request)
            
            if result is None:
                logger.log_error("MT5 order_send returned None. Check connection.")
                return

            if result.retcode == mt5.TRADE_RETCODE_DONE:
                logger.log_trade(self.mt5_symbol, signal, self.lot_size, price, 0, 0, "SUCCESS")
                logger.log_info(f"MT5 Trade Successful! Ticket: {result.order}")
            else:
                logger.log_error(f"MT5 Trade Failed! Retcode: {result.retcode}, Comment: {result.comment}")
                logger.log_info(f"Full MT5 Response: {result._asdict()}")
                
        except Exception as e:
            logger.log_error(f"Exception during MT5 trade execution: {e}")
        finally:
            mt5.shutdown()

    async def run(self):
        print(f"--- Revolut X -> Telegram Bot ---")
        
        try:
            # Login as Bot if token exists, otherwise use User account
            if hasattr(config, 'TELEGRAM_BOT_TOKEN') and config.TELEGRAM_BOT_TOKEN:
                await self.tg_client.start(bot_token=config.TELEGRAM_BOT_TOKEN)
            else:
                await self.tg_client.start(phone=config.TELEGRAM_PHONE)
                
            channel_target = config.TELEGRAM_CHANNELS[0] if config.TELEGRAM_CHANNELS else 'me'
            
            startup_msg = f"🤖 **Revolut X Bot Active**\nPair: `{self.revx_symbol}`\nTimeframe: `{self.interval}m`\nStrategy: EMA {self.fast_ma}/{self.slow_ma}"
            await self.tg_client.send_message(channel_target, startup_msg)
            print(f"Bot started. Monitoring {self.revx_symbol}...")

            last_heartbeat_time = 0
            heartbeat_interval = 300 # 5 minutes

            while True:
                df = self.fetch_candles()
                if df is not None:
                    signal, price = self.generate_signal(df)
                    
                    current_time = time.time()
                    
                    # 1. Send immediate signal notification
                    if signal:
                        await self.send_telegram_signal(signal, price)
                        self.execute_trade_mt5(signal)
                        last_heartbeat_time = current_time
                    
                    # 2. Send periodic heartbeat status update (every 5 mins)
                    elif current_time - last_heartbeat_time >= heartbeat_interval:
                        ema9 = round(df['ema_fast'].iloc[-1], 2)
                        ema21 = round(df['ema_slow'].iloc[-1], 2)
                        diff = round(ema9 - ema21, 2)
                        
                        status_msg = (
                            f"🤖 **Bot Status Update**\n"
                            f"Pair: `{self.revx_symbol}`\n"
                            f"Price: **${price:,.2f}**\n"
                            f"EMA9: `{ema9}` | EMA21: `{ema21}`\n"
                            f"Gap: `{diff}` " + ("📈" if diff > 0 else "📉")
                        )
                        
                        await self.tg_client.send_message(channel_target, status_msg)
                        last_heartbeat_time = current_time
                        logger.log_info(f"Periodic Telegram heartbeat sent: ${price:,.2f}")

                await asyncio.sleep(10) 
        except KeyboardInterrupt: print("Bot stopped.")
        except Exception as e: print(f"Critical error: {e}")

if __name__ == "__main__":
    bot = RevolutXBot()
    asyncio.run(bot.run())
