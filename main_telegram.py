import asyncio
import MetaTrader5 as mt5
import config
from telethon import TelegramClient, events
from logger import log_info, log_error
from signal_parser import parse_signal
from trading_engine import execute_signal, get_last_positions

# --- MetaTrader 5 Initialization ---
def initialize_mt5():
    """
    Initializes the MetaTrader 5 terminal and logs into the trading account.
    Returns:
        bool: True if initialization and login are successful, False otherwise.
    """
    # Initialize the MT5 terminal
    if not mt5.initialize():
        log_error(f"MT5 initialization failed: {mt5.last_error()}")
        return False
        
    # Attempt to login if account details are provided in config
    if config.ACCOUNT_LOGIN != 0:
        authorized = mt5.login(
            login=config.ACCOUNT_LOGIN,
            password=config.ACCOUNT_PASSWORD,
            server=config.ACCOUNT_SERVER
        )
        if not authorized:
            log_error(f"Failed to login to account {config.ACCOUNT_LOGIN}")
            return False
            
    # Success check: Get account info
    account_info = mt5.account_info()
    if account_info:
        log_info(f"MT5 Connected. Account: {account_info.login}, Balance: {account_info.balance}")
        return True
    return False

# --- Telegram Client Setup ---
# Session name 'trading_bot_new_session' is used to store login state locally
client = TelegramClient('trading_bot_new_session', config.TELEGRAM_API_ID, config.TELEGRAM_API_HASH)

@client.on(events.NewMessage(chats=config.TELEGRAM_CHANNELS if config.TELEGRAM_CHANNELS else None))
async def my_event_handler(event):
    """
    Event handler for new Telegram messages from specified channels.
    Parses signals and executes trades if a valid signal is found.
    """
    message_text = event.message.message
    if not message_text:
        return
        
    log_info(f"NEW MESSAGE RECEIVED | From: {event.chat_id} | Text sample: {message_text[:50]}...")
    
    # 1. Parse the signal from the message text
    signal_data = parse_signal(message_text)
    
    # 2. If a valid signal is detected, execute the trade
    if signal_data:
        log_info("Executing signal...")
        # Execute trade in MT5 (Synchronous call within the signal handler)
        result = execute_signal(signal_data)
        
        # After execution, report the status of the last 3 positions to the channel
        last_positions = get_last_positions(3)
        if last_positions:
            response = "✅ **Trade Executed!**\n\n**Current Last 3 Positions:**\n"
            for pos in last_positions:
                response += f"• {pos['type']} {pos['symbol']} | Lots: {pos['volume']} | Open: {pos['price_open']} | Profit: {pos['profit']}\n"
            await event.respond(response)

    # 3. Check for specific commands (e.g., /last to see recent trades)
    if message_text.lower() == '/last':
        last_positions = get_last_positions(3)
        if last_positions:
            response = "**Recent Positions Status:**\n\n"
            for pos in last_positions:
                response += f"• {pos['type']} {pos['symbol']} | Lots: {pos['volume']} | Open: {pos['price_open']} | Profit: {pos['profit']}\n"
            await event.respond(response)
        else:
            await event.respond("No active positions found.")

async def main():
    """
    Main entry point for the Telegram bot.
    Initializes MT5 and starts listening for messages.
    """
    if not initialize_mt5():
        log_error("Could not initialize MT5. Exiting.")
        return

    log_info("Starting Telegram Listener...")
    
    # Start the client and ensure authentication
    await client.start(phone=config.TELEGRAM_PHONE)
    log_info("Telegram Client Started and Authenticated.")
    
    # 🌟 NEW: Fetch and post the status of the last 3 positions immediately on startup
    last_positions = get_last_positions(3)
    if last_positions:
        response = "🤖 **Bot Startup Status**\n\n**Current Last 3 Positions:**\n"
        for pos in last_positions:
            response += f"• [{pos['status']}] {pos['type']} {pos['symbol']} | Lots: {pos['volume']} | Price: {pos['price_open']} | Profit: {pos['profit']}\n"
        # Since we are starting up, we post to the first configured channel if possible
        if config.TELEGRAM_CHANNELS:
            try:
                await client.send_message(config.TELEGRAM_CHANNELS[0], response)
                log_info("Posted last 3 positions status to channel.")
            except Exception as e:
                log_error(f"Could not post startup status to channel: {e}")
    else:
        log_info("No active positions found on startup.")

    # Keep the script running until disconnected (Continuous 24/7 monitoring)
    log_info("Monitoring for new signals 24/7...")
    await client.run_until_disconnected()

if __name__ == "__main__":
    while True:
        try:
            # Run the event loop
            asyncio.run(main())
        except KeyboardInterrupt:
            log_info("Bot stopped by user via keyboard.")
            break
        except Exception as e:
            log_error(f"CRITICAL ERROR: {e}. Attempting to restart in 10 seconds...")
            import time
            time.sleep(10)
        finally:
            # Gracefully shutdown MT5 connection
            mt5.shutdown()
            log_info("MT5 connection closed.")
