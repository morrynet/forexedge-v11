# 📈 ForexEdge

Professional Forex Tools store — MT4/MT5 EAs, Pine Script strategies, and courses — sold at a flat $20 via PayPal with instant download delivery.

Built with Flask, deployed on Render, with a Telegram bot and optional userbot for growth automation.

---

## Project Structure

```
forexedge/
├── app.py               # Flask web app (store, payments, admin, API)
├── bot.py               # Telegram bot (customer support, /ref, AI chat)
├── userbot.py           # Telethon userbot (growth campaigns)
├── migrations.py        # DB migration runner (SQLite + PostgreSQL)
├── campaigns.json       # Userbot campaign config
├── gen_keys.py          # Secret key & password hash generator
├── requirements.txt
├── render.yaml          # Render deployment config (3 services)
└── templates/
    ├── store.html
    ├── success.html
    ├── download.html
    ├── lookup.html
    ├── login.html
    ├── error.html
    └── admin.html
```

> ⚠️ All HTML files must live inside a `templates/` subfolder for Flask to find them.

---

## Services (render.yaml)

| Service | Type | Description |
|---|---|---|
| `forexedge-api` | Web | Flask store + PayPal IPN + admin dashboard |
| `forexedge-bot` | Worker | Telegram customer bot |
| `forexedge-userbot` | Worker | Telethon growth automation |

---

## Quick Start

### 1. Generate secrets

```bash
python gen_keys.py yourAdminPassword
```

Copy the output values into Render's environment variables.

### 2. Set environment variables on Render

See the full reference below. At minimum you need:

- `SECRET_KEY`
- `ADMIN_PASS_HASH` + `ADMIN_PASS`
- `PAYPAL_EMAIL`
- `STORE_URL` (your Render URL, e.g. `https://forexedge-api.onrender.com`)
- `BOT_API_KEY` (shared between `forexedge-api` and `forexedge-bot`)
- `TELEGRAM_BOT_TOKEN`

### 3. Deploy

Push to GitHub — Render auto-deploys all three services.  
Migrations run automatically on each deploy via the build command:

```
pip install -r requirements.txt && python migrations.py
```

---

## Environment Variables

### forexedge-api (Web)

| Variable | Required | Description |
|---|---|---|
| `SECRET_KEY` | ✅ | Flask session secret (generate with `gen_keys.py`) |
| `ADMIN_USER` | ✅ | Admin username (default: `admin`) |
| `ADMIN_PASS_HASH` | ✅ | Werkzeug password hash (from `gen_keys.py`) |
| `ADMIN_PASS` | ✅ | Plaintext password (used by bot API auth) |
| `PAYPAL_EMAIL` | ✅ | Your PayPal business email |
| `STORE_URL` | ✅ | Full public URL of this service |
| `BOT_API_KEY` | ✅ | Shared secret for bot ↔ API calls |
| `USERBOT_API_KEY` | ✅ | Shared secret for userbot ↔ API calls |
| `BOT_USERNAME` | ✅ | Telegram bot username (without @) |
| `ANTHROPIC_API_KEY` | optional | Enables AI responses in the bot |
| `SMTP_HOST` | optional | SMTP server for email receipts |
| `SMTP_PORT` | optional | Default: `587` |
| `SMTP_USER` | optional | SMTP login |
| `SMTP_PASS` | optional | SMTP password |
| `AUDIT_EMAIL` | optional | Receives audit/alert emails |
| `PAYPAL_SANDBOX` | optional | Set `true` for testing |
| `PRICE_USD` | optional | Default product price (default: `20.00`) |
| `MAX_DOWNLOADS` | optional | Download slots per purchase (default: `3`) |
| `DATABASE_URL` | optional | PostgreSQL URL — uses SQLite if not set |

### forexedge-bot (Worker)

| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | From @BotFather |
| `BOT_API_KEY` | ✅ | Must match `forexedge-api` |
| `API_URL` | ✅ | Internal URL of `forexedge-api` |
| `STORE_URL` | ✅ | Public store URL |
| `BOT_USERNAME` | ✅ | Telegram bot username |
| `ADMIN_TELEGRAM_IDS` | optional | Comma-separated admin Telegram user IDs |
| `ANTHROPIC_API_KEY` | optional | Enables AI chat replies |
| `PITCH_COOLDOWN_SEC` | optional | Seconds between sales pitches (default: `300`) |

### forexedge-userbot (Worker)

| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_API_ID` | ✅ | From my.telegram.org |
| `TELEGRAM_API_HASH` | ✅ | From my.telegram.org |
| `TELEGRAM_PHONE` | ✅ | Phone number for the userbot account |
| `USERBOT_API_KEY` | ✅ | Must match `forexedge-api` |
| `API_URL` | ✅ | Internal URL of `forexedge-api` |
| `STORE_URL` | ✅ | Public store URL |
| `TELEGRAM_SESSION_NAME` | optional | Session file path (default: `/tmp/forexedge_userbot`) |
| `DM_DELAY_MIN` | optional | Min seconds between DMs (default: `60`) |
| `DM_DELAY_MAX` | optional | Max seconds between DMs (default: `180`) |
| `DM_PER_DAY_MAX` | optional | Daily DM cap (default: `15`) |
| `GROUP_MSG_DELAY` | optional | Seconds between group posts (default: `900`) |
| `JOIN_DELAY` | optional | Seconds between group joins (default: `45`) |

---

## Database

Supports both **SQLite** (default, zero config) and **PostgreSQL** (set `DATABASE_URL`).

Migrations run automatically on deploy. To run manually:

```bash
python migrations.py
```

---

## Admin Dashboard

Visit `/admin` after deployment. Login with `ADMIN_USER` / `ADMIN_PASS`.

From the dashboard you can:
- Add / edit / delete products and upload files
- View purchases and generate download tokens
- Manage coupons and referral codes
- View audit logs
- Control userbot campaigns

---

## Affiliate Program

Customers can get a referral link via the Telegram bot (`/ref`). They earn **$5 per sale**, paid monthly on the 5th via PayPal.

---

## Common Issues

**`TemplateNotFound: store.html`** — HTML files must be inside a `templates/` folder. If you uploaded files via GitHub's UI directly to the repo root, move them into `templates/`.

**Build exits with status 1** — Check the Render build logs. Usually a missing env var or import error in `app.py` on startup.

**Userbot session** — The first run requires an interactive phone code confirmation. Run locally once to generate the session file, then upload the session string or use a persistent disk on Render.

---

## License

Private / proprietary. Not for redistribution.
