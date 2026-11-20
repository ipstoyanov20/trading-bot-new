import os, json, time, math, logging, subprocess, threading
from datetime import datetime, timedelta, timezone
import MetaTrader5 as mt5
import pandas as pd
import requests

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)

SYMBOL, TIMEFRAME, BB_PERIOD, BB_DEV, RSI_PERIOD = "XAUUSD", mt5.TIMEFRAME_M5, 20, 2.0, 14
WALK_LOOKBACK, WALK_MIN, WALK_ZONE = 10, 4, 0.10
SL_BUFFER, RISK_USD, MAX_LOT, DAILY_LIMIT = 0.50, 50.0, 1.00, 200.0
AUTO_TRADE, MAGIC, DEVIATION = False, 60030, 20
NTFY_TOPIC, WINDOWS_TOAST, QUIET_MODE = "tradingbot_5k_ivaylo", True, False
MT5_PUSH, NTFY_ENABLED = False, True
PUSH_DIR = os.path.expandvars(r"%APPDATA%\MetaQuotes\Terminal\Common\Files")
AI_ENABLED, AI_PROVIDER, AI_CANDLES, AI_MAX_TOKENS = True, "gemini", 60, 4000
GEMINI_MODEL, CLAUDE_MODEL = "gemini-3.5-flash-lite", "claude-sonnet-5-5"
API_KEY = os.environ.get("GEMINI_API_KEY" if AI_PROVIDER == "gemini" else "ANTHROPIC_API_KEY", "")
CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
NEWS_WINDOW_MIN, NEWS_SEARCH, NEWS_REFRESH_MIN, NEWS_SEARCHES = 30, False, 60, 2

RR_RULE = "R:R до целта >= 1.0"
VERDICT_BG = {"TAKE": "ВЛИЗАЙ", "SKIP": "ПРОПУСНИ"}
SIDE_BG = {"BUY": "КУПУВА (BUY)", "SELL": "ПРОДАВА (SELL)"}
TYPE_BG = {"REVERSION": "обратен", "TREND": "по тренда"}
state = {"candle": None, "wait_log": 0, "walk": None}
cal_cache, news_cache = {"t": 0, "ev": None}, {"t": 0, "text": None}

AI_SYSTEM = (
    "You are a disciplined, rule-based trading reviewer for Bollinger Bands (20, 2) signals on XAUUSD M5. "
    "REVERSION: price touched an outer band and closed back inside; target is the middle band; works in calm "
    "ranging markets, fails in strong trends. TREND: price is walking a band and pulled back to the middle band; "
    "target is the outer band in the trend direction; works in strong trends, fails when the trend is exhausted. "
    "Never predict the future, never use emotional language. Judge only from the candles, numbers and news given: "
    "trend and band walk, band squeeze or expansion, RSI, wicks, nearby support/resistance, risk:reward. Say SKIP "
    "when a major USD event is close or the news clearly pushes against the trade. Account: $5,000, $200 daily "
    'loss limit. Reply with JSON only: {"decision": "TAKE" or "SKIP", "confidence": 1-5, "reasons": ["..."], '
    '"invalidation": "in Bulgarian: what would prove the trade wrong", "summary_bg": "1-2 sentences in Bulgarian"}')


def mt5_push(text, order=0):
    try:
        os.makedirs(PUSH_DIR, exist_ok=True)
        name = os.path.join(PUSH_DIR, f"bb_push_{time.time_ns()}_{order}")
        with open(name + ".tmp", "w", encoding="utf-8") as f:
            f.write(text[:255])
        os.replace(name + ".tmp", name + ".txt")
    except Exception as e:
        log.error(f"MT5 push file failed: {e}")


def notify(title, msg, copy=None, push=None):
    log.warning(f"📢 {title}\n{msg}")
    if MT5_PUSH:
        for i, part in enumerate(push if isinstance(push, list) else [push or f"{title}\n{msg}"]):
            mt5_push(part, i)
    def send():
        if WINDOWS_TOAST:
            try:
                t, m = title.replace("'", ""), msg[:200].replace("'", "").replace("\n", " ")
                ps = ("[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null;"
                      "$x=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
                      f"$n=$x.GetElementsByTagName('text');$n.Item(0).InnerText='{t}';$n.Item(1).InnerText='{m}';"
                      "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe').Show([Windows.UI.Notifications.ToastNotification]::new($x))")
                subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, timeout=5)
            except Exception:
                pass
        if not NTFY_ENABLED:
            return
        body = {"topic": NTFY_TOPIC, "title": title, "message": msg}
        if copy:
            body["actions"] = [{"action": "copy", "label": f"📋 {k} {v}", "value": v} for k, v in copy]
        try:
            requests.post("https://ntfy.sh/", json=body, timeout=10)
        except Exception as e:
            log.debug(e)
    threading.Thread(target=send, daemon=True).start()


def get_candles():
    rates = mt5.copy_rates_from_pos(SYMBOL, TIMEFRAME, 0, 200)
    if rates is None or len(rates) < BB_PERIOD + 60:
        return pd.DataFrame()
    df = pd.DataFrame(rates)
    df['time'] = pd.to_datetime(df['time'], unit='s')
    df['middle'] = df['close'].rolling(BB_PERIOD).mean()
    std = df['close'].rolling(BB_PERIOD).std(ddof=0)
    df['upper'], df['lower'] = df['middle'] + BB_DEV * std, df['middle'] - BB_DEV * std
    df['width'] = df['upper'] - df['lower']
    d = df['close'].diff()
    up = d.clip(lower=0).ewm(alpha=1 / RSI_PERIOD, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / RSI_PERIOD, adjust=False).mean()
    df['rsi'] = 100 - 100 / (1 + up / dn.replace(0, 1e-10))
    return df


def band_walk(df):
    p = df.iloc[-(WALK_LOOKBACK + 2):-2]
    slope = df['middle'].iloc[-2] - df['middle'].iloc[-(WALK_LOOKBACK + 2)]
    below = int((p['low'] <= p['lower'] + WALK_ZONE * p['width']).sum())
    above = int((p['high'] >= p['upper'] - WALK_ZONE * p['width']).sum())
    if below >= WALK_MIN and slope < 0 and below > above:
        return 'DOWN', below
    if above >= WALK_MIN and slope > 0 and above > below:
        return 'UP', above
    return None, max(below, above)


def find_signal(df):
    c, (walk, _) = df.iloc[-2], band_walk(df)
    if walk == 'DOWN' and c['high'] >= c['middle'] > c['close'] and c['close'] < c['open']:
        return 'SELL', 'TREND', walk
    if walk == 'UP' and c['low'] <= c['middle'] < c['close'] and c['close'] > c['open']:
        return 'BUY', 'TREND', walk
    lo, hi = c['low'] <= c['lower'] < c['close'], c['high'] >= c['upper'] > c['close']
    if lo and not hi and walk != 'DOWN':
        return 'BUY', 'REVERSION', walk
    if hi and not lo and walk != 'UP':
        return 'SELL', 'REVERSION', walk
    return None, None, walk


def build_plan(df, side, kind):
    c, tick, info = df.iloc[-2], mt5.symbol_info_tick(SYMBOL), mt5.symbol_info(SYMBOL)
    if tick is None or info is None:
        return None
    contract, step, vmin = info.trade_contract_size or 100.0, info.volume_step or 0.01, info.volume_min or 0.01
    if kind == 'REVERSION':
        tp, tp_name = c['middle'], "средна линия"
    else:
        tp, tp_name = (c['lower'], "долна лента") if side == 'SELL' else (c['upper'], "горна лента")
    if side == 'BUY':
        entry, sl = tick.ask, c['low'] - SL_BUFFER
        sl_d, tp_d = entry - sl, tp - entry
    else:
        entry, sl = tick.bid, c['high'] + SL_BUFFER
        sl_d, tp_d = sl - entry, entry - tp
    if sl_d <= 0 or tp_d <= 0:
        return None
    lot = round(min(max(math.floor(RISK_USD / (sl_d * contract) / step) * step, vmin), MAX_LOT), 2)
    return dict(side=side, type=kind, entry=entry, sl=sl, tp=tp, tp_name=tp_name, lot=lot, rr=tp_d / sl_d,
                risk=sl_d * contract * lot, reward=tp_d * contract * lot, time=c['time'])


def usd_events():
    if cal_cache["ev"] is not None and time.time() - cal_cache["t"] < 3600:
        return cal_cache["ev"]
    try:
        r = requests.get(CALENDAR_URL, timeout=15, headers={"User-Agent": "bb-signal-bot"})
        r.raise_for_status()
        ev = [{"title": e.get("title", ""), "time": datetime.fromisoformat(e["date"]).astimezone(timezone.utc),
               "forecast": e.get("forecast", ""), "previous": e.get("previous", "")}
              for e in r.json() if e.get("country") == "USD" and e.get("impact") == "High" and e.get("date")]
        cal_cache.update(t=time.time(), ev=ev)
        log.info(f"📅 Calendar: {len(ev)} high-impact USD events this week")
    except Exception as e:
        log.error(f"Calendar failed: {e}")
    return cal_cache["ev"]


def fmt_event(e):
    extra = f" (прогноза {e['forecast']}, предишно {e['previous']})" if e['forecast'] or e['previous'] else ""
    return f"{e['time'].astimezone():%a %H:%M} {e['title']}{extra}"


def call_ai(system, user, search=False):
    try:
        if AI_PROVIDER == "gemini":
            body = {"contents": [{"role": "user", "parts": [{"text": user}]}],
                    "generationConfig": {"maxOutputTokens": AI_MAX_TOKENS}}
            if system:
                body["systemInstruction"] = {"parts": [{"text": system}]}
            if search:
                body["tools"] = [{"google_search": {}}]
            else:
                body["generationConfig"]["responseMimeType"] = "application/json"
            r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
                              json=body, timeout=120, headers={"x-goog-api-key": API_KEY})
        else:
            body = {"model": CLAUDE_MODEL, "max_tokens": AI_MAX_TOKENS, "messages": [{"role": "user", "content": user}]}
            if system:
                body["system"] = system
            if search:
                body["tools"] = [{"type": "web_search_20250305", "name": "web_search", "max_uses": NEWS_SEARCHES}]
            r = requests.post("https://api.anthropic.com/v1/messages", json=body, timeout=120,
                              headers={"x-api-key": API_KEY, "anthropic-version": "2023-06-01"})
        if r.status_code != 200:
            log.error(f"AI HTTP {r.status_code}: {r.text[:500]}")
            return None, f"HTTP {r.status_code}"
        data = r.json()
        if AI_PROVIDER == "gemini":
            cand = (data.get("candidates") or [{}])[0]
            text = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", []) if not p.get("thought"))
            data["stop_reason"] = cand.get("finishReason")
        else:
            text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        return data, text.strip()
    except Exception as e:
        log.error(f"AI request failed: {e}")
        return None, str(e)


def news_summary():
    if not (AI_ENABLED and NEWS_SEARCH and API_KEY):
        return None
    if news_cache["text"] and time.time() - news_cache["t"] < NEWS_REFRESH_MIN * 60:
        return news_cache["text"]
    prompt = ("Search for the most important news of the last 24 hours that moves gold (XAUUSD): the Federal Reserve, "
              "US inflation and jobs data, the US dollar, US Treasury yields and major geopolitical events. Reply in "
              "Bulgarian with at most 5 short bullet points, facts only, and end with one line: 'Общ фон за златото: "
              "подкрепящ / натискащ / неутрален' with a one-sentence reason. No price predictions.")
    data, text = call_ai(None, prompt, search=True)
    if data and text:
        news_cache.update(t=time.time(), text=text)
        log.info(f"📰 News:\n{text}")
    return news_cache["text"]


def checklist(df, plan, walk):
    c, side = df.iloc[-2], plan['side']
    body = abs(c['close'] - c['open'])
    up_wick, lo_wick = c['high'] - max(c['open'], c['close']), min(c['open'], c['close']) - c['low']
    avg_w = df['width'].iloc[-52:-2].mean()
    slope = df['middle'].iloc[-2] - df['middle'].iloc[-(WALK_LOOKBACK + 2)]
    if plan['type'] == 'REVERSION':
        p5 = df.iloc[-7:-2]
        out = int((p5['close'] < p5['lower']).sum() if side == 'BUY' else (p5['close'] > p5['upper']).sum())
        wick, rsi_ok = (lo_wick, c['rsi'] <= 35) if side == 'BUY' else (up_wick, c['rsi'] >= 65)
        checks = {RR_RULE: plan['rr'] >= 1.0,
                  "Няма пълзене по лентата (макс. 1 от последните 5 свещи извън нея)": out <= 1,
                  "Лентите не се разширяват рязко (ширина <= 1.5x средната)": c['width'] <= 1.5 * avg_w,
                  f"RSI потвърждава ({c['rsi']:.0f})": bool(rsi_ok),
                  "Отхвърляща сянка >= тялото на свещта": wick >= body}
    else:
        wick, rsi_ok, slope_ok = (up_wick, c['rsi'] <= 55, slope < 0) if side == 'SELL' else (lo_wick, c['rsi'] >= 45, slope > 0)
        n = band_walk(df)[1]
        checks = {RR_RULE: plan['rr'] >= 1.0,
                  f"Силен тренд ({n} от последните {WALK_LOOKBACK} свещи са до лентата)": n >= WALK_MIN,
                  "Средната линия върви по посоката на сделката": bool(slope_ok),
                  f"RSI не е обърнал тренда ({c['rsi']:.0f})": bool(rsi_ok),
                  "Сянка срещу връщането >= половината от тялото": wick >= 0.5 * body}
    score = sum(checks.values())
    verdict = "TAKE" if score >= 4 and checks[RR_RULE] else "SKIP"
    ev, now, win = usd_events(), datetime.now(timezone.utc), timedelta(minutes=NEWS_WINDOW_MIN)
    near = [e for e in ev if abs(e["time"] - now) <= win] if ev is not None else None
    nxt = [e for e in ev if now - win <= e["time"] <= now + timedelta(hours=24)] if ev is not None else []
    if near:
        verdict = "SKIP"
    facts = dict(signal_type=plan['type'], band_walk=walk, rsi=round(float(c['rsi']), 1),
                 band_width=round(float(c['width']), 2), avg_band_width_50=round(float(avg_w), 2),
                 middle_slope=round(float(slope), 2), calendar_ok=ev is not None,
                 news_now=[fmt_event(e) for e in near or []], news_next_24h=[fmt_event(e) for e in nxt])
    return checks, score, verdict, facts


def ai_review(df, plan, checks, facts, news):
    if not (AI_ENABLED and API_KEY):
        return None
    h = df.iloc[-(AI_CANDLES + 2):-1][['time', 'open', 'high', 'low', 'close', 'upper', 'middle', 'lower', 'rsi']].copy()
    h['time'] = h['time'].dt.strftime('%m-%d %H:%M')
    msg = (f"Signal: {plan['side']} {plan['type']} on the candle closed at {plan['time']}.\n"
           f"Plan: entry {plan['entry']:.2f}, stop {plan['sl']:.2f}, target {plan['tp']:.2f}, lot {plan['lot']}, "
           f"risk ${plan['risk']:.2f}, reward ${plan['reward']:.2f}, R:R {plan['rr']:.2f}.\n"
           f"Rules: {json.dumps({k: bool(v) for k, v in checks.items()}, ensure_ascii=False)}\n"
           f"Facts: {json.dumps(facts, ensure_ascii=False)}\nNews: {news or 'not available'}\n\n"
           f"Last {AI_CANDLES} completed M5 candles:\n{h.round(2).to_csv(index=False)}")
    data, text = call_ai(AI_SYSTEM, msg)
    if not data:
        return {"error": text}
    try:
        return json.loads(text[text.index("{"):text.rindex("}") + 1])
    except Exception:
        log.error(f"AI reply had no valid JSON (stop_reason={data.get('stop_reason')}): {text[:500]!r}")
        return {"error": f"no JSON, stop_reason={data.get('stop_reason')}"}


def place_trade(p):
    req = {"action": mt5.TRADE_ACTION_DEAL, "symbol": SYMBOL, "volume": float(p['lot']), "price": p['entry'],
           "type": mt5.ORDER_TYPE_BUY if p['side'] == 'BUY' else mt5.ORDER_TYPE_SELL,
           "sl": round(p['sl'], 2), "tp": round(p['tp'], 2), "magic": MAGIC, "deviation": DEVIATION,
           "comment": f"BB_{p['type'][:3]}_{p['side']}", "type_time": mt5.ORDER_TIME_GTC}
    for f in (mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_RETURN):
        res = mt5.order_send({**req, "type_filling": f})
        if res and res.retcode == mt5.TRADE_RETCODE_DONE:
            return log.info(f"✅ Order placed: {p['side']} {p['lot']}")
        if not res or res.retcode != 10030:
            break
    log.error(f"Order failed: {mt5.last_error()}")


def message(p, checks, score, verdict, ai, f, news):
    walk = {"DOWN": "пълзене по долната лента (низходящ тренд)",
            "UP": "пълзене по горната лента (възходящ тренд)"}.get(f['band_walk'], "няма тренд по лентата")
    L = [f"{SIDE_BG[p['side']]} {SYMBOL} - {TYPE_BG[p['type']]} сигнал  (свещта затвори {p['time'] + timedelta(minutes=5):%H:%M}, изпратено {datetime.now():%H:%M:%S})", f"Пазар: {walk}",
         f"Вход ~ {p['entry']:.2f}", f"Стоп (SL) {p['sl']:.2f}  |  Цел (TP) {p['tp']:.2f} ({p['tp_name']})",
         f"Лот {p['lot']}  |  Риск ${p['risk']:.0f}  |  Печалба ${p['reward']:.0f}  |  R:R {p['rr']:.2f}",
         "", f"Правила {score}/5 -> {VERDICT_BG[verdict]}"]
    L += [f"{'✅' if ok else '❌'} {name}" for name, ok in checks.items()]
    if not f['calendar_ok']:
        L.append("⚠️ Календарът с новини не се зареди - провери го сам")
    elif f['news_now']:
        L += [f"❌ Важна новина за долара до {NEWS_WINDOW_MIN} мин -> ПРОПУСНИ"] + [f"    {e}" for e in f['news_now']]
    else:
        L.append(f"✅ Няма важна новина за долара до {NEWS_WINDOW_MIN} мин")
    if f['news_next_24h']:
        L += ["", "Новини за долара в следващите 24 ч:"] + [f"  {e}" for e in f['news_next_24h'][:5]]
    if news:
        L += ["", "Новини:", news]
    if ai and "error" not in ai:
        L += ["", f"AI: {VERDICT_BG.get(ai.get('decision'), ai.get('decision'))} (увереност {ai.get('confidence')}/5)",
              ai.get('summary_bg', '')]
        if ai.get('invalidation'):
            L.append(f"Сделката е грешна, ако: {ai['invalidation']}")
    elif ai:
        L += ["", f"AI проверката не се получи ({ai['error']}). Виж CMD прозореца."]
    L += ["", f"Дневен лимит ${DAILY_LIMIT:.0f}: макс. {int(DAILY_LIMIT // RISK_USD)} губещи сделки днес."]
    return "\n".join(L)


def push_text(p, score, verdict, final, ai):
    cut = lambda t: t if len(t) <= 255 else t[:252] + "..."
    sig = (f"{'🟢' if p['side'] == 'BUY' else '🔴'} {p['side']} {SYMBOL} ({TYPE_BG[p['type']]}) - {VERDICT_BG.get(final, final)}\n"
           f"Вход {p['entry']:.2f}\nSL {p['sl']:.2f}\nTP {p['tp']:.2f}\n"
           f"Лот {p['lot']} | Риск ${p['risk']:.0f} | R:R {p['rr']:.2f} | Правила {score}/5")
    if ai and "error" not in ai:
        adv = f"🤖 AI: {VERDICT_BG.get(ai.get('decision'), ai.get('decision'))} ({ai.get('confidence')}/5)\n{ai.get('summary_bg', '')}"
        if ai.get('invalidation'):
            adv += f"\nГрешна, ако: {ai['invalidation']}"
        return [cut(sig), cut(adv)]
    return [cut(sig), "🤖 AI: няма отговор, виж CMD"] if ai else [cut(sig)]


def iteration():
    df = get_candles()
    if df.empty or pd.isna(df.iloc[-2]['upper']) or df.iloc[-2]['time'] == state["candle"]:
        return
    c = df.iloc[-2]
    state["candle"] = c['time']
    side, kind, walk = find_signal(df)
    if walk != state["walk"]:
        state["walk"] = walk
        log.info(f"Band walk: {walk}" if walk else "No band walk")
    if side is None:
        if time.time() - state["wait_log"] >= 60:
            state["wait_log"] = time.time()
            log.info(f"⏳ No signal | Close {c['close']:.2f} | Upper {c['upper']:.2f}, Mid {c['middle']:.2f}, "
                     f"Lower {c['lower']:.2f} | Walk: {walk or 'none'}")
        return
    plan = build_plan(df, side, kind)
    if plan is None:
        return log.info(f"{side} {kind} skipped: price already past the stop or the target")
    checks, score, verdict, facts = checklist(df, plan, walk)
    if QUIET_MODE and verdict == "SKIP":
        return log.info(f"🔇 {side} {kind}: rules {score}/5 -> SKIP (quiet mode)")
    news = news_summary()
    ai = ai_review(df, plan, checks, facts, news)
    final = "SKIP" if verdict == "SKIP" else (ai.get('decision', verdict) if ai and "error" not in ai else verdict)
    notify(f"{'🟢' if side == 'BUY' else '🔴'} Сигнал {side} ({TYPE_BG[kind]}) - {VERDICT_BG.get(final, final)}",
           message(plan, checks, score, verdict, ai, facts, news),
           copy=[("Вход", f"{plan['entry']:.2f}"), ("SL", f"{plan['sl']:.2f}"), ("TP", f"{plan['tp']:.2f}")],
           push=push_text(plan, score, verdict, final, ai))
    if AUTO_TRADE and final == "TAKE":
        place_trade(plan)


def run():
    while not mt5.initialize():
        log.error(f"MT5 init failed: {mt5.last_error()}. Retrying in 10s...")
        time.sleep(10)
    mt5.symbol_select(SYMBOL, True)
    log.info(f"🚀 BB signal bot | {SYMBOL} M5 | AUTO_TRADE={AUTO_TRADE} | AI={AI_PROVIDER if AI_ENABLED and API_KEY else 'off'} | QUIET={QUIET_MODE}")
    notify("🚀 Ботът е включен", f"Следя {SYMBOL} на M5: обратни сигнали и сигнали по тренда. Сделките ги отваряш ти.",
           push=f"🚀 Ботът е включен. Следя {SYMBOL} M5.")
    try:
        while True:
            try:
                if mt5.terminal_info() is None:
                    mt5.shutdown()
                    if not mt5.initialize():
                        time.sleep(10)
                        continue
                iteration()
            except Exception as e:
                log.error(f"Error (bot continues): {e}", exc_info=True)
                time.sleep(5)
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Stopped by user.")
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    run()
