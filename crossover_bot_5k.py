import os
import MetaTrader5 as mt5
import time
import logging
from datetime import datetime
import pandas as pd
import subprocess
import threading
import xml.sax.saxutils as saxutils
import requests

import config

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Account & Challenge Parameters ($5,000 Challenge Account) ---
ACCOUNT_SIZE = 5000.0               # Challenge Account Size ($5,000)
PHASE_1_TARGET_USD = 400.0          # Phase 1 Target: 8% (+$400.00)
PHASE_2_TARGET_USD = 200.0          # Phase 2 Target: 4% (+$200.00)
MAX_DAILY_LOSS_USD = 200.0          # Daily Loss Limit: 4% (-$200.00) - MONITORING ONLY
MAX_TOTAL_LOSS_USD = 400.0          # Maximum Loss Limit: 8% (-$400.00) - MONITORING ONLY
MAX_RISK_AT_ANY_TIME_USD = 150.0    # Max Risk at any time: -$150.00 limit - MONITORING ONLY

# --- Push Notification Settings ---
ENABLE_WINDOWS_TOAST = True            # Send native Windows desktop push notifications
ENABLE_NTFY_PUSH = True                # Send instant mobile push notifications via ntfy.sh
NTFY_TOPIC = "tradingbot_5k_ivaylo"    # Topic for ntfy mobile app (https://ntfy.sh/tradingbot_5k_ivaylo)

# --- Trading Strategy & Symbol Settings ---
SYMBOL = "XAUUSD"                   # Gold Bot
TIMEFRAME = mt5.TIMEFRAME_M5        # M5 Timeframe
SLEEP_INTERVAL = 1                  # 1 second loop interval
COOLDOWN_SECONDS = 60               # Cooldown between completed trades
MAGIC_NUMBER = 60020                # Unique identifier for this bot's orders
DEVIATION = 20                      # Maximum price slippage in points

# --- Strategy Parameters: Bollinger Bands ---
BB_PERIOD = 20                      # Period 20
BB_DEVIATION = 2.0                  # Deviation 2.0
# Applied Price: Close
AUTHORIZED_ORDER_TYPE = "ALL"       # Authorized order type: ALL (BUY and SELL)

# --- Entry Logic (breakout -> reversal candle back inside) ---
# Step 1: a COMPLETED M5 candle CLOSES OUTSIDE a band.
#         close above UPPER -> SELL setup armed
#         close below LOWER -> BUY setup armed
# Step 2: the NEXT completed candle (the reversal candle) must:
#         SELL: OPEN above the UPPER band AND CLOSE back below the UPPER band
#         BUY:  OPEN below the LOWER band AND CLOSE back above the LOWER band
#         -> valid signal, trade opens at the start of the following candle.
# Other outcomes:
#         SELL: reversal candle still closes above UPPER -> stays armed (waits for another candle)
#         SELL: reversal candle OPENS inside the band      -> setup cancelled (re-checked as a new breakout)
#         BUY: mirrored.

# --- Trade Size & Stop Loss ---
TRADE_VOLUME = 0.10                 # Lot size of the trade
STOP_LOSS_USD = 60.0                # Broker stop loss in account dollars from the entry price
MAX_LOSS_USD = 60.0                 # Software backup stop (broker SL normally closes first)

# --- Take Profit ---
#   BUY  closes when price reaches the UPPER band
#   SELL closes when price reaches the LOWER band
# The middle band does not affect the exit.

# --- Stop Behaviour ---
# When the broker stop loss is hit (or the backup stop closes the trade), the bot STOPS.
# It does not reopen trades until it is started again manually.

# --- Risk Limits (MONITORING ONLY, the bot never stops because of these) ---
# Max loss per trade: -$60 (the stop loss above)

# --- Bot Runtime State ---
last_close_time = 0                 # Timestamp of last closed trade
last_eval_candle_time = None        # Last completed candle the signal logic evaluated
armed_side = None                   # 'SELL', 'BUY' or None (breakout waiting for reversal)
last_status_log_time = 0            # Timestamp of periodic open trade status log
last_wait_log_time = 0              # Timestamp of last "waiting" log
known_tickets = set()               # Tickets seen on the previous loop (detects broker SL closes)
trade_info = {}                     # {ticket: (direction, last P&L)} for detecting closes by broker
stop_bot_reason = None              # Set when a stop loss is hit; the main loop exits when this is set
phase1_announced = False            # Prevents Phase 1 banner from logging every second

# --- Limit Monitoring & Safety State Flags ---
limit_notified_daily = False        # Has daily loss limit been notified?
limit_notified_total = False        # Has total loss limit been notified?
limit_notified_max_risk = False     # Has max risk limit been notified?


def send_push_notification(title, message):
    """
    Sends push notifications across configured channels:
    1. Desktop: Native Windows Toast notification.
    2. Mobile/Browser: ntfy.sh (https://ntfy.sh/{NTFY_TOPIC}).
    3. MT5 mobile app via PushNotificationBridge common file.
    Runs in a background thread to avoid blocking trading execution.
    """
    logger.warning(f"📢 PUSH NOTIFICATION: {title} | {message}")

    def _deliver():
        # 1. Desktop Windows Toast Notification
        if ENABLE_WINDOWS_TOAST:
            try:
                clean_title = saxutils.escape(str(title))
                clean_message = saxutils.escape(str(message))
                ps_cmd = f'''
                [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
                $template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
                $textNodes = $template.GetElementsByTagName('text')
                $textNodes.Item(0).InnerText = '{clean_title}'
                $textNodes.Item(1).InnerText = '{clean_message}'
                $toast = [Windows.UI.Notifications.ToastNotification]::new($template)
                $notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}}\\\\WindowsPowerShell\\\\v1.0\\\\powershell.exe')
                $notifier.Show($toast)
                '''
                subprocess.run(["powershell", "-NoProfile", "-Command", ps_cmd], capture_output=True, timeout=5)
            except Exception as e:
                logger.debug(f"Windows toast error: {e}")

        # 2. Mobile / Web Push Notification via ntfy.sh
        if ENABLE_NTFY_PUSH and NTFY_TOPIC:
            try:
                requests.post(
                    f"https://ntfy.sh/{NTFY_TOPIC}",
                    data=message.encode('utf-8'),
                    headers={
                        "Title": title.encode('utf-8'),
                        "Tags": "robot,chart_with_upwards_trend"
                    },
                    timeout=5
                )
            except Exception as e:
                logger.debug(f"ntfy push error: {e}")

        # 3. MetaTrader Mobile App "Messages" Tab (via MQL5 PushNotificationBridge)
        try:
            common_files_dir = os.path.expandvars(r"%APPDATA%\MetaQuotes\Terminal\Common\Files")
            if os.path.exists(common_files_dir):
                notif_path = os.path.join(common_files_dir, "push_notification.txt")
                clean_notif = f"{title}: {message}"
                clean_notif = clean_notif.encode('ascii', 'ignore').decode('ascii').strip()
                if not clean_notif:
                    clean_notif = f"{title}: {message}"
                if len(clean_notif) > 245:
                    clean_notif = clean_notif[:242] + "..."
                with open(notif_path, "w", encoding="utf-8") as f:
                    f.write(clean_notif)
        except Exception as e:
            logger.debug(f"MT5 common file error: {e}")

    threading.Thread(target=_deliver, daemon=True).start()

# Backward-compatible alias
send_mt5_notification = send_push_notification


def get_filling_type(symbol):
    """Dynamically determines the correct execution filling mode supported by the broker."""
    symbol_info = mt5.symbol_info(symbol)
    if symbol_info is None:
        return mt5.ORDER_FILLING_FOK

    filling_mode = getattr(symbol_info, 'filling_mode', 0)
    if filling_mode & 1:  # FOK supported
        return mt5.ORDER_FILLING_FOK
    elif filling_mode & 2:  # IOC supported
        return mt5.ORDER_FILLING_IOC
    else:
        return mt5.ORDER_FILLING_RETURN


def normalize_volume(symbol_info, volume):
    """Rounds volume to the broker's volume step and clamps it to min/max allowed lots."""
    step = getattr(symbol_info, 'volume_step', 0.01) or 0.01
    vmin = getattr(symbol_info, 'volume_min', 0.01) or 0.01
    vmax = getattr(symbol_info, 'volume_max', volume) or volume
    vol = round(round(volume / step) * step, 2)
    if vol > vmax:
        logger.warning(f"⚠️ Requested volume {volume} exceeds broker max {vmax}. Clamping to {vmax}.")
        vol = vmax
    if 0 < vol < vmin:
        vol = vmin
    return vol


def get_contract_size(symbol_info):
    """Ounces per lot (100 for XAUUSD on most brokers)."""
    return getattr(symbol_info, 'trade_contract_size', 100.0) or 100.0


def get_position_pnl(pos):
    """Floating P&L of an open position including swap and commission."""
    return pos.profit + pos.swap + getattr(pos, 'commission', 0.0)


def get_bollinger_bands(symbol, timeframe, period=20, deviation=2.0, count=60):
    """
    Fetches OHLC data and calculates Bollinger Bands:
    period=20, deviation=2.0, shift=0, applied price=Close.
    """
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, count)
    if rates is None or len(rates) < period + 5:
        return pd.DataFrame()

    df = pd.DataFrame(rates)
    df['time'] = pd.to_datetime(df['time'], unit='s')

    df['middle'] = df['close'].rolling(window=period).mean()
    df['std'] = df['close'].rolling(window=period).std(ddof=0)
    df['upper'] = df['middle'] + deviation * df['std']
    df['lower'] = df['middle'] - deviation * df['std']
    return df


def check_bb_entry_signal(symbol):
    """
    Breakout + reversal candle logic. Evaluated once per COMPLETED M5 candle (iloc[-2]).
    - Breakout: candle closes above UPPER -> arm SELL; closes below LOWER -> arm BUY.
    - Reversal (SELL): next completed candle OPENS above UPPER and CLOSES below UPPER -> SELL signal.
      Still closes above UPPER -> stays armed. Opens inside the band -> cancelled.
    - Reversal (BUY): mirrored with LOWER.
    Returns: (signal, candle_time) where signal is 'BUY', 'SELL' or None.
    """
    global armed_side, last_eval_candle_time, last_wait_log_time

    df = get_bollinger_bands(symbol, TIMEFRAME, period=BB_PERIOD, deviation=BB_DEVIATION, count=60)
    if df.empty or len(df) < BB_PERIOD + 2:
        return None, None

    c = df.iloc[-2]     # last COMPLETED candle
    candle_time = c['time']

    if pd.isna(c['upper']) or pd.isna(c['lower']):
        return None, candle_time

    # Each completed candle is evaluated exactly once
    if candle_time == last_eval_candle_time:
        return None, candle_time
    last_eval_candle_time = candle_time

    o, cl = c['open'], c['close']
    up, lo, mid = c['upper'], c['lower'], c['middle']
    signal = None

    # 1. Check the reversal candle against an armed setup
    if armed_side == 'SELL':
        if o > up and cl < up:
            signal = 'SELL'
            logger.info(
                f"🔴 SELL Signal: reversal candle [{candle_time}] opened above upper {up:.2f} "
                f"and closed back below it (Open {o:.2f}, Close {cl:.2f})"
            )
            armed_side = None
        elif o > up and cl >= up:
            logger.info(f"⏳ Reversal candle [{candle_time}] still above upper {up:.2f}. SELL setup stays armed.")
        else:
            logger.info(f"↩️ Candle [{candle_time}] opened inside upper band {up:.2f}. SELL setup cancelled.")
            armed_side = None

    elif armed_side == 'BUY':
        if o < lo and cl > lo:
            signal = 'BUY'
            logger.info(
                f"🟢 BUY Signal: reversal candle [{candle_time}] opened below lower {lo:.2f} "
                f"and closed back above it (Open {o:.2f}, Close {cl:.2f})"
            )
            armed_side = None
        elif o < lo and cl <= lo:
            logger.info(f"⏳ Reversal candle [{candle_time}] still below lower {lo:.2f}. BUY setup stays armed.")
        else:
            logger.info(f"↩️ Candle [{candle_time}] opened inside lower band {lo:.2f}. BUY setup cancelled.")
            armed_side = None

    # 2. Breakout check (only when no setup is armed and no signal was just produced)
    if signal is None and armed_side is None:
        if cl > up and AUTHORIZED_ORDER_TYPE in ["ALL", "SELL"]:
            armed_side = 'SELL'
            logger.info(
                f"👀 Breakout UP: candle [{candle_time}] closed above upper {up:.2f} "
                f"(Close {cl:.2f}). Waiting for a reversal candle."
            )
        elif cl < lo and AUTHORIZED_ORDER_TYPE in ["ALL", "BUY"]:
            armed_side = 'BUY'
            logger.info(
                f"👀 Breakout DOWN: candle [{candle_time}] closed below lower {lo:.2f} "
                f"(Close {cl:.2f}). Waiting for a reversal candle."
            )

    if signal is None and armed_side is None:
        now = time.time()
        if now - last_wait_log_time >= 15:
            last_wait_log_time = now
            logger.info(
                f"⏳ Waiting for breakout | Last candle [{candle_time}] Close {cl:.2f} | "
                f"Upper {up:.2f}, Mid {mid:.2f}, Lower {lo:.2f}"
            )

    return signal, candle_time


def place_order_safe(symbol, order_type, volume, sl_price=0.0, comment="BB_Single", notify_on_error=True):
    """Places a market order with an optional stop loss PRICE and filling mode fallback. Returns (result, fill_price)."""
    action_name = "BUY" if order_type == mt5.ORDER_TYPE_BUY else "SELL"
    symbol_info = mt5.symbol_info(symbol)
    if not symbol_info:
        err_msg = f"Symbol {symbol} not found."
        logger.error(err_msg)
        if notify_on_error:
            send_push_notification(
                f"❌ Trade Execution Failed ({action_name})",
                f"Failed to execute {action_name} {volume} lots on {symbol} ({comment}). Error: {err_msg}"
            )
        return None, 0.0

    if not symbol_info.visible:
        mt5.symbol_select(symbol, True)

    volume = normalize_volume(symbol_info, volume)

    tick = mt5.symbol_info_tick(symbol)
    if not tick:
        err_msg = f"Failed to get tick for {symbol}"
        logger.error(err_msg)
        if notify_on_error:
            send_push_notification(
                f"❌ Trade Execution Failed ({action_name})",
                f"Failed to execute {action_name} {volume} lots on {symbol} ({comment}). Error: {err_msg}"
            )
        return None, 0.0

    price = tick.ask if order_type == mt5.ORDER_TYPE_BUY else tick.bid
    digits = getattr(symbol_info, 'digits', 2)
    sl = round(sl_price, digits) if sl_price else 0.0

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": float(volume),
        "type": order_type,
        "price": price,
        "sl": float(sl),
        "tp": 0.0,                      # Take profit is managed by the bot
        "magic": MAGIC_NUMBER,
        "comment": comment,
        "deviation": DEVIATION,
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": get_filling_type(symbol),
    }

    modes_to_try = [get_filling_type(symbol), mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_RETURN]
    unique_modes = []
    for m in modes_to_try:
        if m not in unique_modes:
            unique_modes.append(m)

    res = None
    for mode in unique_modes:
        request["type_filling"] = mode
        res = mt5.order_send(request)
        if res and res.retcode in [mt5.TRADE_RETCODE_DONE, 10008, 0]:
            logger.info(f"✅ {action_name} Placed ({comment}) | Vol: {volume} | Price: {price} | SL: {sl} | Filling: {mode}")
            return res, price
        elif res and res.retcode in [10030, getattr(mt5, 'TRADE_RETCODE_UNSUPPORTED_FILLING_MODE', 10030)]:
            continue
        else:
            break

    err = mt5.last_error() if not res else f"{res.comment} (Code: {res.retcode})"
    logger.error(f"Failed to place order ({comment}): {err} | Request: {request}")
    if notify_on_error:
        send_push_notification(
            f"❌ Trade Execution Failed ({action_name})",
            f"Failed to execute {action_name} {volume} lots on {symbol} ({comment}). Error: {err}"
        )
    return None, 0.0


def get_active_positions(symbol):
    """Returns open positions belonging to this bot's MAGIC_NUMBER, oldest first."""
    positions = mt5.positions_get(symbol=symbol)
    if not positions:
        return []
    bot_positions = [p for p in positions if p.magic == MAGIC_NUMBER]
    bot_positions.sort(key=lambda p: (p.time_msc if hasattr(p, 'time_msc') else p.time, p.ticket))
    return bot_positions


def close_position(symbol, position):
    """Closes a single MT5 position safely."""
    tick = mt5.symbol_info_tick(symbol)
    if not tick:
        return False

    close_type = mt5.ORDER_TYPE_SELL if position.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY
    price = tick.bid if position.type == mt5.POSITION_TYPE_BUY else tick.ask

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": position.volume,
        "type": close_type,
        "position": position.ticket,
        "price": price,
        "deviation": DEVIATION,
        "magic": MAGIC_NUMBER,
        "comment": f"Close #{position.ticket}",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": get_filling_type(symbol),
    }

    modes_to_try = [get_filling_type(symbol), mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_RETURN]
    unique_modes = []
    for m in modes_to_try:
        if m not in unique_modes:
            unique_modes.append(m)

    res = None
    for mode in unique_modes:
        request["type_filling"] = mode
        res = mt5.order_send(request)
        if res and res.retcode in [mt5.TRADE_RETCODE_DONE, 10008, 0]:
            logger.info(f"Closed #{position.ticket} at {price} (Profit: ${get_position_pnl(position):.2f})")
            return True
        elif res and res.retcode in [10030, getattr(mt5, 'TRADE_RETCODE_UNSUPPORTED_FILLING_MODE', 10030)]:
            continue
        else:
            break

    err = mt5.last_error() if not res else f"{res.comment} (Code: {res.retcode})"
    logger.error(f"Failed to close #{position.ticket}: {err}")
    return False


def close_position_with_retry(symbol, position, reason=""):
    """Closes one position, retrying up to 5 times."""
    for attempt in range(5):
        still_open = [p for p in get_active_positions(symbol) if p.ticket == position.ticket]
        if not still_open:
            return True
        logger.info(f"Closing #{position.ticket} ({reason}) [Attempt {attempt + 1}/5]...")
        close_position(symbol, still_open[0])
        time.sleep(0.2)

    if any(p.ticket == position.ticket for p in get_active_positions(symbol)):
        send_push_notification(
            "⚠️ Close Failed",
            f"#{position.ticket} still open on {symbol} after 5 attempts ({reason}). Check MT5 now!"
        )
        return False
    return True


def get_daily_realized_pnl(magic=MAGIC_NUMBER):
    """Calculates total closed PnL today (since 00:00 local time) for this bot's magic number."""
    now = datetime.now()
    today_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    deals = mt5.history_deals_get(today_midnight, now)
    if not deals:
        return 0.0

    daily_pnl = 0.0
    for d in deals:
        if d.magic == magic and d.entry == mt5.DEAL_ENTRY_OUT:
            daily_pnl += (d.profit + d.swap + getattr(d, 'commission', 0.0))

    return daily_pnl


def get_total_closed_pnl(magic=MAGIC_NUMBER):
    """Calculates total cumulative closed PnL for Phase 1 target tracking."""
    start_date = datetime(2026, 1, 1)
    now = datetime.now()
    deals = mt5.history_deals_get(start_date, now)
    if not deals:
        return 0.0

    total_pnl = 0.0
    for d in deals:
        if d.magic == magic and d.entry == mt5.DEAL_ENTRY_OUT:
            total_pnl += (d.profit + d.swap + getattr(d, 'commission', 0.0))

    return total_pnl


def calculate_stop_loss(symbol, direction, volume, stop_usd):
    """
    Dynamic stop loss from the ENTRY price (current tick):
      BUY:  SL = entry - distance
      SELL: SL = entry + distance
    distance = stop_usd / (volume * contract size)
    e.g. 0.10 lots, 100 oz: $60 / (0.10 * 100) = $6.00 gold price distance.
    Returns (sl_price, expected_loss_usd) or (None, reason) if no valid SL is possible.
    """
    symbol_info = mt5.symbol_info(symbol)
    tick = mt5.symbol_info_tick(symbol)
    if not symbol_info or not tick:
        return None, "no symbol/tick data"

    vol = normalize_volume(symbol_info, volume)
    dollars_per_price = vol * get_contract_size(symbol_info)
    distance = stop_usd / dollars_per_price
    point = getattr(symbol_info, 'point', 0.01)
    min_dist = max((getattr(symbol_info, 'trade_stops_level', 0) or 0) * point, point * 10)

    if direction == "BUY":
        sl = tick.ask - distance
        if sl >= tick.bid - min_dist:
            return None, f"SL {sl:.2f} too close to price {tick.bid:.2f}"
    else:
        sl = tick.bid + distance
        if sl <= tick.ask + min_dist:
            return None, f"SL {sl:.2f} too close to price {tick.ask:.2f}"

    return sl, stop_usd


def open_trade(symbol, direction, volume, stop_usd, comment):
    """Opens one market trade with a dynamic dollar stop loss from the entry price. Returns (result, fill_price)."""
    sl_price, info = calculate_stop_loss(symbol, direction, volume, stop_usd)
    if sl_price is None:
        logger.warning(f"⚠️ {direction} {volume} lots skipped: {info}")
        return None, 0.0

    order_type = mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL
    res, fill_price = place_order_safe(
        symbol=symbol,
        order_type=order_type,
        volume=volume,
        sl_price=sl_price,
        comment=comment
    )

    if res:
        logger.info(f"🚀 {direction} {volume} lots @ {fill_price:.2f} | SL {sl_price:.2f} (-${info:.2f})")
        send_push_notification(
            f"🟢 {direction} Placed",
            f"{symbol} {direction} {volume} lots @ {fill_price:.2f} | SL {sl_price:.2f} (-${info:.0f})"
        )
        return res, fill_price
    return None, 0.0


def check_challenge_limits(symbol):
    """MONITORING ONLY - Checks challenge limits and sends notifications. Does NOT stop the bot."""
    global limit_notified_daily, limit_notified_total, limit_notified_max_risk

    positions = get_active_positions(symbol)
    daily_realized_pnl = get_daily_realized_pnl(MAGIC_NUMBER)
    floating_pnl = sum(get_position_pnl(p) for p in positions)
    total_today_pnl = daily_realized_pnl + floating_pnl
    cumulative_closed_pnl = get_total_closed_pnl(MAGIC_NUMBER)

    # 1. Daily Loss Limit (4% = -$200)
    if total_today_pnl <= -MAX_DAILY_LOSS_USD and not limit_notified_daily:
        limit_notified_daily = True
        send_push_notification(
            "⚠️ DAILY LOSS LIMIT ALERT",
            f"Today's P&L: ${total_today_pnl:.2f} has reached the daily limit of -${MAX_DAILY_LOSS_USD:.2f}. "
            f"Consider closing positions manually to preserve account."
        )
    elif total_today_pnl > -MAX_DAILY_LOSS_USD + 25.0 and limit_notified_daily:
        limit_notified_daily = False

    # 2. Total Maximum Loss Limit (8% = -$400)
    if cumulative_closed_pnl <= -MAX_TOTAL_LOSS_USD and not limit_notified_total:
        limit_notified_total = True
        send_push_notification(
            "🔴 MAXIMUM LOSS LIMIT ALERT",
            f"Cumulative P&L: ${cumulative_closed_pnl:.2f} has reached the maximum loss limit of -${MAX_TOTAL_LOSS_USD:.2f}. "
            f"This is a critical alert. Consider stopping trading for this challenge."
        )
    elif cumulative_closed_pnl > -MAX_TOTAL_LOSS_USD + 50.0 and limit_notified_total:
        limit_notified_total = False

    # 3. Max Risk / Floating Drawdown Limit (-$150)
    current_loss = min(floating_pnl, total_today_pnl)
    if current_loss <= -MAX_RISK_AT_ANY_TIME_USD and not limit_notified_max_risk:
        limit_notified_max_risk = True
        send_push_notification(
            f"⚠️ MAX RISK ALERT (${MAX_RISK_AT_ANY_TIME_USD:.0f})",
            f"Loss is ${abs(current_loss):.2f} (Limit: -${MAX_RISK_AT_ANY_TIME_USD:.2f})."
        )
    elif current_loss > -MAX_RISK_AT_ANY_TIME_USD + 25.0 and limit_notified_max_risk:
        limit_notified_max_risk = False


def forget_ticket(ticket):
    trade_info.pop(ticket, None)


def get_closed_position_pnl(ticket):
    """Realized P&L of a closed position from the deal history (None if not found)."""
    try:
        deals = mt5.history_deals_get(position=ticket)
    except Exception:
        deals = None
    if not deals:
        return None
    return sum(d.profit + d.swap + getattr(d, 'commission', 0.0) for d in deals if d.entry == mt5.DEAL_ENTRY_OUT)


def close_trade(symbol, pos, title, reason, pnl):
    """Closes one bot trade and sends a notification."""
    global last_close_time
    trade_dir = "BUY" if pos.type == mt5.POSITION_TYPE_BUY else "SELL"
    logger.info(f"🎯 {reason} on #{pos.ticket}: P&L ${pnl:.2f}. Closing...")
    send_push_notification(title, f"{symbol} {trade_dir} #{pos.ticket} closed at ${pnl:.2f}. {reason}.")
    close_position_with_retry(symbol, pos, f"{reason} ${pnl:.2f}")
    forget_ticket(pos.ticket)
    last_close_time = time.time()


def manage_single(symbol, pos, bands, tick):
    """
    Manages one open trade. Returns True if still open, False if the bot closed it.
    - Backup stop at -$60 (broker SL normally closes first). This STOPS the bot.
    - Take profit at the OPPOSITE band (BUY -> upper, SELL -> lower).
    """
    global last_status_log_time, stop_bot_reason

    pnl = get_position_pnl(pos)
    is_buy = pos.type == mt5.POSITION_TYPE_BUY
    trade_dir = "BUY" if is_buy else "SELL"

    if MAX_LOSS_USD > 0 and pnl <= -MAX_LOSS_USD:
        logger.warning(f"🛑 Backup Stop: #{pos.ticket} P&L ${pnl:.2f} <= -${MAX_LOSS_USD:.2f}. Closing and stopping bot...")
        close_trade(symbol, pos, "🛑 Stop Loss Triggered", f"Backup stop -${MAX_LOSS_USD:.0f}", pnl)
        stop_bot_reason = f"Backup stop -${MAX_LOSS_USD:.0f} hit on #{pos.ticket} ({trade_dir}) at ${pnl:.2f}"
        return False

    target_band = None
    price = 0.0
    if bands is not None and tick is not None:
        target_band = bands['upper'] if is_buy else bands['lower']
        price = tick.bid if is_buy else tick.ask
        hit = price >= target_band if is_buy else price <= target_band
        if hit:
            close_trade(symbol, pos, "🏁 Target Band Reached!", f"Opposite band {target_band:.2f} reached", pnl)
            return False

    now = time.time()
    if now - last_status_log_time >= 10:
        last_status_log_time = now
        tgt = f"{target_band:.2f}" if target_band is not None else "n/a"
        logger.info(
            f"📈 #{pos.ticket} {trade_dir} {pos.volume} lots @ {pos.price_open:.2f} | Price {price:.2f} | "
            f"Target {tgt} | P&L ${pnl:.2f} | SL {pos.sl:.2f}"
        )
    return True


def manage_active_trade(symbol):
    """
    Manages the open trade. Returns True while a bot position is open, False when none is open.
    - Broker stop loss hit -> record the stop reason (the main loop then stops the bot).
    """
    global known_tickets, last_close_time, stop_bot_reason

    positions = get_active_positions(symbol)
    current = set(p.ticket for p in positions)

    # Trades that disappeared without the bot closing them -> broker stop loss hit
    for ticket in known_tickets - current:
        if ticket in trade_info:
            direction, last_pnl = trade_info[ticket]
            closed_pnl = get_closed_position_pnl(ticket)
            if closed_pnl is None:
                closed_pnl = last_pnl
            logger.warning(f"🛡️ #{ticket} {direction} closed by broker at ${closed_pnl:.2f}.")
            send_push_notification("🛡️ Closed by Broker", f"{symbol} {direction} #{ticket} closed at ${closed_pnl:.2f} (stop loss).")
            last_close_time = time.time()
            stop_bot_reason = f"Broker stop loss hit on #{ticket} ({direction}) at ${closed_pnl:.2f}"
        forget_ticket(ticket)
    known_tickets = current

    if not positions:
        return False

    # Keep trade_info current for broker-close detection
    for p in positions:
        trade_info[p.ticket] = ('BUY' if p.type == mt5.POSITION_TYPE_BUY else 'SELL', get_position_pnl(p))

    tick = mt5.symbol_info_tick(symbol)
    df = get_bollinger_bands(symbol, TIMEFRAME, period=BB_PERIOD, deviation=BB_DEVIATION, count=60)
    bands = None
    if not df.empty and not pd.isna(df.iloc[-1]['middle']):
        last = df.iloc[-1]
        bands = {'upper': float(last['upper']), 'middle': float(last['middle']), 'lower': float(last['lower'])}

    still_open = False
    for pos in positions:
        if manage_single(symbol, pos, bands, tick):
            still_open = True
    return still_open


def run_bot():
    """Main execution loop for the $5K Challenge XAUUSD M5 breakout bot (single trade, SL -$60)."""
    global last_close_time, phase1_announced, stop_bot_reason

    if not mt5.initialize():
        logger.error(f"MT5 initialization failed: {mt5.last_error()}")
        return

    symbol_info = mt5.symbol_info(SYMBOL)
    if not symbol_info:
        logger.error(f"Symbol {SYMBOL} not found.")
        mt5.shutdown()
        return

    logger.info("=" * 65)
    logger.info(f"🚀 Started $5K Challenge {SYMBOL} Breakout Bot (single trade)")
    logger.info(f"Timeframe: M5 | Indicator: Bollinger Bands (20, 2, Close)")
    logger.info(f"Entry: candle closes outside a band -> reversal candle opens outside and closes back inside")
    logger.info(f"Lot: {TRADE_VOLUME} | Stop Loss: -${STOP_LOSS_USD:.0f} from entry (dynamic)")
    logger.info(f"Take Profit: opposite band (BUY -> upper, SELL -> lower)")
    logger.info(f"Backup Stop: -${MAX_LOSS_USD:.0f}")
    logger.info(f"Stop behaviour: bot STOPS when the stop loss is hit")
    logger.info(f"⚠️ MONITORING (alerts only):")
    logger.info(f"  - Daily Loss Limit: -${MAX_DAILY_LOSS_USD:.2f} (4% of $5,000)")
    logger.info(f"  - Maximum Loss Limit: -${MAX_TOTAL_LOSS_USD:.2f} (8% of $5,000)")
    logger.info(f"  - Max Risk: -${MAX_RISK_AT_ANY_TIME_USD:.2f}")
    logger.info(f"Phase 1 Profit Target: +${PHASE_1_TARGET_USD:.2f} (8% of $5,000)")
    logger.info(f"Phase 2 Profit Target: +${PHASE_2_TARGET_USD:.2f} (4% of $5,000)")
    logger.info("=" * 65)

    send_push_notification(
        "🚀 5K Bot Online",
        f"Breakout bot active on {SYMBOL} (M5). {TRADE_VOLUME} lots, SL -${STOP_LOSS_USD:.0f}. "
        f"Bot stops if the stop loss is hit."
    )

    try:
        while True:
            # 1. Phase 1 Target Tracker (announced once)
            cumulative_closed_pnl = get_total_closed_pnl(MAGIC_NUMBER)
            if cumulative_closed_pnl >= PHASE_1_TARGET_USD and not phase1_announced:
                phase1_announced = True
                logger.info("=" * 65)
                logger.info(f"🎉🎉 PHASE 1 TARGET ACHIEVED! 🎉🎉")
                logger.info(f"Total Cumulative Profit: +${cumulative_closed_pnl:.2f} >= +${PHASE_1_TARGET_USD:.2f} (+8%)")
                logger.info("You may continue to Phase 2 or submit for rewards!")
                logger.info("=" * 65)
                send_push_notification(
                    "🎉 Phase 1 Target Achieved!",
                    f"Cumulative profit +${cumulative_closed_pnl:.2f} >= +${PHASE_1_TARGET_USD:.2f}. Bot keeps running."
                )

            # 2. Challenge Limits (MONITORING ONLY)
            check_challenge_limits(SYMBOL)

            # 3. Update the breakout / reversal state on every completed candle (always, even while in a trade)
            signal, candle_time = check_bb_entry_signal(SYMBOL)

            # 4. Manage the open trade
            still_open = manage_active_trade(SYMBOL)

            # 5. Stop the bot if a stop loss was hit
            if stop_bot_reason:
                logger.error(f"🛑 BOT STOPPED: {stop_bot_reason}")
                send_push_notification(
                    "🛑 BOT STOPPED",
                    f"{SYMBOL}: {stop_bot_reason}. Bot is stopped. Restart it manually."
                )
                break

            if still_open:
                time.sleep(SLEEP_INTERVAL)
                continue

            # 6. Cooldown after a trade closes
            if time.time() - last_close_time < COOLDOWN_SECONDS:
                time.sleep(SLEEP_INTERVAL)
                continue

            # 7. Valid reversal signal -> open the trade
            if signal:
                logger.info(f"📊 Signal [{candle_time}]: {signal}! Placing trade ({TRADE_VOLUME} lots)...")
                open_trade(SYMBOL, signal, TRADE_VOLUME, STOP_LOSS_USD, f"BB_Breakout_{signal}")

            time.sleep(SLEEP_INTERVAL)

    except KeyboardInterrupt:
        logger.info("Bot stopped by user (KeyboardInterrupt).")
    except Exception as e:
        logger.error(f"Unexpected error in main loop: {e}", exc_info=True)
    finally:
        mt5.shutdown()
        logger.info("MT5 connection closed gracefully.")


if __name__ == "__main__":
    run_bot()
