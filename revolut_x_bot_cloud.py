import time
import base64
import json
import requests
import pandas as pd
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives import serialization
import config_cloud as config
import os
import asyncio
from telethon import TelegramClient

class RevolutXAuth:
    def __init__(self, api_key, private_key_path):
        self.api_key = api_key
        with open(private_key_path, "rb") as f:
            self.private_key = serialization.load_pem_private_key(f.read(), password=None)

    def get_headers(self, method, path, query_string="", body=""):
        timestamp = str(int(time.time() * 1000))
        if not path.startswith("/"): path = "/" + path
        message = f"{timestamp}{method.upper()}{path}{query_string}{body}"
        signature_bytes = self.private_key.sign(message.encode('utf-8'))
        signature_b64 = base64.b64encode(signature_bytes).decode('utf-8')
        return {
            "X-Revx-API-Key": self.api_key,
            "X-Revx-Timestamp": timestamp,
            "X-Revx-Signature": signature_b64,
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

class RevolutXBotCloud:
    def __init__(self):
        self.auth = RevolutXAuth(config.REVX_API_KEY, config.REVX_PRIVATE_KEY_PATH)
        self.base_url = config.REVX_BASE_URL
        self.revx_symbol = "BTC-USD"
        self.interval = 5 
        self.fast_ma = 9
        self.slow_ma = 21
        self.tg_client = TelegramClient('revolut_cloud_session', config.TELEGRAM_API_ID, config.TELEGRAM_API_HASH)

    def fetch_candles(self):
        path = f"/api/1.0/candles/{self.revx_symbol}"
        query_string = f"interval={self.interval}"
        url = f"{self.base_url}{path}?{query_string}"
        try:
            headers = self.auth.get_headers("GET", path, query_string=query_string)
            response = requests.get(url, headers=headers)
            if response.status_code == 200:
                json_data = response.json()
                candles = json_data['data'] if isinstance(json_data, dict) and 'data' in json_data else json_data
                df = pd.DataFrame(candles)
                if 'start' in df.columns: df.rename(columns={'start': 'timestamp'}, inplace=True)
                df['close'] = pd.to_numeric(df['close'])
                return df
        except Exception as e: print(f"Fetch Error: {e}")
        return None

    def generate_signal(self, df):
        if df is None or len(df) < self.slow_ma + 1: return None, None
        df['ema_fast'] = df['close'].ewm(span=self.fast_ma, adjust=False).mean()
        df['ema_slow'] = df['close'].ewm(span=self.slow_ma, adjust=False).mean()
        prev_row = df.iloc[-2]; curr_row = df.iloc[-1]
        last_price = curr_row['close']
        
        signal_found = None
        if prev_row['ema_fast'] <= prev_row['ema_slow'] and curr_row['ema_fast'] > curr_row['ema_slow']:
            signal_found = "BUY"
        elif prev_row['ema_fast'] >= prev_row['ema_slow'] and curr_row['ema_fast'] < curr_row['ema_slow']:
            signal_found = "SELL"
        
        return signal_found, last_price

    async def run(self):
        print(f"--- Revolut X Cloud Bot Started ---")
        try:
            await self.tg_client.start(phone=config.TELEGRAM_PHONE)
            channel_target = config.TELEGRAM_CHANNELS[0] if config.TELEGRAM_CHANNELS else 'me'
            await self.tg_client.send_message(channel_target, "🚀 **Cloud Bot Started**\nMonitoring BTC-USD (5m)")
            
            last_sent_price = 0
            while True:
                df = self.fetch_candles()
                if df is not None:
                    signal, price = self.generate_signal(df)
                    if price != last_sent_price:
                        ema9 = round(df['ema_fast'].iloc[-1], 2)
                        ema21 = round(df['ema_slow'].iloc[-1], 2)
                        status_msg = f"💰 **Price:** ${price:,.2f} | EMA9: {ema9} | EMA21: {ema21}"
                        if signal: status_msg += f"\n🚨 **SIGNAL: {signal}**"
                        await self.tg_client.send_message(channel_target, status_msg)
                        last_sent_price = price
                        print(f"Update: ${price} | Signal: {signal if signal else 'None'}")
                await asyncio.sleep(10)
        except Exception as e: print(f"Error: {e}")

if __name__ == "__main__":
    bot = RevolutXBotCloud()
    asyncio.run(bot.run())
