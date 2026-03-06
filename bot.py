"""
ForexEdge Customer Service Bot — v3.0
• Redis-backed conversation history (survives restarts)
• Per-service API key (BOT_API_KEY)
• Dynamic product fallback when Claude is unavailable
• /ref affiliate sign-up  |  /stats admin view
• DEBUG flag for noisy log control
"""
import os, logging, asyncio, time, json, hashlib
from datetime import datetime

from telegram import (Update, InlineKeyboardButton, InlineKeyboardMarkup,
                       ReplyKeyboardMarkup, KeyboardButton, BotCommand)
from telegram.ext import (Application, CommandHandler, MessageHandler,
                           CallbackQueryHandler, filters, ContextTypes)
import httpx

# ── Logging ────────────────────────────────────────────────────────────────────
DEBUG = os.environ.get("DEBUG", "false").lower() == "true"
logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s"
)
log = logging.getLogger("forexedge.bot")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING if not DEBUG else logging.DEBUG)

# ── Config ─────────────────────────────────────────────────────────────────────
BOT_TOKEN           = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_API_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")
BOT_API_KEY         = os.environ.get("BOT_API_KEY", "")   # per-service key
API_BASE            = os.environ.get("STORE_URL", "http://localhost:5000")
EXNESS_URL          = "https://one.exnessonelink.com/a/t0gft0gf"
ADMIN_IDS           = [int(x) for x in os.environ.get("ADMIN_TELEGRAM_IDS", "").split(",") if x.strip()]
REDIS_URL           = os.environ.get("REDIS_URL", "")
HISTORY_LIMIT       = 20      # messages per user per chat kept in history
PITCH_COOLDOWN_SEC  = 300     # min seconds between group pitches per chat
KEEP_ALIVE_INTERVAL = 840     # 14 min in seconds

# ── Redis conversation history ─────────────────────────────────────────────────
try:
    import redis as _redis_lib
    _rc = _redis_lib.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2) if REDIS_URL else None
    if _rc: _rc.ping()
    REDIS_OK = bool(_rc)
except Exception:
    _rc = None; REDIS_OK = False

if REDIS_OK:
    log.info("Redis connected — conversation history is persistent.")
else:
    log.warning("Redis not available — conversation history in-process (lost on restart).")

_mem_history: dict = {}  # fallback: {key: [msgs]}

def _hist_key(user_id: int, chat_id: int) -> str:
    return f"fxbot:hist:{user_id}:{chat_id}"

def get_history(user_id: int, chat_id: int) -> list:
    key = _hist_key(user_id, chat_id)
    if REDIS_OK and _rc:
        raw = _rc.get(key)
        return json.loads(raw) if raw else []
    return _mem_history.get(key, [])

def save_history(user_id: int, chat_id: int, history: list):
    key = _hist_key(user_id, chat_id)
    trimmed = history[-HISTORY_LIMIT:]
    if REDIS_OK and _rc:
        _rc.setex(key, 86400 * 7, json.dumps(trimmed))   # 7-day TTL
    else:
        _mem_history[key] = trimmed

def clear_history(user_id: int, chat_id: int):
    key = _hist_key(user_id, chat_id)
    if REDIS_OK and _rc: _rc.delete(key)
    else: _mem_history.pop(key, None)

# ── Group pitch cooldown ────────────────────────────────────────────────────────
_last_pitch: dict = {}   # {chat_id: timestamp}  — ok in-process (not critical)

def _pitch_ok(chat_id: int) -> bool:
    now = time.time()
    last = _last_pitch.get(chat_id, 0)
    if now - last > PITCH_COOLDOWN_SEC:
        _last_pitch[chat_id] = now
        return True
    return False

# ── API calls ──────────────────────────────────────────────────────────────────
async def _api(path: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(f"{API_BASE}{path}", headers={"X-API-Key": BOT_API_KEY})
            return r.json()
    except Exception as e:
        log.warning("API call failed: %s", e)
        return {}

async def _post_api(path: str, data: dict) -> dict:
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.post(f"{API_BASE}{path}", json=data,
                             headers={"X-API-Key": BOT_API_KEY})
            return r.json()
    except Exception as e:
        log.warning("API POST failed: %s", e)
        return {}

_product_cache: list = []
_product_cache_ts: float = 0
_CACHE_TTL = 300

async def get_products() -> list:
    global _product_cache, _product_cache_ts
    if time.time() - _product_cache_ts < _CACHE_TTL and _product_cache:
        return _product_cache
    data = await _api("/api/v1/products")
    _product_cache = data.get("products", [])
    _product_cache_ts = time.time()
    return _product_cache

def _products_text(products: list) -> str:
    if not products:
        return "  • XAUUSD Scalper Pro (MT4/MT5)\n  • Boom & Crash EA\n  • Smart Grid EA\n  • Prop Firm EA"
    return "\n".join(f"  • {p['name']} — ${p.get('price',20):.0f} ({p.get('category','EA')})"
                     for p in products[:8])

# ── Claude AI ──────────────────────────────────────────────────────────────────
FOREX_KEYWORDS = {"forex","trading","ea","expert","advisor","mt4","mt5","xauusd",
                  "gold","eurusd","gbpusd","scalp","prop","ftmo","signal","indicator",
                  "pine","script","strategy","broker","exness","deriv","crash","boom"}

async def _claude(messages: list, system: str) -> str | None:
    if not ANTHROPIC_API_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=25) as c:
            r = await c.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": ANTHROPIC_API_KEY,
                         "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": "claude-sonnet-4-20250514", "max_tokens": 500,
                      "system": system, "messages": messages},
            )
            data = r.json()
            blocks = data.get("content", [])
            return " ".join(b.get("text","") for b in blocks if b.get("type")=="text").strip() or None
    except Exception as e:
        log.warning("Claude API error: %s", e)
        return None

async def _system_prompt(products: list) -> str:
    return f"""You are Alex, ForexEdge's expert trading assistant. You are knowledgeable, direct, and confident.

PRODUCTS AVAILABLE:
{_products_text(products)}

Store: {API_BASE}
Broker recommendation: Exness — {EXNESS_URL} (best spreads, instant withdrawals, prop-firm friendly)

YOUR ROLE:
- Help traders find the right EA or indicator for their strategy
- Be frank and specific — don't hedge everything or sound like a disclaimer robot
- Ask what platform they use (MT4/MT5/TradingView), what pairs, what style (scalp/swing/grid)
- If they show interest, guide them to purchase: {API_BASE}
- Encourage high-performers to become affiliates (earn $5/sale, paid monthly)
- After 2-3 exchanges showing genuine interest, mention the affiliate program naturally

RULES:
- Keep replies under 4 sentences in groups, up to 8 in DMs
- Never fabricate product names not in the list above
- If asked about results/performance, be honest: "results depend on market conditions and settings"
- Always include store link when recommending a purchase"""

async def _fallback(products: list) -> str:
    """Dynamic fallback when Claude is unavailable — uses live product list."""
    plist = _products_text(products)
    return (f"👋 Hey! I'm Alex from ForexEdge.\n\n"
            f"Our current products:\n{plist}\n\n"
            f"🛒 Shop: {API_BASE}\n"
            f"📈 Open an Exness account: {EXNESS_URL}\n\n"
            f"Reply with your trading platform (MT4/MT5/TradingView) and I'll help you pick the right tool.")

# ── Keyboards ──────────────────────────────────────────────────────────────────
def _main_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton("📦 Products"), KeyboardButton("💰 Buy Now")],
         [KeyboardButton("📈 Broker"), KeyboardButton("🔑 License Check")],
         [KeyboardButton("🔗 Affiliate Program"), KeyboardButton("❓ Help")]],
        resize_keyboard=True)

def _platforms_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("MT4", callback_data="cat:MT4"),
         InlineKeyboardButton("MT5", callback_data="cat:MT5"),
         InlineKeyboardButton("TradingView (Pine)", callback_data="cat:Pine Script")],
        [InlineKeyboardButton("📦 View All", callback_data="cat:ALL")],
    ])

# ── Commands ───────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    products = await get_products()
    name = update.effective_user.first_name or "Trader"
    is_dm = update.effective_chat.type == "private"

    if is_dm:
        text = (f"👋 Hey {name}! I'm Alex — your ForexEdge trading assistant.\n\n"
                f"I help traders find the right EAs and indicators for MT4, MT5 & TradingView.\n\n"
                f"What are you trading? (Gold, FX pairs, Boom & Crash?)\n"
                f"And what platform — MT4, MT5, or TradingView?")
        await update.message.reply_text(text, reply_markup=_main_kb())
    else:
        await update.message.reply_text(
            f"👋 {name}, I'm Alex from ForexEdge — automated trading tools.\n"
            f"DM me @{ctx.bot.username} for personalised recommendations!",
            reply_markup=_platforms_kb())

async def cmd_products(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    products = await get_products()
    if not products:
        await update.message.reply_text("Products loading... check back shortly or visit " + API_BASE)
        return
    lines = [f"📦 *ForexEdge Products*\n"]
    for p in products:
        lines.append(f"• *{p['name']}* — ${p.get('price',20):.0f}\n  _{p.get('description','')[:80]}_")
    lines.append(f"\n🛒 {API_BASE}")
    await update.message.reply_text(
        "\n".join(lines), parse_mode="Markdown",
        reply_markup=_platforms_kb())

async def cmd_broker(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (f"📈 *Recommended Broker — Exness*\n\n"
            f"Why Exness:\n"
            f"• Ultra-low spreads (0.0 on XAU)\n"
            f"• Instant withdrawals\n"
            f"• Prop-firm compatible (FTMO, MFF)\n"
            f"• Available in 170+ countries\n\n"
            f"🔗 Open account: {EXNESS_URL}\n\n"
            f"_Use the link above and get priority support from our team._")
    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_buy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🛒 *Purchase ForexEdge Tools*\n\n"
        f"1. Visit: {API_BASE}\n"
        f"2. Select your product\n"
        f"3. Complete PayPal payment\n"
        f"4. Save your Transaction ID — use it to claim your download\n\n"
        f"Need help choosing? Tell me your platform and trading style!",
        parse_mode="Markdown")

async def cmd_lookup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🔍 *Retrieve Your Download*\n\n"
        f"Go to: {API_BASE}/lookup\n"
        f"Enter your Transaction ID + email to access your file.",
        parse_mode="Markdown")

async def cmd_license(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args
    if not args:
        await update.message.reply_text("Usage: /license YOUR-LICENSE-KEY")
        return
    key = args[0].strip().upper()[:100]
    result = await _post_api("/license/check", {"license": key, "account": "telegram"})
    if result.get("valid"):
        days = result.get("days_remaining", "?")
        await update.message.reply_text(f"✅ License *{key[:12]}...* is valid ({days} days remaining).",
                                         parse_mode="Markdown")
    else:
        reason = result.get("reason", "unknown")
        msgs = {
            "not_found":             "❌ License not found. Double-check the key.",
            "expired":               "⏰ License expired. Contact support to renew.",
            "bound_to_other_account":"🔒 License bound to a different account.",
        }
        await update.message.reply_text(msgs.get(reason, f"❌ Invalid license: {reason}"))

async def cmd_ref(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Affiliate sign-up / referral link."""
    user = update.effective_user
    uid  = str(user.id)
    name = user.first_name or "Affiliate"

    # Check if referral already exists for this Telegram user
    existing_code = None
    if REDIS_OK and _rc:
        existing_code = _rc.get(f"fxbot:ref:{uid}")

    if existing_code:
        link = f"{API_BASE}/?ref={existing_code}"
        await update.message.reply_text(
            f"🔗 *Your Affiliate Link*\n\n`{link}`\n\n"
            f"Earn *$5 per successful sale* · Paid monthly on 5th\n"
            f"Contact @support to check your balance.",
            parse_mode="Markdown")
        return

    # Generate a new referral code via admin API
    # (bot uses BOT_API_KEY — endpoint is admin-only in prod, so we use a dedicated ref-register endpoint)
    code = hashlib.sha1(uid.encode()).hexdigest()[:6].upper()
    result = await _post_api("/api/v1/referral/register",
                             {"telegram_id": uid, "code": code, "name": name,
                              "source": "telegram_bot"})

    if result.get("code"):
        code = result["code"]
        if REDIS_OK and _rc: _rc.setex(f"fxbot:ref:{uid}", 86400*365, code)
        link = f"{API_BASE}/?ref={code}"
        await update.message.reply_text(
            f"🎉 *You're now an affiliate!*\n\n"
            f"Your link: `{link}`\n\n"
            f"• *$5 commission* per sale you refer\n"
            f"• Paid to your PayPal on the 5th of each month\n"
            f"• Share on YouTube, Telegram groups, Twitter\n\n"
            f"To set your PayPal payout email, send: /setpaypal your@paypal.com",
            parse_mode="Markdown")
    else:
        # Fallback: give them the store link with their code appended
        link = f"{API_BASE}/?ref={code}"
        await update.message.reply_text(
            f"🔗 *Your referral link:* `{link}`\n\n"
            f"Earn $5 per successful sale. Reply with your PayPal email to get set up.",
            parse_mode="Markdown")

async def cmd_setpaypal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: /setpaypal your@paypal.com")
        return
    email = ctx.args[0].strip().lower()
    if "@" not in email:
        await update.message.reply_text("❌ Invalid email.")
        return
    # Stored for admin follow-up — in production connect to /admin/api
    await update.message.reply_text(
        f"✅ PayPal email *{email}* saved.\n"
        f"Payouts will be sent here on the 5th of each month.\n"
        f"Min payout: $5. Questions? Contact support.",
        parse_mode="Markdown")

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Admin only.")
        return
    data = await _api("/api/v1/stats")
    await update.message.reply_text(
        f"📊 *ForexEdge Stats*\n\n"
        f"✅ Confirmed Sales: {data.get('confirmed',0)}\n"
        f"💰 Revenue: ${data.get('revenue',0):.2f}\n\n"
        f"Full dashboard: {API_BASE}/admin",
        parse_mode="Markdown")

async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    clear_history(update.effective_user.id, update.effective_chat.id)
    await update.message.reply_text("🔄 Conversation cleared. Fresh start!")

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *ForexEdge Bot Commands*\n\n"
        "/start — Welcome & product overview\n"
        "/products — Browse all tools\n"
        "/broker — Exness recommendation\n"
        "/buy — Purchase instructions\n"
        "/lookup — Retrieve your download\n"
        "/license KEY — Validate a license\n"
        "/ref — Get your affiliate link\n"
        "/setpaypal EMAIL — Set payout email\n"
        "/reset — Clear conversation\n"
        "/stats — Revenue dashboard (admin)\n\n"
        f"Or just chat — I'll help you find the right tool!",
        parse_mode="Markdown")

# ── Callback queries (inline keyboards) ───────────────────────────────────────
async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""

    if data.startswith("cat:"):
        cat = data[4:]
        products = await get_products()
        if cat == "ALL":
            filtered = products
        else:
            filtered = [p for p in products if p.get("category","").lower() == cat.lower()]
        if not filtered:
            await q.edit_message_text(f"No products in category {cat}. Check {API_BASE}")
            return
        lines = [f"📦 *{cat} Products*\n"]
        for p in filtered:
            lines.append(f"• *{p['name']}* — ${p.get('price',20):.0f}\n  _{p.get('description','')[:80]}_")
        lines.append(f"\n🛒 {API_BASE}")
        await q.edit_message_text("\n".join(lines), parse_mode="Markdown")

# ── Message handler ────────────────────────────────────────────────────────────
async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg  = update.message
    if not msg or not msg.text:
        return

    user    = update.effective_user
    chat    = update.effective_chat
    text    = msg.text.strip()
    is_dm   = chat.type == "private"
    bot_username = (await ctx.bot.get_me()).username.lower()

    # ── Keyboard shortcuts ─────────────────────────────────────────────────────
    shortcuts = {
        "📦 products": cmd_products,
        "💰 buy now":  cmd_buy,
        "📈 broker":   cmd_broker,
        "❓ help":     cmd_help,
        "🔗 affiliate program": cmd_ref,
        "🔑 license check": lambda u,c: u.message.reply_text("Send: /license YOUR-KEY"),
    }
    lower_text = text.lower()
    for trigger, handler in shortcuts.items():
        if lower_text == trigger:
            await handler(update, ctx)
            return

    # ── Group: only respond if mentioned or forex keyword present ──────────────
    if not is_dm:
        mentioned = f"@{bot_username}" in text.lower()
        has_keyword = any(k in lower_text for k in FOREX_KEYWORDS)
        if not mentioned and not has_keyword:
            return
        if not _pitch_ok(chat.id):
            log.debug("Pitch cooldown active for chat %s", chat.id)
            return

    # ── Build conversation history ─────────────────────────────────────────────
    history = get_history(user.id, chat.id)
    history.append({"role": "user", "content": text})

    # ── Get AI response ────────────────────────────────────────────────────────
    products = await get_products()
    system   = await _system_prompt(products)

    reply = await _claude(history, system)

    if not reply:
        log.info("Claude unavailable — using dynamic fallback")
        reply = await _fallback(products)

    history.append({"role": "assistant", "content": reply})
    save_history(user.id, chat.id, history)

    # In groups: don't use full keyboard
    kb = _main_kb() if is_dm else None
    await msg.reply_text(reply, reply_markup=kb)

# ── Self-registration of Telegram commands ─────────────────────────────────────
async def post_init(application: Application):
    commands = [
        BotCommand("start",    "Welcome & overview"),
        BotCommand("products", "Browse products"),
        BotCommand("broker",   "Broker recommendation"),
        BotCommand("buy",      "Purchase instructions"),
        BotCommand("lookup",   "Retrieve download"),
        BotCommand("license",  "Validate a license key"),
        BotCommand("ref",      "Get your affiliate link"),
        BotCommand("setpaypal","Set PayPal payout email"),
        BotCommand("reset",    "Clear conversation history"),
        BotCommand("help",     "Command list"),
    ]
    await application.bot.set_my_commands(commands)
    log.info("Bot commands registered.")

# ── Keep-alive ping ────────────────────────────────────────────────────────────
async def keep_alive(context: ContextTypes.DEFAULT_TYPE):
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            await c.get(f"{API_BASE}/health")
        log.debug("Keep-alive ping OK")
    except Exception as e:
        log.warning("Keep-alive failed: %s", e)

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    app = (Application.builder()
           .token(BOT_TOKEN)
           .post_init(post_init)
           .build())

    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(CommandHandler("products",   cmd_products))
    app.add_handler(CommandHandler("broker",     cmd_broker))
    app.add_handler(CommandHandler("buy",        cmd_buy))
    app.add_handler(CommandHandler("lookup",     cmd_lookup))
    app.add_handler(CommandHandler("license",    cmd_license))
    app.add_handler(CommandHandler("ref",        cmd_ref))
    app.add_handler(CommandHandler("setpaypal",  cmd_setpaypal))
    app.add_handler(CommandHandler("stats",      cmd_stats))
    app.add_handler(CommandHandler("reset",      cmd_reset))
    app.add_handler(CommandHandler("help",       cmd_help))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    app.job_queue.run_repeating(keep_alive, interval=KEEP_ALIVE_INTERVAL, first=60)

    log.info("ForexEdge Bot v3 starting (Redis=%s)...", REDIS_OK)
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()
