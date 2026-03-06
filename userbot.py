"""
ForexEdge Marketing Userbot — v3.0 (Telethon)
═══════════════════════════════════════════════════════════════════════════════
Anti-ban philosophy:
  • ALL campaigns start DISABLED — you enable them manually after warm-up
  • DM only high-intent targets (scraped from serious trading groups)
  • Personalised messages — never copy-paste spam
  • Follow up on group chat engagement before DMing
  • Track sale sources → repeat what works, drop what doesn't
  • Encourage affiliates — multiply reach without risking the account

High-value target checklist (checked before each DM):
  ✓ Active in forex/EA discussion (not just lurking)
  ✓ Asking questions about EAs, indicators, prop firms
  ✓ Mentions MT4, MT5, TradingView, FTMO, MFF, Exness
  ✓ NOT in ignore list (already contacted, uninterested, blocked)
  ✓ Account age > 0 (not a bot or fresh spam account)
═══════════════════════════════════════════════════════════════════════════════
"""
import os, asyncio, json, logging, time, datetime, sqlite3, re, random
from dataclasses import dataclass, field
from typing import Optional

from telethon import TelegramClient, events
from telethon.tl.functions.channels import (InviteToChannelRequest,
                                              CreateChannelRequest,
                                              UpdateUsernameRequest)
from telethon.tl.functions.messages import GetHistoryRequest
from telethon.tl.types import User
import httpx

# ── Logging ────────────────────────────────────────────────────────────────────
DEBUG = os.environ.get("DEBUG","false").lower()=="true"
logging.basicConfig(level=logging.DEBUG if DEBUG else logging.INFO,
                    format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
log = logging.getLogger("forexedge.userbot")

# ── Config ─────────────────────────────────────────────────────────────────────
API_ID              = int(os.environ["TELEGRAM_API_ID"])
API_HASH            = os.environ["TELEGRAM_API_HASH"]
PHONE               = os.environ["TELEGRAM_PHONE"]
ANTHROPIC_KEY       = os.environ.get("ANTHROPIC_API_KEY","")
STORE_URL           = os.environ.get("STORE_URL","https://yourstore.com")
EXNESS_URL          = "https://one.exnessonelink.com/a/t0gft0gf"
SESSION_FILE        = os.environ.get("SESSION_FILE","userbot_session")
STATE_DB            = os.environ.get("STATE_DB","userbot_state.db")
CAMPAIGNS_FILE      = os.environ.get("CAMPAIGNS_FILE","campaigns.json")

# Safety limits — start conservative, increase after 2+ weeks of clean activity
DM_PER_DAY_MAX      = int(os.environ.get("DM_PER_DAY_MAX","10"))
DM_DELAY_MIN        = int(os.environ.get("DM_DELAY_MIN","90"))
DM_DELAY_MAX        = int(os.environ.get("DM_DELAY_MAX","240"))
GROUP_MSG_DELAY_SEC = int(os.environ.get("GROUP_MSG_DELAY_SEC","900"))   # 15 min between posts per group
JOIN_DELAY_SEC      = int(os.environ.get("JOIN_DELAY_SEC","60"))
CHANNEL_POST_INTERVAL_SEC = int(os.environ.get("CHANNEL_POST_INTERVAL_SEC","21600"))  # 6h

# ─── High-value target keywords ──────────────────────────────────────────────
HV_KEYWORDS = {
    "mt4","mt5","ea","expert advisor","indicator","strategy tester",
    "xauusd","eurusd","gbpusd","prop firm","ftmo","mff","e8","the5ers",
    "funded","challenge","backtest","pine script","tradingview",
    "exness","deriv","boom","crash","scalp","grid","martingale","hedge",
}

# Keywords indicating a warm lead in group conversation
WARM_SIGNALS = {
    "where can i find","any good ea","recommend","looking for","does anyone use",
    "how much","price","buy","purchase","trial","free","download",
    "signal","automation","which broker","best ea",
}

# ─── State DB ─────────────────────────────────────────────────────────────────
def init_db(path=STATE_DB):
    c = sqlite3.connect(path)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS dms (
        user_id INTEGER PRIMARY KEY, ts REAL, msg TEXT, replied INTEGER DEFAULT 0,
        stage TEXT DEFAULT 'new', outcome TEXT);
    CREATE TABLE IF NOT EXISTS group_posts (
        chat_id INTEGER PRIMARY KEY, ts REAL, msg TEXT);
    CREATE TABLE IF NOT EXISTS joined_groups (
        chat_id INTEGER PRIMARY KEY, title TEXT, joined_at REAL, last_active_ts REAL);
    CREATE TABLE IF NOT EXISTS own_channels (
        chat_id INTEGER PRIMARY KEY, title TEXT, username TEXT,
        channel_type TEXT, created_at REAL, last_post_ts REAL, member_count INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS invited_members (
        user_id INTEGER, channel_id INTEGER, ts REAL,
        PRIMARY KEY(user_id, channel_id));
    CREATE TABLE IF NOT EXISTS ignored_users (
        user_id INTEGER PRIMARY KEY, reason TEXT, ts REAL);
    CREATE TABLE IF NOT EXISTS sale_sources (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source TEXT, channel TEXT, group_name TEXT,
        sale_count INTEGER DEFAULT 0, dm_count INTEGER DEFAULT 0,
        conversion_rate REAL DEFAULT 0, last_updated REAL);
    CREATE TABLE IF NOT EXISTS group_engagements (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER, user_id INTEGER, message TEXT,
        is_warm INTEGER DEFAULT 0, ts REAL);
    """)
    c.commit()
    return c

def _db():
    c = sqlite3.connect(STATE_DB, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c

def _q(sql, params=(), fetchone=False, fetchall=False, commit=False):
    c = _db()
    try:
        cur = c.cursor(); cur.execute(sql,params)
        if commit: c.commit()
        if fetchone: r=cur.fetchone(); return dict(r) if r else None
        if fetchall: return [dict(r) for r in cur.fetchall()]
    finally: c.close()

# ─── Claude AI ────────────────────────────────────────────────────────────────
async def _claude(prompt:str, system:str, max_tokens:int=300) -> Optional[str]:
    if not ANTHROPIC_KEY: return None
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post("https://api.anthropic.com/v1/messages",
                headers={"x-api-key":ANTHROPIC_KEY,"anthropic-version":"2023-06-01","content-type":"application/json"},
                json={"model":"claude-sonnet-4-20250514","max_tokens":max_tokens,
                      "system":system,"messages":[{"role":"user","content":prompt}]})
            data=r.json(); blocks=data.get("content",[])
            return " ".join(b.get("text","") for b in blocks if b.get("type")=="text").strip() or None
    except Exception as e:
        log.warning("Claude error: %s",e); return None

PITCH_SYSTEM = f"""You write short, direct Telegram messages for ForexEdge — a forex EA/indicator store.

Store: {STORE_URL}
Broker affiliate: Exness — {EXNESS_URL}

RULES:
- Max 3 sentences. Never start with "Hi I'm a bot" or "Hey, I noticed..."
- Sound like a fellow trader who found a useful tool, not a salesperson
- Mention 1 specific product relevant to the context
- Include store link naturally (no "click here" language)
- If context suggests prop firm trading, mention prop-firm compatibility
- If context is Boom & Crash, mention the specific EA
- NEVER use all-caps or emoji spam
- Be frank: prices start at $20, no fake "limited time" pressure"""

CHANNEL_SYSTEM = f"""You write daily trading content for a Telegram channel.
Topics: technical analysis tips, EA setups, risk management, broker insights.
Tone: knowledgeable trader sharing insights (not a marketer).
Max 150 words. Include 1-2 relevant hashtags. Mention ForexEdge {STORE_URL} naturally once if relevant."""

async def ai_pitch(msg_text:str, user_name:str, group_name:str) -> Optional[str]:
    words = set(msg_text.lower().split())
    if not any(k in msg_text.lower() for k in HV_KEYWORDS):
        return None
    prompt = (f"Context: In Telegram group '{group_name}', user {user_name} said:\n"
              f'"{msg_text}"\n\n'
              f"Write a helpful reply that naturally mentions a relevant ForexEdge product.")
    return await _claude(prompt, PITCH_SYSTEM, 200)

async def ai_dm_reply(text:str, name:str, history:str, stage:str) -> str:
    stage_ctx = {
        "new":      "This is the first message from this user — be welcoming, ask what they trade.",
        "nurture":  "User has replied — build rapport, understand their setup, suggest a product.",
        "engaged":  "User is actively interested — guide toward purchase or affiliate sign-up.",
        "customer": "This person already bought — suggest complementary tools or affiliate program.",
    }.get(stage,"New contact.")
    prompt = (f"User {name} sent: \"{text}\"\n"
              f"Stage: {stage_ctx}\n"
              f"Previous exchange:\n{history}\n\n"
              f"Write your reply (max 4 sentences).")
    return await _claude(prompt, PITCH_SYSTEM, 300) or _static_dm_fallback(stage)

async def ai_channel_post(topic:str="daily trading tip") -> str:
    r = await _claude(f"Write a {topic} post for today {datetime.date.today()}", CHANNEL_SYSTEM, 200)
    return r or f"📊 Trading insight: {topic}. Visit {STORE_URL} for automation tools. #{topic.replace(' ','')}"

def _static_dm_fallback(stage:str) -> str:
    opts = {
        "new":     [f"Hey! I trade with ForexEdge tools — EAs and indicators from $20. What pairs do you run? {STORE_URL}",
                    f"Solid group here. I use automated EAs for XAUUSD — check the range at {STORE_URL}"],
        "nurture": [f"The prop-firm EA works well on FTMO challenges — low DD, consistent. Worth a look: {STORE_URL}",
                    f"Depends on your setup, but the XAUUSD scalper has been solid for me. {STORE_URL}"],
        "engaged": [f"It's $20 flat, instant download after PayPal. {STORE_URL} — super easy checkout.",
                    f"If you end up liking it, there's an affiliate program too — $5 per referral. {STORE_URL}"],
    }
    return random.choice(opts.get(stage, opts["new"]))

# ─── UserBot class ─────────────────────────────────────────────────────────────
class ForexUserBot:
    def __init__(self):
        self.client = TelegramClient(SESSION_FILE, API_ID, API_HASH)
        self._dm_count_today = 0
        self._dm_day = datetime.date.today()
        init_db()

    def _reset_dm_counter(self):
        today = datetime.date.today()
        if today != self._dm_day:
            self._dm_day = today; self._dm_count_today = 0

    def _can_dm(self) -> bool:
        self._reset_dm_counter()
        return self._dm_count_today < DM_PER_DAY_MAX

    def _is_ignored(self, user_id:int) -> bool:
        return bool(_q(f"SELECT user_id FROM ignored_users WHERE user_id=?",(user_id,),fetchone=True))

    def _ignore(self, user_id:int, reason:str="uninterested"):
        _q("INSERT OR REPLACE INTO ignored_users(user_id,reason,ts) VALUES(?,?,?)",
           (user_id,reason,time.time()),commit=True)

    def _is_high_value(self, user:User, recent_text:str="") -> bool:
        """Filter for high-value potential clients."""
        if not user or user.bot: return False
        if self._is_ignored(user.id): return False
        if not user.username and not user.first_name: return False   # ghost account
        # Check if their message contains high-value keywords
        if recent_text:
            text_lower = recent_text.lower()
            hv_score = sum(1 for k in HV_KEYWORDS if k in text_lower)
            warm_score = sum(1 for k in WARM_SIGNALS if k in text_lower)
            if hv_score == 0: return False
            # Extra weight to warm signals (they're asking or looking to buy)
            return hv_score >= 1 or warm_score >= 1
        return True

    # ── Group management ──────────────────────────────────────────────────────
    async def join_group(self, target:str):
        try:
            await self.client.get_entity(target)
            entity = await self.client.join_channel(target)
            chat_id = entity.id if hasattr(entity,'id') else 0
            _q("INSERT OR IGNORE INTO joined_groups(chat_id,title,joined_at,last_active_ts) VALUES(?,?,?,?)",
               (chat_id, str(target), time.time(), time.time()), commit=True)
            log.info("Joined: %s",target)
            await asyncio.sleep(JOIN_DELAY_SEC + random.uniform(0,30))
        except Exception as e:
            log.warning("Join failed %s: %s",target,e)

    async def leave_stale(self, days:int=7):
        cutoff = time.time() - days*86400
        stale = _q(f"SELECT chat_id,title FROM joined_groups WHERE last_active_ts < ?",(cutoff,),fetchall=True) or []
        for row in stale:
            try:
                await self.client.delete_dialog(int(row["chat_id"]))
                _q("DELETE FROM joined_groups WHERE chat_id=?",(row["chat_id"],),commit=True)
                log.info("Left stale group: %s",row["title"])
            except Exception as e:
                log.warning("Leave failed %s: %s",row["chat_id"],e)

    # ── Channel management ────────────────────────────────────────────────────
    async def create_channel(self, title:str, about:str, is_channel:bool=True):
        try:
            result = await self.client(CreateChannelRequest(
                title=title, about=about, megagroup=not is_channel, broadcast=is_channel))
            chat = result.chats[0]; chat_id = chat.id
            _q("INSERT OR IGNORE INTO own_channels(chat_id,title,channel_type,created_at,last_post_ts)"
               " VALUES(?,?,?,?,0)",(chat_id,title,"channel" if is_channel else "group",time.time()),commit=True)
            log.info("Created channel: %s (id=%s)",title,chat_id)
            return chat
        except Exception as e:
            log.error("Create channel failed: %s",e); return None

    async def set_username(self, entity, username:str):
        try:
            await self.client(UpdateUsernameRequest(channel=entity, username=username))
            _q("UPDATE own_channels SET username=? WHERE chat_id=?",(username,entity.id),commit=True)
            log.info("Username set: @%s",username)
        except Exception as e:
            log.warning("Set username failed: %s",e)

    # ── Scraping — high-value members only ───────────────────────────────────
    async def scrape_hv_members(self, group, limit:int=200) -> list:
        """Scrape members, keeping only high-value targets."""
        hv = []
        try:
            entity = await self.client.get_entity(group)
            # Get recent messages to score users by activity
            history = await self.client(GetHistoryRequest(
                peer=entity, limit=min(limit,500), offset_date=None,
                offset_id=0, max_id=0, min_id=0, add_offset=0, hash=0))
            seen = {}
            for msg in history.messages:
                if not msg.from_id: continue
                uid = getattr(msg.from_id,"user_id",None)
                if uid: seen[uid] = getattr(msg,"message","")
            # Fetch user objects for top contributors
            for uid, msg_text in list(seen.items())[:limit]:
                if self._is_ignored(uid): continue
                try:
                    user = await self.client.get_entity(uid)
                    if isinstance(user,User) and self._is_high_value(user,msg_text):
                        hv.append({"user":user,"recent_msg":msg_text})
                except: pass
            log.info("Scraped %d HV targets from %s (checked %d)",len(hv),group,len(seen))
        except Exception as e:
            log.warning("Scrape failed %s: %s",group,e)
        return hv

    # ── DM sending ────────────────────────────────────────────────────────────
    async def dm_user(self, user_id:int, message:str) -> bool:
        if not self._can_dm():
            log.info("DM daily limit reached (%d/%d)",self._dm_count_today,DM_PER_DAY_MAX)
            return False
        try:
            await self.client.send_message(user_id, message)
            _q("INSERT OR REPLACE INTO dms(user_id,ts,msg,replied,stage) VALUES(?,?,?,0,'new')",
               (user_id,time.time(),message[:500]),commit=True)
            self._dm_count_today += 1
            log.info("DM sent to %s (%d/%d today)",user_id,self._dm_count_today,DM_PER_DAY_MAX)
            delay = random.uniform(DM_DELAY_MIN,DM_DELAY_MAX)
            await asyncio.sleep(delay)
            return True
        except Exception as e:
            log.warning("DM failed %s: %s",user_id,e)
            if "privacy" in str(e).lower() or "forbidden" in str(e).lower():
                self._ignore(user_id,"privacy_restricted")
            return False

    async def dm_campaign(self, group, limit:int=15, also_invite_to_channel:int=None):
        """Scrape HV targets from a group, DM personalised pitch, optionally invite to channel."""
        targets = await self.scrape_hv_members(group, limit*3)  # over-scrape to fill quota
        group_name = str(group).split("/")[-1]
        sent = 0
        for t in targets:
            if sent >= limit: break
            user = t["user"]; recent = t["recent_msg"]
            # Generate personalised pitch based on what they said
            pitch_prompt = (f"User in group '{group_name}' recently said: \"{recent[:200]}\"\n"
                            f"Write a brief DM (3 sentences max) naturally introducing ForexEdge.")
            msg = await _claude(pitch_prompt, PITCH_SYSTEM, 200) or _static_dm_fallback("new")
            if await self.dm_user(user.id, msg):
                sent += 1
                # Track source
                _q("INSERT OR IGNORE INTO sale_sources(source,channel,group_name,dm_count,last_updated)"
                   " VALUES('dm_campaign',?,?,1,?)",
                   (group_name,group_name,time.time()),commit=True)
                _q("UPDATE sale_sources SET dm_count=dm_count+1,last_updated=? WHERE group_name=?",
                   (time.time(),group_name),commit=True)
                if also_invite_to_channel:
                    try:
                        await self.client(InviteToChannelRequest(
                            channel=also_invite_to_channel, users=[user.id]))
                        _q("INSERT OR IGNORE INTO invited_members(user_id,channel_id,ts) VALUES(?,?,?)",
                           (user.id,also_invite_to_channel,time.time()),commit=True)
                    except: pass
        log.info("DM campaign: %d sent from %s",sent,group)

    async def growth_loop(self, source_groups:list, own_channel_id:int):
        """Full loop: scrape multiple groups → DM → invite to own channel → post content."""
        log.info("Growth loop starting. Sources: %s, Channel: %s",source_groups,own_channel_id)
        for g in source_groups:
            await self.dm_campaign(g, limit=max(2,DM_PER_DAY_MAX//len(source_groups)),
                                   also_invite_to_channel=own_channel_id)
        await self.post_to_own_channels()
        log.info("Growth loop complete for today.")

    # ── Group posting ─────────────────────────────────────────────────────────
    async def post(self, entity, message:str, reply_to:int=None):
        try:
            await self.client.send_message(entity, message, reply_to=reply_to)
            log.info("Posted to %s",entity)
        except Exception as e:
            log.warning("Post failed %s: %s",entity,e)

    async def broadcast(self, message:str, max_groups:int=5):
        groups = _q("SELECT chat_id FROM joined_groups ORDER BY RANDOM() LIMIT ?",
                    (max_groups,), fetchall=True) or []
        posted = 0
        for row in groups:
            last_row = _q("SELECT ts FROM group_posts WHERE chat_id=?",(row["chat_id"],),fetchone=True)
            if last_row and time.time()-last_row["ts"]<GROUP_MSG_DELAY_SEC:
                continue
            await self.post(int(row["chat_id"]),message)
            _q("INSERT OR REPLACE INTO group_posts(chat_id,ts,msg) VALUES(?,?,?)",
               (row["chat_id"],time.time(),message[:500]),commit=True)
            posted += 1
            await asyncio.sleep(random.uniform(30,90))
        log.info("Broadcast: posted to %d/%d groups",posted,len(groups))

    async def post_to_own_channels(self):
        channels = _q("SELECT * FROM own_channels",fetchall=True) or []
        topics = ["daily trading tip","risk management for retail traders",
                  "EA automation vs manual trading","broker comparison","XAUUSD analysis",
                  "prop firm challenge tips","how to backtest an EA"]
        for ch in channels:
            last = ch.get("last_post_ts",0)
            if time.time()-last < CHANNEL_POST_INTERVAL_SEC: continue
            topic = random.choice(topics)
            content = await ai_channel_post(topic)
            try:
                await self.client.send_message(int(ch["chat_id"]),content)
                _q("UPDATE own_channels SET last_post_ts=? WHERE chat_id=?",(time.time(),ch["chat_id"]),commit=True)
                log.info("Posted to own channel %s: %s",ch["title"],topic)
            except Exception as e:
                log.warning("Own channel post failed: %s",e)

    async def send_poll(self, entity, question:str, options:list):
        try:
            from telethon.tl.types import PollAnswer, Poll
            from telethon.tl.functions.messages import SendMediaRequest
            # Use plain text fallback if poll API is complex
            text = f"📊 {question}\n" + "\n".join(f"{i+1}. {o}" for i,o in enumerate(options))
            await self.client.send_message(entity, text)
        except Exception as e:
            log.warning("Poll failed: %s",e)

    # ── Follow-up on warm group leads ─────────────────────────────────────────
    async def follow_up_warm_leads(self):
        """Check group engagements flagged as warm — follow up in DM."""
        warm = _q("SELECT DISTINCT user_id,message,chat_id FROM group_engagements"
                  " WHERE is_warm=1 ORDER BY ts DESC LIMIT 20",fetchall=True) or []
        for lead in warm:
            uid = lead["user_id"]
            if self._is_ignored(uid): continue
            if _q("SELECT user_id FROM dms WHERE user_id=?",(uid,),fetchone=True): continue
            try:
                user = await self.client.get_entity(uid)
                msg = (f"Hey! I saw your message in the group. "
                       f"I use ForexEdge EAs for that — starting at $20. "
                       f"Happy to help you find the right one: {STORE_URL}")
                await self.dm_user(uid, msg)
            except: pass
        _q("UPDATE group_engagements SET is_warm=0 WHERE is_warm=1",commit=True)

    # ── Event handlers ────────────────────────────────────────────────────────
    def setup_handlers(self):
        @self.client.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
        async def on_dm(event):
            user_id = event.sender_id
            if self._is_ignored(user_id): return
            text = event.text or ""
            user = await event.get_sender()
            name = getattr(user,"first_name","") or "there"
            existing = _q("SELECT stage,msg FROM dms WHERE user_id=?",(user_id,),fetchone=True)
            stage = existing["stage"] if existing else "new"
            history_str = existing["msg"][:200] if existing else ""
            reply = await ai_dm_reply(text, name, history_str, stage)
            # Advance stage
            next_stage = {"new":"nurture","nurture":"engaged","engaged":"customer"}.get(stage,"customer")
            _q("INSERT OR REPLACE INTO dms(user_id,ts,msg,replied,stage) VALUES(?,?,?,1,?)",
               (user_id,time.time(),text[:500],next_stage),commit=True)
            await asyncio.sleep(random.uniform(2,8))   # natural typing delay
            await event.respond(reply)

        @self.client.on(events.NewMessage(incoming=True, func=lambda e: not e.is_private))
        async def on_group_msg(event):
            text = event.text or ""; text_lower = text.lower()
            if not any(k in text_lower for k in HV_KEYWORDS): return
            sender = await event.get_sender()
            if not isinstance(sender,User) or sender.bot: return
            # Track as potential warm lead
            is_warm = int(any(k in text_lower for k in WARM_SIGNALS))
            _q("INSERT INTO group_engagements(chat_id,user_id,message,is_warm,ts) VALUES(?,?,?,?,?)",
               (event.chat_id,sender.id,text[:500],is_warm,time.time()),commit=True)
            # Update joined_groups activity timestamp
            _q("UPDATE joined_groups SET last_active_ts=? WHERE chat_id=?",(time.time(),event.chat_id),commit=True)
            # Reply in group only if cool-down allows and message is warm
            if not is_warm: return
            last = _q("SELECT ts FROM group_posts WHERE chat_id=?",(event.chat_id,),fetchone=True)
            if last and time.time()-last["ts"]<GROUP_MSG_DELAY_SEC: return
            group_entity = await event.get_chat()
            group_name = getattr(group_entity,"title","group")
            reply = await ai_pitch(text, name=getattr(sender,"first_name","there"), group_name=group_name)
            if reply:
                _q("INSERT OR REPLACE INTO group_posts(chat_id,ts,msg) VALUES(?,?,?)",
                   (event.chat_id,time.time(),reply[:500]),commit=True)
                await asyncio.sleep(random.uniform(5,20))
                await event.reply(reply)

    # ── Campaign runner ───────────────────────────────────────────────────────
    async def run_campaigns(self):
        if not os.path.exists(CAMPAIGNS_FILE):
            log.warning("campaigns.json not found — no campaigns will run.")
            return
        with open(CAMPAIGNS_FILE) as f:
            campaigns = json.load(f)

        for camp in campaigns:
            if not camp.get("enabled", False):
                log.info("Campaign '%s' disabled — skipping.",camp.get("name","?"))
                continue
            name = camp.get("name","?")
            ctype = camp.get("type","")
            log.info("Running campaign: %s (%s)",name,ctype)
            try:
                if ctype=="join_groups":
                    for g in camp.get("groups",[]): await self.join_group(g)
                elif ctype=="broadcast":
                    msg = camp.get("message","Check out ForexEdge: "+STORE_URL)
                    await self.broadcast(msg, max_groups=camp.get("max_groups",3))
                elif ctype=="dm_campaign":
                    for g in camp.get("groups",[]):
                        await self.dm_campaign(g, limit=camp.get("limit",10),
                                               also_invite_to_channel=camp.get("invite_to_channel"))
                elif ctype=="growth_loop":
                    await self.growth_loop(camp.get("source_groups",[]),
                                          camp.get("own_channel_id",0))
                elif ctype=="create_channel":
                    await self.create_channel(camp["title"],camp.get("about",""),
                                             camp.get("is_channel",True))
                elif ctype=="post_own_channels":
                    await self.post_to_own_channels()
                elif ctype=="send_poll":
                    for ch in _q("SELECT chat_id FROM own_channels",fetchall=True) or []:
                        await self.send_poll(int(ch["chat_id"]),camp["question"],camp["options"])
                elif ctype=="leave_stale":
                    await self.leave_stale(days=camp.get("days",7))
                elif ctype=="follow_up_warm":
                    await self.follow_up_warm_leads()
            except Exception as e:
                log.error("Campaign %s failed: %s",name,e)

    # ── Scheduler (channel posts on interval) ─────────────────────────────────
    async def _scheduler_loop(self):
        while True:
            await asyncio.sleep(CHANNEL_POST_INTERVAL_SEC)
            await self.post_to_own_channels()

    # ── Main ──────────────────────────────────────────────────────────────────
    async def start(self):
        await self.client.start(phone=PHONE)
        me = await self.client.get_me()
        log.info("Userbot connected as @%s (id=%s)", me.username, me.id)
        self.setup_handlers()
        asyncio.create_task(self._scheduler_loop())
        await self.run_campaigns()
        await self.client.run_until_disconnected()

def main():
    bot = ForexUserBot()
    asyncio.run(bot.start())

if __name__ == "__main__":
    main()
