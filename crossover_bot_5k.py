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
MAX_DAILY_LOSS_USD = 200.0          # Daily Loss Limit: 4% (-$200.00) - ALERT ONLY
MAX_TOTAL_LOSS_USD = 400.0          # Maximum Loss Limit: 8% (-$400.00) - ALERT ONLY
MAX_RISK_USD = 150.0                # Max risk at any time (-$150.00) - ALERT ONLY

# --- Push Notification Settings ---
ENABLE_WINDOWS_TOAST = True            # Send native Windows desktop push notifications
ENABLE_NTFY_PUSH = True                # Send instant mobile push notifications via ntfy.sh
NTFY_TOPIC = "tradingbot_5k_ivaylo"    # Topic for ntfy mobile app (https://ntfy.sh/tradingbot_5k_ivaylo)

# --- Trading Strategy & Symbol Settings ---
SYMBOL = "XAUUSD"                   # Gold Bot
TIMEFRAME = mt5.TIMEFRAME_M5        # Bollinger Bands are calculated on M5
CANDLE_SECONDS = 300                # Length of one M5 candle in seconds
SLEEP_INTERVAL = 1                  # Check price every 1 second
COOLDOWN_SECONDS = 60               # Wait after a trade closes before the next entry
MAGIC_NUMBER = 60020                # Unique identifier for this bot's orders
DEVIATION = 20                      # Maximum price slippage in points

# --- Bollinger Bands (classic) ---
BB_PERIOD = 20                      # Period 20
BB_DEVIATION = 2.0                  # Deviation 2.0
# Applied Price: Close

# =====================================================================
# STRATEGY
# =====================================================================
# 1. REVERSION trade (counter-trend, classic Bollinger):
#      price TOUCHES the UPPER band -> SELL, target = LOWER band
#      price TOUCHES the LOWER band -> BUY,  target = UPPER band
#      Stop loss -$140 from entry.
#
# 2. BREAKOUT -> FLIP:
#      While a REVERSION trade is open, every COMPLETED M5 candle is checked:
#        SELL is open and a candle CLOSES ABOVE the UPPER band -> close the SELL, open BUY  (impulse up)
#        BUY  is open and a candle CLOSES BELOW the LOWER band -> close the BUY,  open SELL (impulse down)
#      This means price is "walking the band" against our trade, so we turn with it.
#
# 3. IMPULSE trade (with the trend, band walk):
#      Take profit +$100 and stop loss -$140, both from entry, set at the broker.
#      The bands do not close an impulse trade.
#
# 4. After an impulse, no new REVERSION trade on the same band until price returns to the middle band.
#      (Stops the bot from selling the top again while price is still walking the upper band.)
#
# One position at a time. The bot NEVER stops by itself (Ctrl+C to stop).
# =====================================================================

# --- Trade Size ---
TRADE_VOLUME = 0.10                 # Lot size for both reversion and impulse trades

# --- Reversion trade ---
REV_STOP_LOSS_USD = 140.0           # Broker stop loss from entry

# --- Impulse trade ---
IMP_TAKE_PROFIT_USD = 100.0         # Broker take profit from entry
IMP_STOP_LOSS_USD = 140.0           # Broker stop loss from entry

# --- Backup (software) exits, in case the broker SL/TP is missing ---
BACKUP_STOP_USD = 140.0
BACKUP_TP_USD = 100.0               # Impulse trades only

# --- After an impulse, block reversion on that band until price touches the middle band ---
BLOCK_REVERSION_AFTER_IMPULSE = True

REV_COMMENT = "BB_Rev"
IMP_COMMENT = "BB_Imp"

# --- Bot Runtime State ---
last_close_time = 0                 # Timestamp of last closed trade
last_signal_key = None              # (side, candle_time) of the last reversion entry
last_breakout_candle = None         # Last completed candle checked for a breakout
last_status_log_time = 0
last_wait_log_time = 0
known_tickets = set()               # Tickets seen on the previous loop (detects broker SL/TP closes)
trade_info = {}                     # {ticket: (direction, mode, last P&L)}
trade_modes = {}                    # {ticket: 'REV' or 'IMP'}
blocked_band = None                 # 'UPPER', 'LOWER' or None (reversion blocked on that band)
phase1_announced = False

# --- Limit Monitoring State Flags ---
limit_notified_daily = False
limit_notified_total = False
limit_notified_max_risk = False


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


def ensure_mt5_connection():
    """Reconnects to MT5 if the connection was lost. Returns True when connected."""
    try:
        if mt5.terminal_info() is not None:
            return True
    except Exception:
        pass
    logger.warning("🔌 MT5 connection lost. Reconnecting...")
    try:
        mt5.shutdown()
    except Exception:
        pass
    if mt5.initialize():
        logger.info("✅ MT5 reconnected.")
        return True
    logger.error(f"MT5 reconnect failed: {mt5.last_error()}. Retrying in 10s.")
    return False


def get_filling_type(symbol):
    """Dynamically determines the correct execution filling mode supported by the broker."""
    symbol_info = mt5.symbol_info(symbol)
    if symbol_info is None:
        return mt5.ORDER_FILLING_FOK
    filling_mode = getattr(symbol_info, 'filling_mode', 0)
    if filling_mode & 1:
        return mt5.ORDER_FILLING_FOK
    elif filling_mode & 2:
        return mt5.ORDER_FILLING_IOC
    return mt5.ORDER_FILLING_RETURN


def filling_modes(symbol):
    modes = [get_filling_type(symbol), mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_RETURN]
    unique = []
    for m in modes:
        if m not in unique:
            unique.append(m)
    return unique


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
    OHLC data with Bollinger Bands on Close.
    iloc[-1] = forming candle (live), iloc[-2] = last completed candle.
    't' = candle open time in server seconds.
    """
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, count)
    if rates is None or len(rates) < period + 5:
        return pd.DataFrame()

    df = pd.DataFrame(rates)
    df['t'] = df['time'].astype('int64')
    df['time'] = pd.to_datetime(df['time'], unit='s')
    df['middle'] = df['close'].rolling(window=period).mean()
    df['std'] = df['close'].rolling(window=period).std(ddof=0)
    df['upper'] = df['middle'] + deviation * df['std']
    df['lower'] = df['middle'] - deviation * df['std']
    return df


def bands_of(row):
    if pd.isna(row['upper']) or pd.isna(row['lower']) or pd.isna(row['middle']):
        return None
    return {
        'upper': float(row['upper']),
        'middle': float(row['middle']),
        'lower': float(row['lower']),
        'time': row['time'],
        't': int(row['t']),
        'open': float(row['open']),
        'close': float(row['close']),
    }


def get_band_data(symbol):
    """Returns (live_bands, last_completed_candle) or (None, None)."""
    df = get_bollinger_bands(symbol, TIMEFRAME, period=BB_PERIOD, deviation=BB_DEVIATION, count=60)
    if df.empty or len(df) < BB_PERIOD + 2:
        return None, None
    return bands_of(df.iloc[-1]), bands_of(df.iloc[-2])


# ---------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------
def calculate_levels(symbol, direction, volume, sl_usd, tp_usd=None):
    """
    Stop loss and take profit prices from the ENTRY price, in account dollars:
      distance = usd / (volume * contract size)
      0.10 lots, 100 oz: $140 -> $14.00 gold move, $100 -> $10.00 gold move.
    Returns (sl, tp) or (None, reason).
    """
    symbol_info = mt5.symbol_info(symbol)
    tick = mt5.symbol_info_tick(symbol)
    if not symbol_info or not tick:
        return None, "no symbol/tick data"

    vol = normalize_volume(symbol_info, volume)
    dollars_per_price = vol * get_contract_size(symbol_info)
    sl_dist = sl_usd / dollars_per_price
    tp_dist = tp_usd / dollars_per_price if tp_usd else None
    point = getattr(symbol_info, 'point', 0.01)
    min_dist = max((getattr(symbol_info, 'trade_stops_level', 0) or 0) * point, point * 10)

    if direction == "BUY":
        entry = tick.ask
        sl = entry - sl_dist
        tp = entry + tp_dist if tp_dist else 0.0
        if sl >= tick.bid - min_dist:
            return None, f"SL {sl:.2f} too close to price {tick.bid:.2f}"
        if tp and tp <= tick.bid + min_dist:
            return None, f"TP {tp:.2f} too close to price {tick.bid:.2f}"
    else:
        entry = tick.bid
        sl = entry + sl_dist
        tp = entry - tp_dist if tp_dist else 0.0
        if sl <= tick.ask + min_dist:
            return None, f"SL {sl:.2f} too close to price {tick.ask:.2f}"
        if tp and tp >= tick.ask - min_dist:
            return None, f"TP {tp:.2f} too close to price {tick.ask:.2f}"
    return sl, tp


def place_order_safe(symbol, order_type, volume, sl_price=0.0, tp_price=0.0, comment="BB"):
    """Places a market order with SL/TP prices and filling mode fallback. Returns (result, fill_price)."""
    action_name = "BUY" if order_type == mt5.ORDER_TYPE_BUY else "SELL"
    symbol_info = mt5.symbol_info(symbol)
    if not symbol_info:
        logger.error(f"Symbol {symbol} not found.")
        send_push_notification(f"❌ Trade Failed ({action_name})", f"Symbol {symbol} not found.")
        return None, 0.0
    if not symbol_info.visible:
        mt5.symbol_select(symbol, True)

    volume = normalize_volume(symbol_info, volume)
    tick = mt5.symbol_info_tick(symbol)
    if not tick:
        logger.error(f"Failed to get tick for {symbol}")
        send_push_notification(f"❌ Trade Failed ({action_name})", f"No tick data for {symbol}.")
        return None, 0.0

    price = tick.ask if order_type == mt5.ORDER_TYPE_BUY else tick.bid
    digits = getattr(symbol_info, 'digits', 2)
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": float(volume),
        "type": order_type,
        "price": price,
        "sl": float(round(sl_price, digits)) if sl_price else 0.0,
        "tp": float(round(tp_price, digits)) if tp_price else 0.0,
        "magic": MAGIC_NUMBER,
        "comment": comment,
        "deviation": DEVIATION,
        "type_time": mt5.ORDER_TIME_GTC,
    }

    res = None
    for mode in filling_modes(symbol):
        request["type_filling"] = mode
        res = mt5.order_send(request)
        if res and res.retcode in [mt5.TRADE_RETCODE_DONE, 10008, 0]:
            logger.info(f"✅ {action_name} {volume} ({comment}) @ {price} | SL {request['sl']} | TP {request['tp']}")
            return res, price
        elif res and res.retcode == 10030:
            continue
        else:
            break

    err = mt5.last_error() if not res else f"{res.comment} (Code: {res.retcode})"
    logger.error(f"Failed to place order ({comment}): {err} | Request: {request}")
    send_push_notification(f"❌ Trade Failed ({action_name})", f"{symbol} {action_name} {volume} ({comment}): {err}")
    return None, 0.0


def open_trade(symbol, direction, mode):
    """Opens a REVERSION ('REV') or IMPULSE ('IMP') trade. Returns True on success."""
    if mode == 'REV':
        levels = calculate_levels(symbol, direction, TRADE_VOLUME, REV_STOP_LOSS_USD)
        comment = f"{REV_COMMENT}_{direction}"
    else:
        levels = calculate_levels(symbol, direction, TRADE_VOLUME, IMP_STOP_LOSS_USD, IMP_TAKE_PROFIT_USD)
        comment = f"{IMP_COMMENT}_{direction}"

    sl, tp = levels
    if sl is None:
        logger.warning(f"⚠️ {mode} {direction} skipped: {tp}")
        return False

    order_type = mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL
    res, fill_price = place_order_safe(symbol, order_type, TRADE_VOLUME, sl, tp, comment)
    if not res:
        return False

    ticket = getattr(res, 'order', 0)
    if ticket:
        trade_modes[ticket] = mode

    if mode == 'REV':
        target = "LOWER band" if direction == "SELL" else "UPPER band"
        msg = f"{symbol} {direction} {TRADE_VOLUME} @ {fill_price:.2f} | SL {sl:.2f} (-${REV_STOP_LOSS_USD:.0f}) | Target: {target}"
        send_push_notification(f"🔄 Reversion {direction}", msg)
    else:
        msg = (f"{symbol} {direction} {TRADE_VOLUME} @ {fill_price:.2f} | "
               f"TP {tp:.2f} (+${IMP_TAKE_PROFIT_USD:.0f}) | SL {sl:.2f} (-${IMP_STOP_LOSS_USD:.0f})")
        send_push_notification(f"🚀 Impulse {direction}", msg)
    logger.info(f"🚀 {mode} | {msg}")
    return True


def get_active_positions(symbol):
    """Open positions of this bot (MAGIC_NUMBER), oldest first."""
    positions = mt5.positions_get(symbol=symbol)
    if not positions:
        return []
    bot_positions = [p for p in positions if p.magic == MAGIC_NUMBER]
    bot_positions.sort(key=lambda p: (getattr(p, 'time_msc', p.time), p.ticket))
    return bot_positions


def get_trade_mode(pos):
    """'IMP' for impulse trades, 'REV' for reversion trades."""
    if pos.ticket in trade_modes:
        return trade_modes[pos.ticket]
    mode = 'IMP' if str(getattr(pos, 'comment', '')).startswith(IMP_COMMENT) else 'REV'
    trade_modes[pos.ticket] = mode
    return mode


def close_position(symbol, position):
    """Closes a single MT5 position."""
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
    }
    res = None
    for mode in filling_modes(symbol):
        request["type_filling"] = mode
        res = mt5.order_send(request)
        if res and res.retcode in [mt5.TRADE_RETCODE_DONE, 10008, 0]:
            logger.info(f"Closed #{position.ticket} at {price} (P&L: ${get_position_pnl(position):.2f})")
            return True
        elif res and res.retcode == 10030:
            continue
        else:
            break
    err = mt5.last_error() if not res else f"{res.comment} (Code: {res.retcode})"
    logger.error(f"Failed to close #{position.ticket}: {err}")
    return False


def close_position_with_retry(symbol, position, reason=""):
    """Closes one position, retrying up to 5 times. Returns True when it is closed."""
    for attempt in range(5):
        still_open = [p for p in get_active_positions(symbol) if p.ticket == position.ticket]
        if not still_open:
            return True
        logger.info(f"Closing #{position.ticket} ({reason}) [Attempt {attempt + 1}/5]...")
        close_position(symbol, still_open[0])
        time.sleep(0.2)
    if any(p.ticket == position.ticket for p in get_active_positions(symbol)):
        send_push_notification("⚠️ Close Failed",
                               f"#{position.ticket} still open on {symbol} after 5 attempts ({reason}). Check MT5 now!")
        return False
    return True


def forget_ticket(ticket):
    trade_info.pop(ticket, None)
    trade_modes.pop(ticket, None)


def close_trade(symbol, pos, title, reason):
    """Closes one bot trade and sends a notification. Returns True when closed."""
    global last_close_time
    pnl = get_position_pnl(pos)
    trade_dir = "BUY" if pos.type == mt5.POSITION_TYPE_BUY else "SELL"
    logger.info(f"🎯 {reason} on #{pos.ticket}: P&L ${pnl:.2f}. Closing...")
    closed = close_position_with_retry(symbol, pos, reason)
    if closed:
        send_push_notification(title, f"{symbol} {trade_dir} #{pos.ticket} closed at ${pnl:.2f}. {reason}.")
        forget_ticket(pos.ticket)
        last_close_time = time.time()
    return closed


# ---------------------------------------------------------------------
# P&L history & alerts (never stop the bot)
# ---------------------------------------------------------------------
def sum_closed_pnl(start, end, magic=MAGIC_NUMBER):
    deals = mt5.history_deals_get(start, end)
    if not deals:
        return 0.0
    return sum(d.profit + d.swap + getattr(d, 'commission', 0.0)
               for d in deals if d.magic == magic and d.entry == mt5.DEAL_ENTRY_OUT)


def get_daily_realized_pnl():
    now = datetime.now()
    return sum_closed_pnl(now.replace(hour=0, minute=0, second=0, microsecond=0), now)


def get_total_closed_pnl():
    return sum_closed_pnl(datetime(2026, 1, 1), datetime.now())


def get_closed_position_pnl(ticket):
    """Realized P&L of a closed position from the deal history (None if not found)."""
    try:
        deals = mt5.history_deals_get(position=ticket)
    except Exception:
        deals = None
    if not deals:
        return None
    return sum(d.profit + d.swap + getattr(d, 'commission', 0.0) for d in deals if d.entry == mt5.DEAL_ENTRY_OUT)


def check_challenge_limits(symbol):
    """ALERTS ONLY. Never stops the bot."""
    global limit_notified_daily, limit_notified_total, limit_notified_max_risk

    floating_pnl = sum(get_position_pnl(p) for p in get_active_positions(symbol))
    total_today_pnl = get_daily_realized_pnl() + floating_pnl
    cumulative_closed_pnl = get_total_closed_pnl()

    if total_today_pnl <= -MAX_DAILY_LOSS_USD and not limit_notified_daily:
        limit_notified_daily = True
        send_push_notification("⚠️ DAILY LOSS LIMIT ALERT",
                               f"Today's P&L ${total_today_pnl:.2f} reached -${MAX_DAILY_LOSS_USD:.0f}. Bot keeps running.")
    elif total_today_pnl > -MAX_DAILY_LOSS_USD + 25.0:
        limit_notified_daily = False

    if cumulative_closed_pnl <= -MAX_TOTAL_LOSS_USD and not limit_notified_total:
        limit_notified_total = True
        send_push_notification("🔴 MAXIMUM LOSS LIMIT ALERT",
                               f"Cumulative P&L ${cumulative_closed_pnl:.2f} reached -${MAX_TOTAL_LOSS_USD:.0f}. Bot keeps running.")
    elif cumulative_closed_pnl > -MAX_TOTAL_LOSS_USD + 50.0:
        limit_notified_total = False

    current_loss = min(floating_pnl, total_today_pnl)
    if current_loss <= -MAX_RISK_USD and not limit_notified_max_risk:
        limit_notified_max_risk = True
        send_push_notification(f"⚠️ MAX RISK ALERT (${MAX_RISK_USD:.0f})",
                               f"Loss is ${abs(current_loss):.2f}. Bot keeps running.")
    elif current_loss > -MAX_RISK_USD + 25.0:
        limit_notified_max_risk = False


# ---------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------
def update_band_block(tick, live):
    """Clears the post-impulse block once price comes back to the middle band."""
    global blocked_band
    if blocked_band is None or live is None or tick is None:
        return
    if blocked_band == 'UPPER' and tick.bid <= live['middle']:
        logger.info(f"✅ Price {tick.bid:.2f} back at middle {live['middle']:.2f}. Reversion at UPPER band allowed again.")
        blocked_band = None
    elif blocked_band == 'LOWER' and tick.ask >= live['middle']:
        logger.info(f"✅ Price {tick.ask:.2f} back at middle {live['middle']:.2f}. Reversion at LOWER band allowed again.")
        blocked_band = None


def check_reversion_signal(tick, live):
    """
    Classic Bollinger touch, checked every second:
      bid touches UPPER -> SELL
      ask touches LOWER -> BUY
    Returns (side, candle_time) or (None, None).
    """
    global last_wait_log_time
    if tick is None or live is None:
        return None, None

    up, mid, lo = live['upper'], live['middle'], live['lower']
    side, band = None, None
    if tick.bid >= up:
        side, band = 'SELL', 'UPPER'
    elif tick.ask <= lo:
        side, band = 'BUY', 'LOWER'

    if side is not None:
        if BLOCK_REVERSION_AFTER_IMPULSE and blocked_band == band:
            now = time.time()
            if now - last_wait_log_time >= 15:
                last_wait_log_time = now
                logger.info(f"⏸️ {band} band touched, but reversion is blocked after the impulse "
                            f"(waiting for middle {mid:.2f}).")
            return None, None
        if (side, live['time']) == last_signal_key:
            return None, None
        logger.info(f"📊 Reversion {side} [{live['time']}] | Bid {tick.bid:.2f} / Ask {tick.ask:.2f} touched {band} band")
        return side, live['time']

    now = time.time()
    if now - last_wait_log_time >= 15:
        last_wait_log_time = now
        logger.info(f"⏳ Waiting for a band touch | Bid {tick.bid:.2f} | Upper {up:.2f}, Mid {mid:.2f}, Lower {lo:.2f}")
    return None, None


def check_breakout_flip(symbol, pos, last_closed):
    """
    REVERSION trade only. Checks each completed candle once:
      SELL open and candle CLOSED above UPPER -> close SELL, open BUY impulse
      BUY  open and candle CLOSED below LOWER -> close BUY,  open SELL impulse
    Only candles that closed AFTER the trade was opened count.
    Returns True if the trade was flipped.
    """
    global last_breakout_candle, blocked_band
    if last_closed is None:
        return False
    if last_closed['t'] == last_breakout_candle:
        return False
    last_breakout_candle = last_closed['t']

    if last_closed['t'] + CANDLE_SECONDS <= pos.time:
        return False   # candle closed before the trade was opened

    is_buy = pos.type == mt5.POSITION_TYPE_BUY
    c, up, lo = last_closed['close'], last_closed['upper'], last_closed['lower']

    if not is_buy and c > up:
        new_dir, band, txt = "BUY", 'UPPER', f"candle closed {c:.2f} ABOVE upper {up:.2f}"
    elif is_buy and c < lo:
        new_dir, band, txt = "SELL", 'LOWER', f"candle closed {c:.2f} BELOW lower {lo:.2f}"
    else:
        return False

    logger.warning(f"⚡ BREAKOUT [{last_closed['time']}]: {txt}. Flipping to {new_dir} impulse.")
    if not close_trade(symbol, pos, "⚡ Breakout - Reversion Closed", f"Breakout: {txt}"):
        return False

    blocked_band = band
    if open_trade(symbol, new_dir, 'IMP'):
        send_push_notification("⚡ Flipped to Impulse",
                               f"{symbol}: {txt}. Opened {new_dir} impulse, TP +${IMP_TAKE_PROFIT_USD:.0f} / SL -${IMP_STOP_LOSS_USD:.0f}.")
    return True


# ---------------------------------------------------------------------
# Position management
# ---------------------------------------------------------------------
def detect_broker_closes(symbol, current):
    """Reports trades that disappeared without the bot closing them (broker SL or TP)."""
    global known_tickets, last_close_time
    for ticket in known_tickets - current:
        if ticket in trade_info:
            direction, mode, last_pnl = trade_info[ticket]
            closed_pnl = get_closed_position_pnl(ticket)
            if closed_pnl is None:
                closed_pnl = last_pnl
            kind = "take profit" if closed_pnl > 0 else "stop loss"
            name = "Impulse" if mode == 'IMP' else "Reversion"
            logger.warning(f"🛡️ {name} #{ticket} {direction} closed by broker ({kind}) at ${closed_pnl:.2f}.")
            send_push_notification(f"🛡️ {name} Closed ({kind})",
                                   f"{symbol} {direction} #{ticket} closed at ${closed_pnl:.2f}. Bot keeps running.")
            last_close_time = time.time()
        forget_ticket(ticket)
    known_tickets = current


def manage_position(symbol, pos, tick, live, last_closed):
    """Returns True while the trade is still open."""
    global last_status_log_time

    pnl = get_position_pnl(pos)
    is_buy = pos.type == mt5.POSITION_TYPE_BUY
    trade_dir = "BUY" if is_buy else "SELL"
    mode = get_trade_mode(pos)
    trade_info[pos.ticket] = (trade_dir, mode, pnl)

    # 1. Backup stop (both modes)
    if BACKUP_STOP_USD > 0 and pnl <= -BACKUP_STOP_USD:
        close_trade(symbol, pos, "🛑 Stop Loss", f"Backup stop -${BACKUP_STOP_USD:.0f}")
        return False

    if mode == 'IMP':
        # 2a. Impulse: backup take profit (broker TP normally closes first)
        if BACKUP_TP_USD > 0 and pnl >= BACKUP_TP_USD:
            close_trade(symbol, pos, "🏁 Impulse Take Profit", f"Profit +${BACKUP_TP_USD:.0f} reached")
            return False
        target_txt = f"TP {pos.tp:.2f}"
    else:
        # 2b. Reversion: take profit at the opposite band
        target_txt = "n/a"
        if live is not None and tick is not None:
            target = live['upper'] if is_buy else live['lower']
            price = tick.bid if is_buy else tick.ask
            target_txt = f"{target:.2f}"
            if (price >= target) if is_buy else (price <= target):
                close_trade(symbol, pos, "🏁 Target Band Reached!", f"Opposite band {target:.2f} reached")
                return False

        # 3. Reversion: breakout against the trade -> flip to impulse
        if check_breakout_flip(symbol, pos, last_closed):
            return True   # a new impulse trade is (normally) open now

    now = time.time()
    if now - last_status_log_time >= 10:
        last_status_log_time = now
        logger.info(f"📈 {mode} #{pos.ticket} {trade_dir} {pos.volume} @ {pos.price_open:.2f} | "
                    f"Target {target_txt} | P&L ${pnl:.2f} | SL {pos.sl:.2f}")
    return True


def manage_active_trade(symbol, tick, live, last_closed):
    """Returns True while a bot position is open."""
    positions = get_active_positions(symbol)
    detect_broker_closes(symbol, set(p.ticket for p in positions))
    if not positions:
        return False

    still_open = False
    for pos in positions:
        if manage_position(symbol, pos, tick, live, last_closed):
            still_open = True
    # Re-read after a possible flip so the new impulse ticket is tracked
    if still_open:
        for p in get_active_positions(symbol):
            known_tickets.add(p.ticket)
    return still_open


# ---------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------
def bot_iteration():
    """One pass of the main loop. Any error is caught in run_bot and the bot continues."""
    global last_signal_key, phase1_announced

    cumulative_closed_pnl = get_total_closed_pnl()
    if cumulative_closed_pnl >= PHASE_1_TARGET_USD and not phase1_announced:
        phase1_announced = True
        logger.info(f"🎉 PHASE 1 TARGET ACHIEVED! Total: +${cumulative_closed_pnl:.2f}")
        send_push_notification("🎉 Phase 1 Target Achieved!",
                               f"Cumulative profit +${cumulative_closed_pnl:.2f}. Bot keeps running.")

    check_challenge_limits(SYMBOL)

    tick = mt5.symbol_info_tick(SYMBOL)
    live, last_closed = get_band_data(SYMBOL)
    update_band_block(tick, live)

    if manage_active_trade(SYMBOL, tick, live, last_closed):
        return

    if time.time() - last_close_time < COOLDOWN_SECONDS:
        return

    side, candle_time = check_reversion_signal(tick, live)
    if side:
        last_signal_key = (side, candle_time)
        open_trade(SYMBOL, side, 'REV')


def run_bot():
    """Checks price every second and runs 24/7 until stopped manually (Ctrl+C)."""
    while not mt5.initialize():
        logger.error(f"MT5 initialization failed: {mt5.last_error()}. Retrying in 10s...")
        time.sleep(10)

    logger.info("=" * 65)
    logger.info(f"🚀 {SYMBOL} Bollinger Reversion + Breakout Flip Bot (24/7, manual stop only)")
    logger.info(f"Bands: M5 BB({BB_PERIOD}, {BB_DEVIATION}, Close) | Price check every {SLEEP_INTERVAL}s | Lot {TRADE_VOLUME}")
    logger.info(f"Reversion: touch UPPER -> SELL, touch LOWER -> BUY | target opposite band | SL -${REV_STOP_LOSS_USD:.0f}")
    logger.info(f"Flip: candle closes beyond the band against the trade -> close it, open impulse with the move")
    logger.info(f"Impulse: TP +${IMP_TAKE_PROFIT_USD:.0f} | SL -${IMP_STOP_LOSS_USD:.0f}")
    logger.info(f"Alerts only: daily -${MAX_DAILY_LOSS_USD:.0f}, total -${MAX_TOTAL_LOSS_USD:.0f}, max risk -${MAX_RISK_USD:.0f}")
    logger.info("=" * 65)

    send_push_notification("🚀 5K Bot Online",
                           f"{SYMBOL}: reversion at the bands, flip to impulse on breakout "
                           f"(TP +${IMP_TAKE_PROFIT_USD:.0f} / SL -${IMP_STOP_LOSS_USD:.0f}). Runs 24/7.")

    error_notified = False
    try:
        while True:
            try:
                if not ensure_mt5_connection():
                    time.sleep(10)
                    continue
                bot_iteration()
                error_notified = False
            except Exception as e:
                logger.error(f"Error in loop (bot continues): {e}", exc_info=True)
                if not error_notified:
                    error_notified = True
                    send_push_notification("⚠️ Bot Error", f"{SYMBOL}: {e}. Bot keeps running.")
                time.sleep(5)
            time.sleep(SLEEP_INTERVAL)
    except KeyboardInterrupt:
        logger.info("Bot stopped by user (Ctrl+C).")
    finally:
        mt5.shutdown()
        logger.info("MT5 connection closed.")


if __name__ == "__main__":
    run_bot()
