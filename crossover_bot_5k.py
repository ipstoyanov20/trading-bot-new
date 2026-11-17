"""
Classic Bollinger Bands signal bot for XAUUSD (M5) with an AI second opinion.

SIGNAL ONLY by default: the bot never places trades (AUTO_TRADE = False).
It tells you on your phone what the classic strategy says, with entry, stop loss,
take profit, lot size for your risk, a fixed rule checklist and an AI review.
You place the trade yourself in MetaTrader.

Classic Bollinger Bands mean reversion:
  BUY : the completed candle touched/pierced the LOWER band and CLOSED back inside
  SELL: the completed candle touched/pierced the UPPER band and CLOSED back inside
  Take profit: the MIDDLE band (20 SMA)
  Stop loss  : just beyond the signal candle's extreme (low for BUY, high for SELL)

Requirements:  pip install MetaTrader5 pandas requests
AI (optional): set the environment variable ANTHROPIC_API_KEY
"""

import os
import json
import time
import logging
import math
import subprocess
import threading
import xml.sax.saxutils as saxutils

import MetaTrader5 as mt5
import pandas as pd
import requests

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# =====================================================================
# SETTINGS
# =====================================================================
SYMBOL = "XAUUSD"
TIMEFRAME = mt5.TIMEFRAME_M5
BB_PERIOD = 20                  # Classic: 20
BB_DEVIATION = 2.0              # Classic: 2.0
RSI_PERIOD = 14
SLEEP_INTERVAL = 1              # Check every second

# --- Trade plan ---
SL_BUFFER = 0.50                # $ gold price beyond the signal candle's extreme
RISK_PER_TRADE_USD = 50.0       # 1% of $5,000. Lot size is calculated from this.
MAX_LOT = 1.00
ACCOUNT_SIZE = 5000.0
DAILY_LOSS_LIMIT_USD = 200.0    # Shown in the message as a reminder

# --- Execution ---
AUTO_TRADE = False              # Keep False: signals only, you trade manually
MAGIC_NUMBER = 60030
DEVIATION = 20

# --- Notifications ---
ENABLE_WINDOWS_TOAST = True
ENABLE_NTFY_PUSH = True
NTFY_TOPIC = "tradingbot_5k_ivaylo"     # Install the ntfy app and subscribe to this topic

# --- AI review (Claude API) ---
AI_ENABLED = True
AI_MODEL = "claude-sonnet-5-5"
AI_CANDLES = 60                 # How many past candles the AI sees
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

last_signal_candle = None
last_wait_log = 0


# =====================================================================
# NOTIFICATIONS
# =====================================================================
def notify(title, message):
    logger.warning(f"📢 {title}\n{message}")

    def _deliver():
        if ENABLE_WINDOWS_TOAST:
            try:
                t = saxutils.escape(title).replace("'", "")
                m = saxutils.escape(message[:200]).replace("'", "")
                ps = f'''
                [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
                $tpl = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
                $n = $tpl.GetElementsByTagName('text')
                $n.Item(0).InnerText = '{t}'
                $n.Item(1).InnerText = '{m}'
                $toast = [Windows.UI.Notifications.ToastNotification]::new($tpl)
                [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}}\\WindowsPowerShell\\v1.0\\powershell.exe').Show($toast)
                '''
                subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, timeout=5)
            except Exception as e:
                logger.debug(f"Toast error: {e}")
        if ENABLE_NTFY_PUSH and NTFY_TOPIC:
            try:
                requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=message.encode("utf-8"),
                              headers={"Title": title.encode("utf-8"), "Tags": "chart_with_upwards_trend"},
                              timeout=5)
            except Exception as e:
                logger.debug(f"ntfy error: {e}")

    threading.Thread(target=_deliver, daemon=True).start()


# =====================================================================
# DATA & INDICATORS
# =====================================================================
def get_candles(count=200):
    rates = mt5.copy_rates_from_pos(SYMBOL, TIMEFRAME, 0, count)
    if rates is None or len(rates) < BB_PERIOD + 60:
        return pd.DataFrame()
    df = pd.DataFrame(rates)
    df['time'] = pd.to_datetime(df['time'], unit='s')
    df['middle'] = df['close'].rolling(BB_PERIOD).mean()
    std = df['close'].rolling(BB_PERIOD).std(ddof=0)
    df['upper'] = df['middle'] + BB_DEVIATION * std
    df['lower'] = df['middle'] - BB_DEVIATION * std
    df['width'] = df['upper'] - df['lower']

    delta = df['close'].diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / RSI_PERIOD, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / RSI_PERIOD, adjust=False).mean()
    df['rsi'] = 100 - 100 / (1 + gain / loss.replace(0, 1e-10))
    return df


# =====================================================================
# CLASSIC SIGNAL
# =====================================================================
def classic_signal(df):
    """Checks the last COMPLETED candle. Returns 'BUY', 'SELL' or None."""
    c = df.iloc[-2]
    touched_lower = c['low'] <= c['lower'] and c['close'] > c['lower']
    touched_upper = c['high'] >= c['upper'] and c['close'] < c['upper']
    if touched_lower and not touched_upper:
        return 'BUY'
    if touched_upper and not touched_lower:
        return 'SELL'
    return None


def build_plan(df, side):
    """Entry, stop loss, take profit, lot size and $ risk/reward."""
    c = df.iloc[-2]
    tick = mt5.symbol_info_tick(SYMBOL)
    info = mt5.symbol_info(SYMBOL)
    if tick is None or info is None:
        return None
    contract = getattr(info, 'trade_contract_size', 100.0) or 100.0
    step = getattr(info, 'volume_step', 0.01) or 0.01
    vmin = getattr(info, 'volume_min', 0.01) or 0.01

    if side == 'BUY':
        entry = tick.ask
        sl = c['low'] - SL_BUFFER
        tp = c['middle']
        sl_dist, tp_dist = entry - sl, tp - entry
    else:
        entry = tick.bid
        sl = c['high'] + SL_BUFFER
        tp = c['middle']
        sl_dist, tp_dist = sl - entry, entry - tp

    if sl_dist <= 0 or tp_dist <= 0:
        return None     # price already past the stop or the target

    lot = math.floor(RISK_PER_TRADE_USD / (sl_dist * contract) / step) * step
    lot = round(min(max(lot, vmin), MAX_LOT), 2)
    return {
        'side': side, 'entry': entry, 'sl': sl, 'tp': tp, 'lot': lot,
        'risk_usd': sl_dist * contract * lot,
        'reward_usd': tp_dist * contract * lot,
        'rr': tp_dist / sl_dist,
        'candle_time': c['time'],
    }


# =====================================================================
# FIXED RULE CHECKLIST (no emotions, same rules every time)
# =====================================================================
def rule_checklist(df, plan):
    c = df.iloc[-2]
    side = plan['side']
    prev5 = df.iloc[-7:-2]
    if side == 'BUY':
        walk = int((prev5['close'] < prev5['lower']).sum())
        wick = min(c['open'], c['close']) - c['low']
        rsi_ok = c['rsi'] <= 35
    else:
        walk = int((prev5['close'] > prev5['upper']).sum())
        wick = c['high'] - max(c['open'], c['close'])
        rsi_ok = c['rsi'] >= 65
    body = abs(c['close'] - c['open'])
    avg_width = df['width'].iloc[-52:-2].mean()
    slope = df['middle'].iloc[-2] - df['middle'].iloc[-12]

    checks = {
        "R:R to middle band >= 1.0": plan['rr'] >= 1.0,
        "No band walk (max 1 of last 5 candles closed outside)": walk <= 1,
        "Bands not expanding hard (width <= 1.5x average)": c['width'] <= 1.5 * avg_width,
        f"RSI confirms ({c['rsi']:.0f})": bool(rsi_ok),
        "Rejection wick >= candle body": wick >= body,
    }
    score = sum(checks.values())
    verdict = "TAKE" if score >= 4 and checks["R:R to middle band >= 1.0"] else "SKIP"
    facts = {
        'rsi': round(float(c['rsi']), 1),
        'band_width': round(float(c['width']), 2),
        'avg_band_width_50': round(float(avg_width), 2),
        'middle_slope_10_candles': round(float(slope), 2),
        'closes_outside_band_last5': walk,
    }
    return checks, score, verdict, facts


# =====================================================================
# AI REVIEW (Claude API)
# =====================================================================
AI_SYSTEM = (
    "You are a disciplined, rule-based trading reviewer for a classic Bollinger Bands (20, 2) "
    "mean-reversion strategy on XAUUSD M5. You do not predict the future and you never use emotional "
    "language. Judge only from the candles and numbers provided. Check: trend and band walk against the "
    "signal, squeeze or expansion of the bands, RSI, the signal candle's wick, recent support/resistance in "
    "the candles, and the risk:reward to the middle band. The trader has a $5,000 prop account with a $200 "
    "daily loss limit. Reply with JSON only, no other text: "
    '{"decision": "TAKE" or "SKIP", "confidence": 1-5, '
    '"reasons": ["short reason", "..."], "invalidation": "what would prove the trade wrong", '
    '"summary_bg": "one or two sentences in Bulgarian"}'
)


def ai_review(df, plan, checks, facts):
    if not AI_ENABLED or not ANTHROPIC_API_KEY:
        return None
    cols = ['time', 'open', 'high', 'low', 'close', 'upper', 'middle', 'lower', 'rsi']
    hist = df.iloc[-(AI_CANDLES + 2):-1][cols].copy()
    hist['time'] = hist['time'].dt.strftime('%m-%d %H:%M')
    candles_csv = hist.round(2).to_csv(index=False)

    user_msg = (
        f"Signal: {plan['side']} on the candle closed at {plan['candle_time']}.\n"
        f"Plan: entry {plan['entry']:.2f}, stop {plan['sl']:.2f}, target {plan['tp']:.2f} (middle band), "
        f"lot {plan['lot']}, risk ${plan['risk_usd']:.2f}, reward ${plan['reward_usd']:.2f}, R:R {plan['rr']:.2f}.\n"
        f"Rule checklist: {json.dumps({k: bool(v) for k, v in checks.items()})}\n"
        f"Facts: {json.dumps(facts)}\n\n"
        f"Last {AI_CANDLES} completed M5 candles (oldest first):\n{candles_csv}"
    )
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": AI_MODEL, "max_tokens": 600, "system": AI_SYSTEM,
                  "messages": [{"role": "user", "content": user_msg}]},
            timeout=60,
        )
        r.raise_for_status()
        text = "".join(b.get("text", "") for b in r.json().get("content", []))
        start, end = text.find("{"), text.rfind("}")
        return json.loads(text[start:end + 1])
    except Exception as e:
        logger.error(f"AI review failed: {e}")
        return {"error": str(e)}


# =====================================================================
# OPTIONAL AUTO TRADE (keep AUTO_TRADE = False on FundedNext without the EA add-on)
# =====================================================================
def place_trade(plan):
    info = mt5.symbol_info(SYMBOL)
    digits = getattr(info, 'digits', 2)
    req = {
        "action": mt5.TRADE_ACTION_DEAL, "symbol": SYMBOL, "volume": float(plan['lot']),
        "type": mt5.ORDER_TYPE_BUY if plan['side'] == 'BUY' else mt5.ORDER_TYPE_SELL,
        "price": plan['entry'], "sl": round(plan['sl'], digits), "tp": round(plan['tp'], digits),
        "magic": MAGIC_NUMBER, "comment": f"BB_{plan['side']}", "deviation": DEVIATION,
        "type_time": mt5.ORDER_TIME_GTC,
    }
    for filling in (mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_RETURN):
        req["type_filling"] = filling
        res = mt5.order_send(req)
        if res and res.retcode == mt5.TRADE_RETCODE_DONE:
            logger.info(f"✅ Order placed: {plan['side']} {plan['lot']}")
            return True
        if not res or res.retcode != 10030:
            break
    logger.error(f"Order failed: {mt5.last_error() if not res else res.comment}")
    return False


# =====================================================================
# MESSAGE
# =====================================================================
def format_message(plan, checks, score, verdict, ai):
    lines = [
        f"{plan['side']} {SYMBOL}  (candle {plan['candle_time']:%H:%M})",
        f"Entry ~ {plan['entry']:.2f}",
        f"SL {plan['sl']:.2f}  |  TP {plan['tp']:.2f} (middle band)",
        f"Lot {plan['lot']}  |  Risk ${plan['risk_usd']:.0f}  |  Reward ${plan['reward_usd']:.0f}  |  R:R {plan['rr']:.2f}",
        "",
        f"Rules {score}/5 -> {verdict}",
    ]
    lines += [f"{'OK ' if ok else 'NO '} {name}" for name, ok in checks.items()]
    if ai and "error" not in ai:
        lines += ["", f"AI: {ai.get('decision')} (confidence {ai.get('confidence')}/5)",
                  ai.get('summary_bg', '')]
        if ai.get('invalidation'):
            lines.append(f"Invalid if: {ai['invalidation']}")
    elif ai:
        lines += ["", "AI review unavailable this time."]
    lines += ["", f"Daily loss limit ${DAILY_LOSS_LIMIT_USD:.0f}: max {int(DAILY_LOSS_LIMIT_USD // RISK_PER_TRADE_USD)} losing trades today."]
    return "\n".join(lines)


# =====================================================================
# MAIN LOOP
# =====================================================================
def iteration():
    global last_signal_candle, last_wait_log
    df = get_candles()
    if df.empty or pd.isna(df.iloc[-2]['upper']):
        return

    candle_time = df.iloc[-2]['time']
    if candle_time == last_signal_candle:
        return

    side = classic_signal(df)
    if side is None:
        if time.time() - last_wait_log >= 60:
            last_wait_log = time.time()
            c = df.iloc[-2]
            logger.info(f"⏳ No signal | Close {c['close']:.2f} | Upper {c['upper']:.2f}, "
                        f"Mid {c['middle']:.2f}, Lower {c['lower']:.2f}")
        return

    last_signal_candle = candle_time
    plan = build_plan(df, side)
    if plan is None:
        logger.info(f"{side} signal on {candle_time} skipped: price is already past the stop or the target.")
        return

    checks, score, verdict, facts = rule_checklist(df, plan)
    ai = ai_review(df, plan, checks, facts)
    final = ai.get('decision', verdict) if ai and "error" not in ai else verdict
    notify(f"{'🟢' if side == 'BUY' else '🔴'} BB {side} signal - {final}",
           format_message(plan, checks, score, verdict, ai))

    if AUTO_TRADE and verdict == "TAKE":
        place_trade(plan)


def run():
    while not mt5.initialize():
        logger.error(f"MT5 init failed: {mt5.last_error()}. Retrying in 10s...")
        time.sleep(10)
    mt5.symbol_select(SYMBOL, True)
    logger.info(f"🚀 Classic Bollinger signal bot | {SYMBOL} M5 BB({BB_PERIOD},{BB_DEVIATION}) | "
                f"AUTO_TRADE={AUTO_TRADE} | AI={'on' if AI_ENABLED and ANTHROPIC_API_KEY else 'off'}")
    notify("🚀 BB signal bot online", f"Watching {SYMBOL} M5. Signals only, you place the trades.")

    try:
        while True:
            try:
                if mt5.terminal_info() is None:
                    logger.warning("MT5 disconnected, reconnecting...")
                    mt5.shutdown()
                    if not mt5.initialize():
                        time.sleep(10)
                        continue
                iteration()
            except Exception as e:
                logger.error(f"Error (bot continues): {e}", exc_info=True)
                time.sleep(5)
            time.sleep(SLEEP_INTERVAL)
    except KeyboardInterrupt:
        logger.info("Stopped by user.")
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    run()
