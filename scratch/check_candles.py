import asyncio
import config
from revolut_x_bot import RevolutXBot

async def check():
    bot = RevolutXBot()
    df = bot.fetch_candles()
    if df is not None:
        print("Columns:", df.columns.tolist())
        print("First few rows:\n", df.head())
    else:
        print("Failed to fetch candles")

if __name__ == "__main__":
    asyncio.run(check())
