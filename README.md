# ForexEdge SaaS — v3.0

Complete forex EA / indicator e-commerce platform.
Three-service architecture: Flask API · Customer Service Bot · Marketing Userbot.

---

## What's New in v3

| Area | v2 | v3 |
|---|---|---|
| CSRF | Generated but not checked | ✅ Enforced on every POST |
| Rate limiting | In-process (breaks with multiple workers) | ✅ Redis-backed sliding window |
| Admin password | Plaintext in `ADMIN_PASS` env | ✅ bcrypt hash in `ADMIN_PASS_HASH` |
| API keys | Single shared `API_SECRET` | ✅ Per-service `BOT_API_KEY` / `USERBOT_API_KEY` |
| PayPal IPN | Amount check only | ✅ receiver_email + currency + amount |
| Error responses | Raw exceptions exposed | ✅ Sanitised messages, internal errors logged |
| DB migrations | Manual `init_db.py` | ✅ Versioned migration system |
| Tests | None | ✅ 40+ pytest tests |
| Conversation history | In-process memory | ✅ Redis-backed (survives restarts) |
| Userbot campaigns | Some enabled by default | ✅ All opt-in, only `leave_stale` auto-runs |
| Referral payouts | Manual tracking | ✅ Auto audit email on last day of month |
| Admin UI | Products / purchases / licenses | ✅ Coupons + Referrals + Payouts tabs |

---

## Quick Start

### 1. Hash your admin password
```bash
python hash_password.py
# → copies ADMIN_PASS_HASH=pbkdf2:... to put in your env
```

### 2. Set environment variables
```bash
cp .env.example .env
# Fill in all values — especially:
#   ADMIN_PASS_HASH, DATABASE_URL, REDIS_URL,
#   PAYPAL_RECEIVER_EMAIL, SMTP_USER, SMTP_PASS
```

### 3. Run migrations
```bash
python migrations.py
```

### 4. Start (development)
```bash
flask --app app run --debug
```

### 5. Run tests
```bash
pytest tests/ -v
```

---

## Deployment (Render)

```bash
# Connect your GitHub repo, then:
render deploy --yaml render.yaml
```

After deploying:
1. Go to Render → forex-saas-api → Environment
2. Set `ADMIN_PASS_HASH` (from `python hash_password.py`)
3. Set `PAYPAL_RECEIVER_EMAIL` (your actual PayPal email)
4. Set `SMTP_USER` + `SMTP_PASS` (Gmail App Password)

### First userbot run (one-time phone auth)
```bash
# SSH into Render forex-saas-userbot service
python userbot.py
# Enter your phone verification code when prompted
# Session saved to /data/userbot_session — subsequent runs are automatic
```

---

## Monthly Referral Audit

**Automatic:** Configure a cron job to POST to `/cron/monthly-audit` on the last day of each month:
```bash
# Render cron job (or any scheduler):
curl -X POST https://yourstore.com/cron/monthly-audit \
     -H "X-Cron-Key: your-CRON_KEY-from-env"
```

**Manual:** Admin Panel → Dashboard → "Run Audit Now"

**Flow:**
1. Last day of month: audit runs → email sent to `morrynet@gmail.com`
2. Email lists all affiliates, their sales count, and PayPal address
3. You pay each affiliate via PayPal (Friends & Family) by 5th of month
4. Admin Panel → Payouts → click "Mark Paid" for each one

---

## Security Architecture

```
Browser ──→ Flask API (4 gunicorn workers)
                │
                ├── Redis  ← rate limiting (shared across all workers)
                │          ← bot conversation history
                │
                ├── PostgreSQL ← all business data
                │
                └── SMTP ← monthly audit emails

Telegram ──→ Customer Service Bot (python-telegram-bot)
                │
                └── BOT_API_KEY → Flask /api/v1/*

Telegram ──→ Marketing Userbot (Telethon)
                │
                └── USERBOT_API_KEY → Flask /api/v1/*
```

### Security checklist (all ✅)
- [x] CSRF token on every state-changing POST
- [x] Admin password: bcrypt hash
- [x] Per-service API keys (bot ≠ userbot ≠ admin)
- [x] PayPal IPN: VERIFIED + receiver_email + currency + amount ≥ $1
- [x] Rate limiting via Redis (survives multiple gunicorn workers)
- [x] Constant-time comparisons for all secrets (`hmac.compare_digest`)
- [x] 64-character hex download tokens (brute-force resistant)
- [x] Token expiry enforced on download
- [x] No raw exceptions exposed to clients
- [x] Audit log for all admin actions, IPN events, downloads
- [x] Session cookies: `httponly=True`, `samesite=Lax`, `secure=True` in prod
- [x] Input length caps on all user-supplied fields

---

## Userbot — Anti-Ban Strategy

Campaigns start **OFF**. Enable them in `campaigns.json` following this schedule:

| Week | Enable | Notes |
|---|---|---|
| 1 | `join_forex_groups` | Join 3-5 groups, no posting yet |
| 2 | `daily_channel_posts`, `engagement_poll` | Build your own channel |
| 3 | `broadcast_pitch` (max_groups=3) | Post in joined groups |
| 4+ | `follow_up_warm_leads`, `dm_campaign` (limit=10) | DM hot leads only |
| 6+ | Increase `DM_PER_DAY_MAX` to 20, then 30 | Gradual scale-up |

**High-value target checklist (automated):**
- ✅ Active in forex/EA discussion (not lurking)
- ✅ Mentioned MT4/MT5/TradingView/FTMO/prop firm
- ✅ Asking questions (warm signals: "looking for", "any good ea", "price?")
- ✅ NOT previously contacted or ignored
- ✅ Real account (has username or name)

**Track what works:**
- `sale_sources` table records which groups convert
- Review weekly: `SELECT * FROM sale_sources ORDER BY conversion_rate DESC`
- Double down on converting groups, drop zero-convert ones

---

## File Structure

```
forexedge/
├── app.py              ← Flask API (all business logic)
├── bot.py              ← Customer service Telegram bot
├── userbot.py          ← Marketing userbot (Telethon)
├── migrations.py       ← DB migration system
├── hash_password.py    ← Admin password hashing utility
├── campaigns.json      ← Userbot campaign config (all opt-in)
├── render.yaml         ← Render deployment blueprint
├── requirements.txt
├── .env.example        ← All env vars documented
├── templates/          ← Jinja2 HTML templates
│   ├── store.html
│   ├── admin.html      ← Full SPA: products/purchases/licenses/coupons/referrals/payouts/audit
│   ├── download.html
│   ├── lookup.html
│   ├── success.html
│   ├── login.html
│   └── error.html
└── tests/
    └── test_app.py     ← 40+ pytest tests
```
