import asyncio
import MetaTrader5 as mt5
import config
from telethon import TelegramClient
from signal_parser import parse_signal
from trading_engine import execute_signal
import logging

# Disable all internal logging to see our prints clearly
logging.getLogger('telethon').setLevel(logging.CRITICAL)

async def execute_third_historical_message():
    # 1. Initialize MT5
    if not mt5.initialize():
        print(f"MT5 initialization failed: {mt5.last_error()}")
        return

    if config.ACCOUNT_LOGIN != 0:
        authorized = mt5.login(
            login=config.ACCOUNT_LOGIN,
            password=config.ACCOUNT_PASSWORD,
            server=config.ACCOUNT_SERVER
        )
        if not authorized:
            print(f"Failed to login to MT5 account {config.ACCOUNT_LOGIN}")
            mt5.shutdown()
            return
            
    # 2. Setup Telegram Client
    client = TelegramClient('trading_bot_new_session', config.TELEGRAM_API_ID, config.TELEGRAM_API_HASH)
    
    try:
        await client.start(phone=config.TELEGRAM_PHONE)
        print("Telegram Client Started.")
        
        channel = config.TELEGRAM_CHANNELS[0]
        print(f"Fetching messages from {channel}...")
        
        # Search history for the 3rd valid signal
        print(f"Searching history of {channel} for the 3rd valid signal...")
        messages = await client.get_messages(channel, limit=100)
        
        valid_signals_found = []
        
        for msg in messages:
            if not msg.message: continue
            
            signal_data = parse_signal(msg.message)
            if signal_data:
                valid_signals_found.append((msg, signal_data))
                if len(valid_signals_found) >= 3:
                    break
        
        if len(valid_signals_found) < 3:
            print(f"❌ Error: Found only {len(valid_signals_found)} valid signals in the last 100 messages.")
            return

        # Target is the 3rd one we found (0: 1st newest, 1: 2nd newest, 2: 3rd newest)
        target_msg, target_signal = valid_signals_found[2]
        
        print("\n--- Found 3 Signals Recently ---")
        for i, (m, s) in enumerate(valid_signals_found):
            print(f"Signal #{i+1} ({m.date}): {s['symbol']} {s['type']} at {s['entry_range']}")
        print("-" * 30)
        
        print(f"✅ Ready to execute signal #3:\n{target_msg.message}")
        print("-" * 30)
        
        # 3. Execute
        print(f"Executing {target_signal['symbol']} {target_signal['type']} in MT5...")
        execute_signal(target_signal)
        print("Execution command sent to MT5.")
            
    except Exception as e:
        print(f"An error occurred: {e}")
    finally:
        await client.disconnect()
        mt5.shutdown()
        print("Connections closed.")

if __name__ == "__main__":
    asyncio.run(execute_third_historical_message())
