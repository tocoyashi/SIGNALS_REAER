import os
import json
import re
import ccxt
import requests
from datetime import datetime

# ─── Config ─────────────────────────────────────────────
# Only TRACKER_TOKEN is needed now!
# The bot works in ANY channel it's added to as admin — no CHANNEL_ID required.
TELEGRAM_TOKEN = os.environ.get("TRACKER_TOKEN")
SIGNALS_FILE = "active_signals.json"


# ─── Storage ────────────────────────────────────────────
def load_signals():
    if os.path.exists(SIGNALS_FILE):
        with open(SIGNALS_FILE, 'r') as f:
            return json.load(f)
    return {}


def save_signals(signals):
    with open(SIGNALS_FILE, 'w') as f:
        json.dump(signals, f, indent=2)


# ─── Telegram API ───────────────────────────────────────
def send_message(chat_id, text, reply_to_id=None):
    """Send a message to a specific channel (identified by chat_id)."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        'chat_id': chat_id,
        'text': text,
        'parse_mode': 'HTML',
    }
    if reply_to_id:
        payload['reply_to_message_id'] = reply_to_id
    try:
        requests.post(url, json=payload, timeout=15)
    except Exception as e:
        print(f"  Error sending message to {chat_id}: {e}")


def get_all_posts():
    """
    Get ALL recent posts from ANY channel the bot is admin of.
    Works with both 'channel_post' (channels) and 'message' (groups).
    No CHANNEL_ID filter — the bot reads everything it has access to.
    """
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    all_posts = []
    try:
        resp = requests.post(url, json={'limit': 100, 'timeout': 5}, timeout=15)
        if not resp.json().get('ok'):
            return []

        updates = resp.json().get('result', [])

        # Confirm all updates so they don't re-appear
        if updates:
            last_id = updates[-1].get('update_id', 0)
            requests.post(url, json={'offset': last_id + 1, 'limit': 1}, timeout=10)

        for update in updates:
            # Channel posts use 'channel_post', groups use 'message'
            post = update.get('channel_post') or update.get('message')
            if not post:
                continue

            # Skip private messages (only process channels and groups)
            chat_type = post.get('chat', {}).get('type', '')
            if chat_type not in ('channel', 'group', 'supergroup'):
                continue

            all_posts.append(post)

        return all_posts
    except Exception as e:
        print(f"  Error getting posts: {e}")
        return []


# ─── Signal Parser ──────────────────────────────────────
def _extract_symbol(text_upper):
    """Extract trading pair symbol from text (many formats). Returns e.g. 'BTC/USDT' or None."""
    # Words to ignore (false positives)
    SKIP = {'LEVERAGE', 'STRATEGY', 'TARGET', 'SIGNAL', 'AVERAGE', 'ABOVE',
            'BELOW', 'SHORT', 'LONG', 'POINT', 'FIRST', 'SECOND', 'THIRD',
            'FOURTH', 'FIFTH', 'TRADE', 'PRICE', 'MARKET', 'ORDER', 'CLOSE',
            'OPEN', 'BREAK', 'LEVEL', 'AREA', 'ZONE', 'SETUP', 'TIME',
            'CHART', 'PATTERN', 'INDICATOR', 'CONFIRM', 'REJECT', 'HOLD',
            'SCALP', 'SWING', 'DAILY', 'WEEKLY', 'ALERT', 'UPDATE',
            'RESULT', 'PROFIT', 'LOSS', 'RISK', 'REWARD', 'EXCHANGE',
            'COIN', 'AUTOMATED', 'TYPE', 'TRACKING', 'SIGNALS', 'STRENGTH',
            'ASSET', 'EXCHANGES', 'ISOLATED', 'CROSS', 'BINGX'}

    # 0) Pairs: ENA/USDT, Pairs: BTCUSDT
    m = re.search(r'pairs\s*[:\-=]?\s*([A-Z0-9]{2,12})/?(USDT|BUSD|USDC|BTC|ETH)\b', text_upper)
    if m and m.group(1) not in SKIP:
        return f"{m.group(1)}/{m.group(2)}"

    # 1) BTC/USDT, ETH/USDT, USD1/USDT, 1INCH/USDT
    m = re.search(r'\b([A-Z0-9]{2,12})/(USDT|BUSD|USDC|BTC|ETH)\b', text_upper)
    if m and m.group(1) not in SKIP:
        return m.group(0)

    # 2) #BTCUSDT, #USD1USDT
    m = re.search(r'#([A-Z0-9]{3,12})(USDT|BUSD|USDC)\b', text_upper)
    if m and m.group(1) not in SKIP:
        return f"{m.group(1)}/{m.group(2)}"

    # 3) BTCUSDT, SOLUSDT (bare, after space/start/emoji)
    m = re.search(r'(?:^|[\s#])' + r'([A-Z0-9]{2,12})(USDT|BUSD|USDC)\b', text_upper)
    if m and m.group(1) not in SKIP:
        return f"{m.group(1)}/{m.group(2)}"

    return None


def _first_price(text, start=0):
    """Find first price-like number (with decimal point) in text starting from `start`."""
    m = re.search(r'([\d,]+\.\d+)', text[start:])
    if m:
        return float(m.group(1).replace(',', ''))
    # Fallback: integer price
    m = re.search(r'([\d,]+)', text[start:])
    if m:
        val = m.group(1).replace(',', '')
        if len(val) >= 2:  # at least 2 digits to avoid single-digit noise
            return float(val)
    return None


def _extract_entry(text):
    """Extract entry price (case-insensitive, many formats)."""
    # ── Bracket format: Entry = [ 0.0733 TO 0.0731 ] ──
    bracket_m = re.search(
        r'entry\s*(?:price|zone|level)?\s*[:\-=]?\s*\[\s*([\d,]+\.\d+)\s*(?:TO|to|—|-|~)\s*([\d,]+\.\d+)\s*\]',
        text, re.IGNORECASE
    )
    if bracket_m:
        return float(bracket_m.group(1).replace(',', ''))

    patterns = [
        r'entry\s*(?:price|zone|level|target|point)?\s*[:\-=]?\s*',
        r'buy\s*(?:zone|price|level|area)?\s*[:\-=]?\s*',
        r'sell\s*(?:zone|price|level|area)?\s*[:\-=]?\s*',
        r'(?:open|entry)\s*[@=]\s*',
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            after = text[m.end():]
            # Skip parenthetical like (TP1) or (limit)
            skip = re.match(r'\s*\([^)]*\)\s*', after)
            start = skip.end() if skip else 0
            after = after[start:]
            # Numbered list: 1) 16.80 2) 16.29
            num_m = re.match(r'(?:\d+\s*[:)\-\.]\s*)+([\d,]+\.\d+)', after)
            if num_m:
                return float(num_m.group(1).replace(',', ''))
            price = _first_price(after)
            if price is not None:
                return price
    return None


def _extract_tps(text):
    """Extract TP levels — supports many label formats + dash-separated on one line."""
    tps = {}

    # ── Method 1: Labeled targets (Target 1:, TP1:, Take Profit 1:, T1:) ──
    label_pats = [
        lambda i: rf'target\s*{i}\s*[:\-=]?',
        lambda i: rf'take[ \t]*profit[ \t]*{i}\s*[:\-=]?',
        lambda i: rf'tp\s*{i}\s*[:\-=]?',
        lambda i: rf'\bT{i}\s*[:\-=]?',
    ]
    for i in range(1, 11):
        for make_pat in label_pats:
            m = re.search(make_pat(i), text, re.IGNORECASE)
            if m:
                after = text[m.end():]
                # Skip parenthetical like (TP1)
                skip = re.match(r'\s*\([^)]*\)\s*', after)
                start = skip.end() if skip else 0
                price = _first_price(after, start)
                if price is not None:
                    tps[f'tp{i}'] = price
                    break

    # ── Method 2: TARGETS: 4.612 - 4.528 - 4.388 (all on one line) ──
    if not tps:
        m = re.search(r'targets?\s*[:\-=]?\s*([\d,]+\.?\d*(?:\s*[-–—]\s*[\d,]+\.?\d*)+)', text, re.IGNORECASE)
        if m:
            prices = re.findall(r'([\d,]+(?:\.\d+)?)', m.group(1))
            for idx, p in enumerate(prices, 1):
                tps[f'tp{idx}'] = float(p.replace(',', ''))

    # ── Method 3: Numbered list after label (1) val\n2) val) ──
    # Handles: Targets: 1) 16.89 2) 17.24  and  Take Profit ☄\n1) 42.79
    if not tps:
        m = re.search(r'(?:targets?|take\s*profit|tps?)\b', text, re.IGNORECASE)
        if m:
            after = text[m.end():]
            # Skip non-numeric junk (emojis, symbols, spaces, colons) until numbers
            skip = re.match(r'[^0-9]*', after)
            start = skip.end() if skip else 0
            nums = re.findall(r'\d+\s*[:)\-]\s*([\d,]+(?:\.\d+)?)', after[start:])
            for idx, p in enumerate(nums, 1):
                tps[f'tp{idx}'] = float(p.replace(',', ''))

    # ── Method 4: Bracket comma-separated [ 0.0745, 0.0753, 0.0763 ] ──
    if not tps:
        m = re.search(
            r'(?:take\s*profit|targets?|tps?)\b[^\[]*?\[\s*([\d,]+\.\d+(?:\s*,\s*[\d,]+\.\d+)*)\s*\]',
            text, re.IGNORECASE
        )
        if m:
            prices = re.findall(r'([\d,]+\.\d+)', m.group(1))
            for idx, p in enumerate(prices, 1):
                tps[f'tp{idx}'] = float(p.replace(',', ''))

    return tps


def _extract_sl(text):
    """Extract stop loss (many formats, case-insensitive)."""
    patterns = [
        r'(?:stop\s*-?\s*loss|stoploss|\bSL\b|\bstop\b|\brisk\b)\s*[:\-=]?\s*',
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            after = text[m.end():]
            # Skip emojis/symbols like ⛔️
            skip = re.match(r'[^0-9]*', after)
            start = skip.end() if skip else 0
            price = _first_price(after, start)
            if price is not None:
                return price
    return None


def parse_signal(text):
    """
    Universal signal parser — supports many formats from different channels.

    Extracts: direction, symbol, entry, TP1-TP10, SL
    """
    text_upper = text.upper()

    # ── Direction ──
    if re.search(r'\bLONG\b|\bBUY\b', text_upper):
        direction = 'LONG'
    elif re.search(r'\bSHORT\b|\bSELL\b', text_upper):
        direction = 'SHORT'
    else:
        return None

    # ── Symbol ──
    symbol = _extract_symbol(text_upper)
    if not symbol:
        return None

    # ── Entry ──
    entry = _extract_entry(text)
    if entry is None:
        return None

    # ── TPs ──
    tps = _extract_tps(text)

    # ── SL ──
    sl = _extract_sl(text)

    # Must have at least one TP and SL
    if not tps or sl is None:
        return None

    return {
        'direction': direction,
        'symbol': symbol,
        'entry': entry,
        **tps,
        'sl': sl,
    }


# ─── Price Fetcher ──────────────────────────────────────
_exchange = None

def get_exchange():
    global _exchange
    if _exchange is None:
        _exchange = ccxt.mexc({'enableRateLimit': True})
    return _exchange


def get_current_price(symbol):
    """Fetch current price from MEXC."""
    try:
        ticker = get_exchange().fetch_ticker(symbol)
        return ticker['last']
    except Exception as e:
        print(f"  Error fetching {symbol} price: {e}")
        return None


# ─── TP/SL Checker ──────────────────────────────────────
def check_signals(active_signals):
    """Check all active signals against current prices. Send alerts to the correct channel."""
    completed = []

    for sig_id, sig in active_signals.items():
        symbol = sig['symbol']
        direction = sig['direction']
        entry = sig['entry']
        sl = sig.get('sl')
        chat_id = sig['chat_id']     # Send alert to the SAME channel

        price = get_current_price(symbol)
        if price is None:
            continue

        # Collect all TP levels
        tps = []
        for i in range(1, 11):
            key = f'tp{i}'
            if key in sig:
                tps.append((f'TP{i}', sig[key]))

        hit_tps = sig.get('hit_tps', [])
        entry_hit = sig.get('entry_hit', False)

        # ── Check Entry Price (one-time alert) ──
        if not entry_hit:
            entry_triggered = False
            if direction == 'LONG' and price >= entry:
                entry_triggered = True
            elif direction == 'SHORT' and price <= entry:
                entry_triggered = True

            if entry_triggered:
                sig['entry_hit'] = True
                msg = (
                    f"⚡ <b>ENTRY REACHED!</b>\n\n"
                    f"📊 <code>{symbol}</code> | {direction}\n"
                    f"💰 Entry: <code>{entry}</code>\n"
                    f"💵 Current: <code>{price}</code>\n"
                    f"⏰ {datetime.now().strftime('%H:%M:%S')}\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"🔍 Now monitoring TP/SL..."
                )
                send_message(chat_id, msg, reply_to_id=sig['message_id'])
                print(f"  ⚡ {symbol} ENTRY REACHED at {entry} (channel {chat_id})")

        # Check each TP
        for tp_name, tp_price in tps:
            if tp_name in hit_tps:
                continue

            triggered = False
            if direction == 'LONG' and price >= tp_price:
                triggered = True
            elif direction == 'SHORT' and price <= tp_price:
                triggered = True

            if triggered:
                hit_tps.append(tp_name)
                if direction == 'LONG':
                    pnl = ((tp_price - entry) / entry) * 100
                else:
                    pnl = ((entry - tp_price) / entry) * 100

                emoji = {"TP1": "🟩", "TP2": "🟦", "TP3": "🟪", "TP4": "🟧", "TP5": "⬜"}.get(tp_name, "✅")
                msg = (
                    f"{emoji} <b>{tp_name} HIT!</b>\n\n"
                    f"📊 <code>{symbol}</code> | {direction}\n"
                    f"🎯 {tp_name}: <code>{tp_price}</code>\n"
                    f"💵 Current: <code>{price}</code>\n"
                    f"📈 PnL: <code>+{pnl:.2f}%</code>\n"
                    f"⏰ {datetime.now().strftime('%H:%M:%S')}"
                )
                send_message(chat_id, msg, reply_to_id=sig['message_id'])
                print(f"  {emoji} {symbol} {tp_name} HIT at {tp_price} (channel {chat_id})")

        sig['hit_tps'] = hit_tps

        # Check SL (only if not all TPs hit)
        all_tp_hit = len(hit_tps) >= len(tps) if tps else False
        if not all_tp_hit and sl:
            sl_hit = False
            if direction == 'LONG' and price <= sl:
                sl_hit = True
            elif direction == 'SHORT' and price >= sl:
                sl_hit = True

            if sl_hit:
                if direction == 'LONG':
                    pnl = ((sl - entry) / entry) * 100
                else:
                    pnl = ((entry - sl) / entry) * 100

                msg = (
                    f"🛑 <b>STOP LOSS HIT!</b>\n\n"
                    f"📊 <code>{symbol}</code> | {direction}\n"
                    f"🛑 SL: <code>{sl}</code>\n"
                    f"💵 Current: <code>{price}</code>\n"
                    f"📉 PnL: <code>{pnl:.2f}%</code>\n"
                    f"⏰ {datetime.now().strftime('%H:%M:%S')}"
                )
                send_message(chat_id, msg, reply_to_id=sig['message_id'])
                print(f"  🛑 {symbol} SL HIT at {sl} (channel {chat_id})")
                completed.append(sig_id)
                continue

        # Remove signal if all TPs reached
        if all_tp_hit:
            completed.append(sig_id)

    # Remove completed signals
    for sig_id in completed:
        active_signals.pop(sig_id, None)

    return active_signals


# ─── Main ───────────────────────────────────────────────
def main():
    print(f"=== Signal Tracker — {datetime.now().strftime('%Y-%m-%d %H:%M')} ===")

    if not TELEGRAM_TOKEN:
        print("  ERROR: TRACKER_TOKEN not set!")
        return

    # 1. Load existing active signals
    active = load_signals()
    print(f"  Active signals: {len(active)}")

    # 2. Get ALL posts from ALL channels the bot is in
    posts = get_all_posts()
    print(f"  Posts found: {len(posts)}")

    # 3. Parse new signals from posts
    new_count = 0
    for post in posts:
        msg_id = post.get('message_id')
        chat_id = post.get('chat', {}).get('id')
        chat_title = post.get('chat', {}).get('title', 'Unknown')
        chat_type = post.get('chat', {}).get('type', '')

        text = post.get('text', '') or post.get('caption', '')
        if not text:
            continue

        # Unique key = chat_id + message_id (avoids conflicts between channels)
        key = f"{chat_id}:{msg_id}"
        if key in active:
            continue  # Already tracking this signal

        parsed = parse_signal(text)
        if not parsed:
            continue

        # Store chat info with the signal
        parsed['chat_id'] = chat_id
        parsed['message_id'] = msg_id
        parsed['hit_tps'] = []
        parsed['time'] = datetime.now().isoformat()
        parsed['channel_title'] = chat_title
        active[key] = parsed
        new_count += 1

        # ─── Fetch current price for the reply ───
        current_price = get_current_price(parsed['symbol'])

        # ─── REPLAY: Reply to the original signal in the SAME channel ───
        d = "🟢" if parsed['direction'] == 'LONG' else "🔴"
        confirm = (
            f"👁 <b>Tracking Signal #{msg_id}</b> {d}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 <code>{parsed['symbol']}</code> | <b>{parsed['direction']}</b>\n"
            f"💰 Entry: <code>{parsed['entry']}</code>"
        )
        if current_price is not None:
            confirm += f"\n💵 Current: <code>{current_price}</code>"
        if 'sl' in parsed:
            confirm += f"\n🛑 SL: <code>{parsed['sl']}</code>"
        for i in range(1, 11):
            tp_key = f'tp{i}'
            if tp_key in parsed:
                confirm += f"\n🎯 TP{i}: <code>{parsed[tp_key]}</code>"
        confirm += f"\n━━━━━━━━━━━━━━━━━━━━\n⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"

        send_message(chat_id, confirm, reply_to_id=msg_id)
        print(f"  ✅ Tracking: {parsed['symbol']} {parsed['direction']} in [{chat_title}] (#{msg_id})")

    if new_count:
        print(f"  New signals tracked: {new_count}")

    # 4. Save signals
    save_signals(active)

    # 5. Check all active signals for TP/SL hits
    if active:
        print(f"\n  Checking {len(active)} active signal(s)...")
        active = check_signals(active)
        save_signals(active)
        remaining = len(active)
        print(f"  Remaining active: {remaining}")
    else:
        print("\n  No active signals to check.")

    print(f"\n=== Done ===")


if __name__ == "__main__":
    main()
