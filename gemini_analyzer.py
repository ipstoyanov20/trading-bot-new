import google.generativeai as genai
import logging
from firebase_logger import get_recent_trades

logger = logging.getLogger(__name__)

try:
    import config_secrets
    GEMINI_API_KEY = config_secrets.GEMINI_API_KEY
except ImportError:
    import os
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

genai.configure(api_key=GEMINI_API_KEY)

# Use the latest model
model = genai.GenerativeModel('gemini-1.5-pro')

def analyze_market(trend, curr_k, curr_d):
    """
    Sends the recent trade history and live market data to Gemini for analysis.
    Returns the action recommended by Gemini.
    """
    try:
        trades = get_recent_trades(limit=10)
        
        prompt = f"""
        You are an elite automated trading AI specializing in Gold (XAUUSD) 1-minute scalping.
        
        Live Market Data:
        - Trend (50 EMA vs 200 EMA): {trend}
        - Stochastic K: {curr_k}
        - Stochastic D: {curr_d}
        
        Recent Trades from Database:
        {trades if trades else "No recent trades recorded."}
        
        Based on the current live data and the historical performance of the recent trades, decide whether to open a batch of 5 new positions.
        - If the trend is strong and recent trades were successful, recommend the direction of the trend (BUY or SELL).
        - If recent trades have been losing heavily or the market is flat, recommend HOLD.
        
        Respond with ONLY ONE WORD: BUY, SELL, or HOLD.
        """
        
        response = model.generate_content(prompt)
        decision = response.text.strip().upper()
        
        if decision in ['BUY', 'SELL', 'HOLD']:
            logger.info(f"Gemini Analysis complete. Recommended Action: {decision}")
            return decision
        else:
            logger.warning(f"Gemini returned unexpected response: {decision}")
            return 'HOLD'
            
    except Exception as e:
        logger.error(f"Gemini Analysis failed: {e}")
        return 'HOLD'
