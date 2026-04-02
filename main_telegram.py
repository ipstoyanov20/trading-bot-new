import asyncio
import MetaTrader5 as mt5
import config
from telethon import TelegramClient, events
from logger import log_info, log_error
from signal_parser import parse_signal
from trading_engine import execute_signal

# --- Initialize MT5 ---
def initialize_mt5():
    if not mt5.initialize():
        log_error(f"MT5 initialization failed: {mt5.last_error()}")
        return False
        
    if config.ACCOUNT_LOGIN != 0:
        authorized = mt5.login(
            login=config.ACCOUNT_LOGIN,
            password=config.ACCOUNT_PASSWORD,
            server=config.ACCOUNT_SERVER
        )
        if not authorized:
            log_error(f"Failed to login to account {config.ACCOUNT_LOGIN}")
            return False
            
    account_info = mt5.account_info()
    if account_info:
        log_info(f"MT5 Connected. Account: {account_info.login}, Balance: {account_info.balance}")
        return True
    return False

# --- Telegram Client ---
client = TelegramClient('trading_bot_session', config.TELEGRAM_API_ID, config.TELEGRAM_API_HASH)

@client.on(events.NewMessage(chats=config.TELEGRAM_CHANNELS if config.TELEGRAM_CHANNELS else None))
async def my_event_handler(event):
    message_text = event.message.message
    if not message_text:
        return
        
    log_info(f"NEW MESSAGE RECEIVED | From: {event.chat_id} | Text sample: {message_text[:50]}...")
    
    # Parse the signal
    signal_data = parse_signal(message_text)
    
    if signal_data:
        log_info("Executing signal...")
        # Execute trade in MT5 (Note: execute_signal is synchronous, so we run it in a thread if needed, or just call it)
        # For simplicity in this bot, we just call it.
        execute_signal(signal_data)

async def main():
    if not initialize_mt5():
        return

    log_info("Starting Telegram Listener...")
    
    # Start the client
    await client.start(phone=config.TELEGRAM_PHONE)
    log_info("Telegram Client Started and Authenticated.")
    
    # Run until disconnected
    await client.run_until_disconnected()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log_info("Bot stopped by user.")
    finally:
        mt5.shutdown()
        log_info("MT5 connection closed.")
