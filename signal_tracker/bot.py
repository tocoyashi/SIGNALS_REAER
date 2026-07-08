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
def parse_signal(text):
    """
    Parse a signal message and extract:
      - direction (LONG/BUY or SHORT/SELL)
      - symbol (e.g. BTC/USDT or #BTCUSDT)
      - entry price
      - TP1-TP4 (optional)
      - SL (stop loss)

    Supports multiple formats:
      Format 1:  LONG BTC/USDT | Entry: 65000 | TP1: 66000 | SL: 63000
      Format 2:  #STRKUSDT | Long Entry Zone: 0.03008 | Target 1: 0.03036 | Stop-Loss: 0.02862
    """
    data = {}
    text_upper = text.upper()

    # ── Direction ──
    if 'LONG' in text_upper or 'BUY' in text_upper:
        data['direction'] = 'LONG'
    elif 'SHORT' in text_upper or 'SELL' in text_upper:
        data['direction'] = 'SHORT'
    else:
        return None

    # ── Symbol ──
    # Try format 1: BTC/USDT, ETH/USDT, etc.
    symbol_match = re.search(r'([A-Z]{2,12}/(?:USDT|BUSD|USDC))', text_upper)
    if symbol_match:
        data['symbol'] = symbol_match.group(1)
    else:
        # Try format 2: #BTCUSDT, #STRKUSDT, etc.
        hash_match = re.search(r'#([A-Z]{3,12})(USDT|BUSD|USDC)\b', text_upper)
        if hash_match:
            data['symbol'] = f"{hash_match.group(1)}/{hash_match.group(2)}"
        else:
            return None

    # ── Entry Price ──
    entry_match = re.search(r'[Ee]ntry\s*(?:Zone)?\s*[:\-]?\s*([\d,]+\.?\d*)', text)
    if entry_match:
        data['entry'] = float(entry_match.group(1).replace(',', ''))
    else:
        return None

    # ── TP levels ──
    # Support: "TP1:", "TP 1:", "Target 1:", "Target1:", "🎯 1:"
    for i in range(1, 5):
        key = f'tp{i}'
        # Try "Target N" first (format 2)
        tp = re.search(rf'Target\s*{i}\s*[:\-]?\s*([\d,]+\.?\d*)', text, re.IGNORECASE)
        if not tp:
            # Try "TP N" (format 1)
            tp = re.search(rf'TP\s*{i}\s*[:\-]?\s*([\d,]+\.?\d*)', text, re.IGNORECASE)
        if tp:
            data[key] = float(tp.group(1).replace(',', ''))

    # ── SL ──
    # Support: "SL:", "Stop-Loss:", "Stop Loss:", "🔺 Stop-Loss:"
    sl = re.search(
        r'(?:SL|Stop\s*-?\s*Loss)\s*[:\-]?\s*([\d,]+\.?\d*)',
        text, re.IGNORECASE
    )
    if sl:
        data['sl'] = float(sl.group(1).replace(',', ''))

    # Must have at least one TP and SL
    has_tp = any(f'tp{i}' in data for i in range(1, 5))
    has_sl = 'sl' in data
    return data if has_tp and has_sl else None


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
        for i in range(1, 5):
            key = f'tp{i}'
            if key in sig:
                tps.append((f'TP{i}', sig[key]))

        hit_tps = sig.get('hit_tps', [])

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

                emoji = {"TP1": "🟩", "TP2": "🟦", "TP3": "🟪", "TP4": "🟧"}.get(tp_name, "✅")
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

        # ─── REPLAY: Reply to the original signal in the SAME channel ───
        d = "🟢" if parsed['direction'] == 'LONG' else "🔴"
        confirm = (
            f"👁 <b>Tracking Signal #{msg_id}</b> {d}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 <code>{parsed['symbol']}</code> | <b>{parsed['direction']}</b>\n"
            f"💰 Entry: <code>{parsed['entry']}</code>"
        )
        if 'sl' in parsed:
            confirm += f"\n🛑 SL: <code>{parsed['sl']}</code>"
        for i in range(1, 5):
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
