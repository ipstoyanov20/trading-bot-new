import asyncio
from telethon import TelegramClient
import config
from logger import log_info, log_error

client = TelegramClient('trading_bot_session', config.TELEGRAM_API_ID, config.TELEGRAM_API_HASH)

async def main():
    log_info("Connecting to Telegram to list your channels...")
    await client.start(phone=config.TELEGRAM_PHONE)
    
    log_info("--- YOUR CHANNELS AND GROUPS ---")
    async for dialog in client.iter_dialogs():
        if dialog.is_channel or dialog.is_group:
            log_info(f"NAME: {dialog.name} | ID: {dialog.id}")
    log_info("--------------------------------")
    await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
