"""
ForexEdge SaaS — Flask Backend  v3.0  (Production-hardened)
Security: CSRF on all POSTs · Redis rate limiting · bcrypt passwords
          Per-service API keys · Full PayPal IPN checks · Referral audits
"""
import os, uuid, datetime, json, logging, urllib.parse, urllib.request
import mimetypes, hmac, secrets, time, smtplib, ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from collections import defaultdict
from functools import wraps
from io import BytesIO

from flask import (Flask, request, jsonify, render_template, session,
                   redirect, url_for, send_file)
from werkzeug.security import generate_password_hash, check_password_hash

# ── Optional Redis ─────────────────────────────────────────────────────────────
try:
    import redis as _redis_lib
    _redis_url = os.environ.get("REDIS_URL", "")
    _rc = _redis_lib.from_url(_redis_url, decode_responses=True, socket_connect_timeout=2) if _redis_url else None
    if _rc: _rc.ping()
    REDIS_OK = bool(_rc)
except Exception:
    _rc = None; REDIS_OK = False

# ── Database ───────────────────────────────────────────────────────────────────
USE_PG = bool(os.environ.get("DATABASE_URL"))
if USE_PG:
    import psycopg2, psycopg2.extras, psycopg2.pool; PH = "%s"
    _pool = None
    def _get_pool():
        global _pool
        if not _pool:
            url = os.environ["DATABASE_URL"].replace("postgres://","postgresql://",1)
            _pool = psycopg2.pool.ThreadedConnectionPool(2,10,url,cursor_factory=psycopg2.extras.RealDictCursor)
        return _pool
    def db(): return _get_pool().getconn()
    def release(c): _get_pool().putconn(c)
else:
    import sqlite3; PH = "?"
    DB_PATH = os.environ.get("DB_PATH","saas.db")
    def db():
        c = sqlite3.connect(DB_PATH,check_same_thread=False,timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL"); c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA busy_timeout=5000"); return c
    def release(c): c.close()

def query(sql, params=(), fetchone=False, fetchall=False, commit=False):
    conn = db()
    try:
        cur = conn.cursor(); cur.execute(sql, params)
        if commit: conn.commit()
        if fetchone:
            r = cur.fetchone(); return dict(r) if r else None
        if fetchall: return [dict(r) for r in cur.fetchall()]
        return None
    except Exception as e:
        log.error("DB: %s | %s", e, sql[:60]); raise
    finally:
        if USE_PG: release(conn)
        else: conn.close()

# ── App ────────────────────────────────────────────────────────────────────────
_here = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, template_folder=os.path.join(_here,"templates"), static_folder=os.path.join(_here,"static"))
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("HTTPS","false").lower()=="true",
    PERMANENT_SESSION_LIFETIME=datetime.timedelta(hours=8),
    MAX_CONTENT_LENGTH=50*1024*1024,
)
logging.basicConfig(level=logging.DEBUG if os.environ.get("DEBUG") else logging.INFO,
                    format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
log = logging.getLogger("forexedge")

# ── Config ─────────────────────────────────────────────────────────────────────
ADMIN_USER        = os.environ.get("ADMIN_USER","admin")
ADMIN_PASS_HASH   = os.environ.get("ADMIN_PASS_HASH","")   # bcrypt hash — preferred
ADMIN_PASS_PLAIN  = os.environ.get("ADMIN_PASS","")        # deprecated fallback
BOT_API_KEY       = os.environ.get("BOT_API_KEY","")
USERBOT_API_KEY   = os.environ.get("USERBOT_API_KEY","")
API_SECRET        = os.environ.get("API_SECRET","")        # legacy compat
PAYPAL_BUTTON_URL = os.environ.get("PAYPAL_BUTTON_URL","https://www.paypal.com/ncp/payment/Q4B6YUQN6X5GS")
PAYPAL_RECEIVER   = os.environ.get("PAYPAL_RECEIVER_EMAIL","")
PAYPAL_IPN_URL    = "https://ipnpb.paypal.com/cgi-bin/webscr"
PAYPAL_SANDBOX_IPN= "https://ipnpb.sandbox.paypal.com/cgi-bin/webscr"
SANDBOX           = os.environ.get("PAYPAL_SANDBOX","false").lower()=="true"
STORE_URL         = os.environ.get("STORE_URL","http://localhost:5000")
EXNESS_URL        = "https://one.exnessonelink.com/a/t0gft0gf"
PRICE_USD         = "20.00"
MAX_DOWNLOADS     = 3
DOWNLOAD_TTL_DAYS = int(os.environ.get("DOWNLOAD_TOKEN_TTL_DAYS","30"))
REFERRAL_FEE_USD  = float(os.environ.get("REFERRAL_COMMISSION_USD","5.00"))
SMTP_HOST         = os.environ.get("SMTP_HOST","smtp.gmail.com")
SMTP_PORT         = int(os.environ.get("SMTP_PORT","587"))
SMTP_USER         = os.environ.get("SMTP_USER","")
SMTP_PASS         = os.environ.get("SMTP_PASS","")
ADMIN_EMAIL       = os.environ.get("ADMIN_EMAIL","morrynet@gmail.com")
CRON_KEY          = os.environ.get("CRON_KEY","")

def now_iso(): return datetime.datetime.utcnow().isoformat()

# ══════════════════════════════════════════════════════════════════════════════
#  RATE LIMITER  — Redis-backed, in-process fallback
# ══════════════════════════════════════════════════════════════════════════════
_local_rl: dict = defaultdict(lambda: {"t":10.0,"ts":time.time()})

def _allow(key:str, max_t:float=10, rate:float=1.0) -> bool:
    if REDIS_OK and _rc:
        rk = f"rl:{key}"; now_ms = int(time.time()*1000)
        win = int(1000/rate); pipe = _rc.pipeline()
        pipe.zremrangebyscore(rk,0,now_ms-win*int(max_t))
        pipe.zcard(rk)
        pipe.zadd(rk,{str(now_ms)+secrets.token_hex(3):now_ms})
        pipe.expire(rk,int(max_t/rate)+5)
        res = pipe.execute(); return res[1] < max_t
    b = _local_rl[key]; now=time.time()
    b["t"]=min(max_t,b["t"]+(now-b["ts"])*rate); b["ts"]=now
    if b["t"]>=1: b["t"]-=1; return True
    return False

def rate_limit(key_fn,max_t=10,rate=1.0):
    def dec(f):
        @wraps(f)
        def w(*a,**kw):
            k = key_fn() if callable(key_fn) else key_fn
            if not _allow(k,max_t,rate):
                log.warning("Rate limit: %s",k)
                return jsonify(error="Too many requests. Please wait."),429
            return f(*a,**kw)
        return w
    return dec

# ══════════════════════════════════════════════════════════════════════════════
#  CSRF  — generated per-session, validated on every state-changing POST
# ══════════════════════════════════════════════════════════════════════════════
def _csrf() -> str:
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(32)
    return session["csrf_token"]

@app.context_processor
def inject_globals():
    # Expose csrf_token as a CALLABLE so both {{ csrf_token }} and {{ csrf_token() }}
    # work in templates — the old v2 templates use the () form.
    return {"csrf_token": _csrf, "store_url": STORE_URL, "exness_url": EXNESS_URL}

def csrf_required(f):
    @wraps(f)
    def wrapper(*a,**kw):
        if request.method in ("POST","PUT","DELETE","PATCH"):
            tok = (request.form.get("_csrf")
                   or request.headers.get("X-CSRF-Token")
                   or (request.get_json(silent=True) or {}).get("_csrf",""))
            exp = session.get("csrf_token","")
            if not exp or not hmac.compare_digest(exp, tok or ""):
                log.warning("CSRF fail: %s %s",request.path,request.remote_addr)
                _audit("csrf_failure",request.path)
                if request.is_json or request.path.startswith("/admin/api"):
                    return jsonify(error="CSRF validation failed."),403
                return render_template("error.html",msg="Session expired. Go back and try again."),403
        return f(*a,**kw)
    return wrapper

# ══════════════════════════════════════════════════════════════════════════════
#  AUTH
# ══════════════════════════════════════════════════════════════════════════════
def _check_pass(pw:str)->bool:
    if ADMIN_PASS_HASH:
        return check_password_hash(ADMIN_PASS_HASH,pw)
    if ADMIN_PASS_PLAIN:
        log.warning("Using plaintext ADMIN_PASS — set ADMIN_PASS_HASH instead")
        return hmac.compare_digest(ADMIN_PASS_PLAIN,pw)
    return False

def login_required(f):
    @wraps(f)
    def d(*a,**kw):
        if not session.get("admin"): return redirect(url_for("admin_login"))
        return f(*a,**kw)
    return d

def _valid_key(k:str)->bool:
    return any(k and hmac.compare_digest(k,valid) for valid in [BOT_API_KEY,USERBOT_API_KEY,API_SECRET] if valid)

def api_key_required(f):
    @wraps(f)
    def w(*a,**kw):
        if not _valid_key(request.headers.get("X-API-Key","")):
            return jsonify(error="Unauthorized"),401
        return f(*a,**kw)
    return w

def _admin_post(f):
    return login_required(csrf_required(f))

# ══════════════════════════════════════════════════════════════════════════════
#  AUDIT LOG
# ══════════════════════════════════════════════════════════════════════════════
def _audit(action,target="",detail="",actor="system"):
    try:
        ip = request.remote_addr if request else ""
        query(f"INSERT INTO audit_log(actor,action,target,detail,ip_address,created_at)"
              f" VALUES({PH},{PH},{PH},{PH},{PH},{PH})",
              (actor,action,str(target)[:200],str(detail)[:500],ip,now_iso()),commit=True)
    except Exception as e:
        log.warning("Audit fail: %s",e)

# ══════════════════════════════════════════════════════════════════════════════
#  EMAIL
# ══════════════════════════════════════════════════════════════════════════════
def send_email(to:str, subject:str, html:str)->bool:
    if not SMTP_USER or not SMTP_PASS:
        log.warning("Email not configured — skip send to %s",to); return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"]=subject; msg["From"]=SMTP_USER; msg["To"]=to
        msg.attach(MIMEText(html,"html"))
        ctx=ssl.create_default_context()
        with smtplib.SMTP(SMTP_HOST,SMTP_PORT) as s:
            s.ehlo(); s.starttls(context=ctx); s.login(SMTP_USER,SMTP_PASS)
            s.sendmail(SMTP_USER,to,msg.as_string())
        log.info("Email sent → %s: %s",to,subject); return True
    except Exception as e:
        log.error("Email failed → %s: %s",to,e); return False

# ══════════════════════════════════════════════════════════════════════════════
#  PAYPAL IPN
# ══════════════════════════════════════════════════════════════════════════════
def verify_ipn(raw:bytes)->(bool,str):
    try:
        body=b"cmd=_notify-validate&"+raw
        ipn=PAYPAL_SANDBOX_IPN if SANDBOX else PAYPAL_IPN_URL
        req=urllib.request.Request(ipn,data=body,
            headers={"Content-Type":"application/x-www-form-urlencoded","User-Agent":"ForexEdge/3.0"})
        with urllib.request.urlopen(req,timeout=15) as r:
            res=r.read().decode()
        return (True,"ok") if res=="VERIFIED" else (False,res)
    except Exception as e:
        return False,str(e)

# ══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════════════
def validate_coupon(code:str):
    if not code: return None
    row=query(f"SELECT * FROM coupons WHERE UPPER(code)=UPPER({PH}) AND active=1",(code,),fetchone=True)
    if not row: return None
    if row.get("max_uses") and row["uses"]>=row["max_uses"]: return None
    if row.get("expires_at"):
        exp=row["expires_at"]
        if isinstance(exp,str): exp=datetime.datetime.fromisoformat(exp)
        if exp<datetime.datetime.utcnow(): return None
    return row

def calc_price(base:float,coupon)->float:
    if not coupon: return base
    if coupon.get("discount_usd"): return max(0,round(base-float(coupon["discount_usd"]),2))
    if coupon.get("discount_pct"): return round(base*(1-int(coupon["discount_pct"])/100),2)
    return base

def token_expiry():
    if not DOWNLOAD_TTL_DAYS: return None
    return (datetime.datetime.utcnow()+datetime.timedelta(days=DOWNLOAD_TTL_DAYS)).isoformat()

def token_expired(row:dict)->bool:
    exp=row.get("token_expires_at")
    if not exp: return False
    if isinstance(exp,str): exp=datetime.datetime.fromisoformat(exp)
    return exp<datetime.datetime.utcnow()

def err(msg:str,code:int=400):
    return render_template("error.html",msg=msg),code

# ── Referral cookie ────────────────────────────────────────────────────────────
@app.before_request
def _track_ref():
    ref=request.args.get("ref","").strip().upper()
    if ref and not request.cookies.get("ref"): session["_ref"]=ref

@app.after_request
def _set_ref(response):
    ref=session.pop("_ref",None)
    if ref: response.set_cookie("ref",ref,max_age=30*86400,httponly=True,
                                 samesite="Lax",secure=app.config["SESSION_COOKIE_SECURE"])
    return response

# ══════════════════════════════════════════════════════════════════════════════
#  REFERRAL AUDIT (runs last day of month; email to morrynet@gmail.com)
# ══════════════════════════════════════════════════════════════════════════════
def run_referral_audit(triggered_by:str="cron")->dict:
    period=datetime.date.today().strftime("%Y-%m")
    existing=query(f"SELECT COUNT(*) AS c FROM payouts WHERE period={PH}",(period,),fetchone=True)
    if existing and existing.get("c",0)>0:
        return {"ok":False,"error":f"Audit for {period} already ran","payouts_created":0}

    refs=query(
        "SELECT r.*, "
        f"  COALESCE((SELECT COUNT(*) FROM purchases p WHERE p.referral_code=r.code AND p.confirmed=1 AND p.confirmed_at LIKE {PH}),0) AS month_count "
        "FROM referrals r WHERE r.active=1",
        (period+"%",),fetchall=True) or []

    created=0; lines=[]
    for ref in refs:
        cnt=int(ref.get("month_count",0))
        if cnt==0: continue
        amount=round(cnt*REFERRAL_FEE_USD,2)
        query(f"INSERT INTO payouts(referral_code,paypal_email,period,amount,status,notes,created_at)"
              f" VALUES({PH},{PH},{PH},{PH},'pending',{PH},{PH})",
              (ref["code"],ref.get("paypal_email",""),period,amount,
               f"{cnt} sale(s) × ${REFERRAL_FEE_USD:.2f}",now_iso()),commit=True)
        query(f"UPDATE referrals SET total_earned=total_earned+{PH} WHERE code={PH}",(amount,ref["code"]),commit=True)
        created+=1
        lines.append(f"<tr><td>{ref.get('name','?')}</td><td>{ref['code']}</td>"
                     f"<td>{ref.get('paypal_email','⚠️ MISSING')}</td>"
                     f"<td>{cnt}</td><td><b>${amount:.2f}</b></td></tr>")

    total=(query("SELECT COALESCE(SUM(amount),0) AS s FROM payouts WHERE status='pending'",fetchone=True) or {}).get("s",0)
    table=("<table border='1' cellpadding='8' style='border-collapse:collapse'>"
           "<tr><th>Name</th><th>Code</th><th>PayPal</th><th>Sales</th><th>Amount</th></tr>"
           +"".join(lines)+"</table>" if lines else "<p>No commissions this period.</p>")
    html=(f"<html><body style='font-family:Arial'><h2 style='color:#00d4a0'>ForexEdge Referral Audit — {period}</h2>"
          f"<p>Triggered by: <b>{triggered_by}</b> | Payout due: <b>5th of next month</b></p>"
          f"{table}<p><b>Total Pending: ${float(total):.2f}</b></p>"
          f"<p><a href='{STORE_URL}/admin'>Open Admin → Payouts</a></p>"
          f"<hr><small>ForexEdge automated system · {now_iso()}</small></body></html>")
    sent=send_email(ADMIN_EMAIL,f"ForexEdge Referral Audit {period} — ${float(total):.2f} due",html)
    _audit("referral_audit",period,f"created={created} total=${float(total):.2f} email={sent}",actor=triggered_by)
    return {"ok":True,"period":period,"payouts_created":created,"total_due":float(total),"email_sent":sent}

# ══════════════════════════════════════════════════════════════════════════════
#  STOREFRONT
# ══════════════════════════════════════════════════════════════════════════════
@app.route("/")
def storefront():
    products=query("SELECT id,name,description,category,price FROM products WHERE active=1 ORDER BY created_at DESC",fetchall=True) or []
    return render_template("store.html",products=products,price=PRICE_USD,paypal_url=PAYPAL_BUTTON_URL)

@app.route("/buy/<product_id>")
@rate_limit(lambda:f"buy:{request.remote_addr}",max_t=20)
def buy(product_id):
    prod=query(f"SELECT id,name,price FROM products WHERE id={PH} AND active=1",(product_id,),fetchone=True)
    if not prod: return jsonify(error="Not found"),404
    session["cart_product_id"]=product_id
    return jsonify(ok=True,product_id=product_id,name=prod["name"],price=float(prod.get("price") or 20))

@app.route("/coupon/check",methods=["POST"])
@rate_limit(lambda:f"coupon:{request.remote_addr}",max_t=10,rate=0.5)
def coupon_check():
    code=(request.get_json(silent=True) or {}).get("code","").strip().upper()
    c=validate_coupon(code)
    if not c: return jsonify(valid=False,msg="Invalid or expired coupon.")
    disc=f"{c['discount_pct']}% off" if c.get("discount_pct") else f"${float(c.get('discount_usd',0)):.2f} off"
    return jsonify(valid=True,msg=disc,original=PRICE_USD,final=f"{calc_price(float(PRICE_USD),c):.2f}")

@app.route("/payment/success")
def payment_success():
    products=query("SELECT id,name,category FROM products WHERE active=1 ORDER BY name",fetchall=True) or []
    return render_template("success.html",tx=request.args.get("tx",""),
                           product_id=session.get("cart_product_id",""),products=products,price=PRICE_USD)

@app.route("/payment/claim",methods=["POST"])
@csrf_required
@rate_limit(lambda:f"claim:{request.remote_addr}",max_t=5,rate=0.05)
def claim_download():
    tx=request.form.get("tx","").strip(); email=request.form.get("email","").strip().lower()
    pid=request.form.get("product_id","").strip(); cc=request.form.get("coupon_code","").strip().upper()
    if not tx or not email or not pid: return err("All fields are required.")
    if "@" not in email or "." not in email.split("@")[-1] or len(email)>254: return err("Invalid email.")
    if len(tx)>120: return err("Invalid transaction ID.")
    existing=query(f"SELECT download_token,product_id,downloads_used,max_downloads FROM purchases WHERE txn_id={PH}",(tx,),fetchone=True)
    if existing:
        prod=query(f"SELECT name FROM products WHERE id={PH}",(existing["product_id"],),fetchone=True)
        return render_template("download.html",token=existing["download_token"],used=existing["downloads_used"],
                               max=existing["max_downloads"],product_name=(prod or {}).get("name","Your File"),email=email)
    prod=query(f"SELECT id,name,price FROM products WHERE id={PH} AND active=1",(pid,),fetchone=True)
    if not prod: return err("Invalid product.")
    coupon=validate_coupon(cc) if cc else None
    amount=calc_price(float(prod.get("price") or 20),coupon)
    token=secrets.token_hex(32)   # 64-char hex token
    ref=request.cookies.get("ref","")
    try:
        query(f"INSERT INTO purchases(id,product_id,email,txn_id,download_token,downloads_used,"
              f"max_downloads,confirmed,amount_usd,coupon_code,referral_code,ip_address,created_at,token_expires_at)"
              f" VALUES({PH},{PH},{PH},{PH},{PH},0,{PH},0,{PH},{PH},{PH},{PH},{PH},{PH})",
              (str(uuid.uuid4()),pid,email,tx,token,MAX_DOWNLOADS,amount,cc or None,ref or None,
               request.remote_addr,now_iso(),token_expiry()),commit=True)
    except Exception:
        return err("Transaction ID already used or database error.")
    if coupon: query(f"UPDATE coupons SET uses=uses+1 WHERE UPPER(code)=UPPER({PH})",(cc,),commit=True)
    if ref: query(f"UPDATE referrals SET total_sales=total_sales+1 WHERE code={PH}",(ref,),commit=True)
    session.pop("cart_product_id",None)
    _audit("claim_download",tx,f"email={email} amount={amount}")
    return render_template("download.html",token=token,used=0,max=MAX_DOWNLOADS,product_name=prod["name"],email=email)

@app.route("/download/<token>")
@rate_limit(lambda:f"dl:{request.remote_addr}",max_t=15,rate=0.3)
def download_file(token):
    if not token or len(token)!=64 or not all(c in "0123456789abcdef" for c in token):
        return err("Invalid download link.",404)
    row=query(f"SELECT pu.id,pu.downloads_used,pu.max_downloads,pu.token_expires_at,"
              f"pr.name,pr.filename,pr.filedata FROM purchases pu"
              f" JOIN products pr ON pu.product_id=pr.id WHERE pu.download_token={PH}",(token,),fetchone=True)
    if not row: return err("Invalid or expired link.",404)
    if token_expired(row): return err("Link expired. Use /lookup to get a new one.",403)
    if row["downloads_used"]>=row["max_downloads"]: return err(f"Download limit reached ({row['max_downloads']}). Contact support.",403)
    if not row["filedata"]: return err("File not available. Contact support.",503)
    query(f"UPDATE purchases SET downloads_used=downloads_used+1 WHERE download_token={PH}",(token,),commit=True)
    _audit("file_download",token[:12],f"file={row['filename']} {row['downloads_used']+1}/{row['max_downloads']}")
    filename=row["filename"] or f"{row['name']}.ex4"
    data=bytes(row["filedata"]) if isinstance(row["filedata"],memoryview) else row["filedata"]
    mime=mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return send_file(BytesIO(data),mimetype=mime,as_attachment=True,download_name=filename)

@app.route("/lookup",methods=["GET","POST"])
@csrf_required
@rate_limit(lambda:f"lookup:{request.remote_addr}",max_t=10,rate=0.2)
def lookup():
    if request.method=="GET": return render_template("lookup.html")
    txn=request.form.get("txn_id","").strip(); email=request.form.get("email","").strip().lower()
    if not txn or not email: return render_template("lookup.html",error="Enter both fields.")
    row=query(f"SELECT pu.download_token,pu.downloads_used,pu.max_downloads,pu.token_expires_at,pr.name"
              f" FROM purchases pu LEFT JOIN products pr ON pu.product_id=pr.id"
              f" WHERE pu.txn_id={PH} AND LOWER(pu.email)={PH}",(txn,email),fetchone=True)
    if not row: return render_template("lookup.html",error="No purchase found. Check your Transaction ID and email.")
    _audit("lookup",txn,f"email={email}")
    return render_template("download.html",token=row["download_token"],used=row["downloads_used"],
                           max=row["max_downloads"],product_name=row.get("name") or "Your File",email=email)

# ══════════════════════════════════════════════════════════════════════════════
#  PAYPAL IPN
# ══════════════════════════════════════════════════════════════════════════════
@app.route("/webhook/paypal/ipn",methods=["POST"])
def paypal_ipn():
    raw=request.get_data()
    flat={k:v[0] for k,v in urllib.parse.parse_qs(raw.decode("utf-8",errors="replace")).items()}
    try:
        query(f"INSERT INTO transactions(sub_id,event_type,payload,created_at) VALUES({PH},{PH},{PH},{PH})",
              (flat.get("txn_id",""),flat.get("payment_status",""),json.dumps(flat)[:4000],now_iso()),commit=True)
    except Exception as e: log.error("IPN log: %s",e)

    ok,reason=verify_ipn(raw)
    if not ok:
        log.warning("IPN INVALID: %s from %s",reason,request.remote_addr)
        _audit("ipn_invalid",flat.get("txn_id",""),f"reason={reason}")
        return "INVALID",200

    receiver=flat.get("receiver_email","").lower(); currency=flat.get("mc_currency","")
    status=flat.get("payment_status",""); txn=flat.get("txn_id",""); email=flat.get("payer_email","").lower()
    try: amount=float(flat.get("mc_gross","0") or "0")
    except: amount=0.0

    # Receiver email guard
    if PAYPAL_RECEIVER and not SANDBOX and receiver!=PAYPAL_RECEIVER.lower():
        log.warning("IPN receiver mismatch: %s",receiver); _audit("ipn_receiver_mismatch",txn,f"got={receiver}"); return "MISMATCH",200
    # Currency guard
    if currency and currency.upper()!="USD":
        log.warning("IPN currency: %s",currency); _audit("ipn_currency",txn,f"currency={currency}"); return "CURRENCY",200
    # Low-amount guard
    if status=="Completed" and amount<1.0:
        log.warning("IPN low amount %.2f txn=%s",amount,txn); _audit("ipn_low_amount",txn,f"amt={amount}"); return "LOW",200

    _audit("ipn_verified",txn,f"status={status} amount={amount}")
    if status=="Completed":
        t=now_iso()
        query(f"UPDATE purchases SET confirmed=1,confirmed_at={PH},amount_usd={PH} WHERE txn_id={PH}",(t,amount,txn),commit=True)
        if not query(f"SELECT id FROM purchases WHERE txn_id={PH}",(txn,),fetchone=True):
            query(f"INSERT INTO purchases(id,product_id,email,txn_id,download_token,downloads_used,max_downloads,"
                  f"confirmed,amount_usd,created_at,confirmed_at,token_expires_at)"
                  f" VALUES({PH},NULL,{PH},{PH},{PH},0,{PH},1,{PH},{PH},{PH},{PH})",
                  (str(uuid.uuid4()),email,txn,secrets.token_hex(32),MAX_DOWNLOADS,amount,t,t,token_expiry()),commit=True)
    elif status in ("Refunded","Reversed"):
        query(f"UPDATE purchases SET confirmed=0 WHERE txn_id={PH}",(txn,),commit=True)
        _audit(f"ipn_{status.lower()}",txn)
    return "OK",200

# ══════════════════════════════════════════════════════════════════════════════
#  LICENSE CHECK
# ══════════════════════════════════════════════════════════════════════════════
@app.route("/license/check",methods=["POST"])
@rate_limit(lambda:f"lic:{request.remote_addr}",max_t=20,rate=0.5)
def license_check():
    data=request.get_json(force=True,silent=True) or {}
    key=data.get("license","").strip().upper()[:100]; account=data.get("account","").strip()[:100]
    if not key or not account: return jsonify(valid=False,reason="missing_fields"),400
    row=query(f"SELECT expiry,account FROM licenses WHERE key={PH}",(key,),fetchone=True)
    if not row: return jsonify(valid=False,reason="not_found")
    expiry=row["expiry"]
    if isinstance(expiry,str): expiry=datetime.datetime.fromisoformat(expiry.replace("Z","+00:00"))
    if getattr(expiry,"tzinfo",None) is None: expiry=expiry.replace(tzinfo=datetime.timezone.utc)
    now=datetime.datetime.now(datetime.timezone.utc)
    if expiry<now: return jsonify(valid=False,reason="expired")
    bound=row.get("account")
    if bound and bound!=account: return jsonify(valid=False,reason="bound_to_other_account")
    if not bound: query(f"UPDATE licenses SET account={PH} WHERE key={PH}",(account,key),commit=True)
    return jsonify(valid=True,days_remaining=(expiry-now).days)

# ══════════════════════════════════════════════════════════════════════════════
#  PUBLIC BOT API
# ══════════════════════════════════════════════════════════════════════════════
@app.route("/api/v1/products")
@api_key_required
@rate_limit(lambda:f"api:{request.remote_addr}",max_t=60,rate=2.0)
def api_v1_products():
    return jsonify(products=query("SELECT id,name,description,category,price FROM products WHERE active=1",fetchall=True) or [])

@app.route("/api/v1/stats")
@api_key_required
@rate_limit(lambda:f"api:{request.remote_addr}",max_t=30,rate=0.5)
def api_v1_stats():
    confirmed=(query("SELECT COUNT(*) AS c FROM purchases WHERE confirmed=1",fetchone=True) or {}).get("c",0)
    revenue=(query("SELECT COALESCE(SUM(amount_usd),0) AS s FROM purchases WHERE confirmed=1",fetchone=True) or {}).get("s",0)
    return jsonify(confirmed=confirmed,revenue=float(revenue))

# ── Cron ────────────────────────────────────────────────────────────────────────
@app.route("/cron/monthly-audit",methods=["POST"])
def cron_monthly_audit():
    k=request.headers.get("X-Cron-Key","")
    if not CRON_KEY or not hmac.compare_digest(k,CRON_KEY): return jsonify(error="Unauthorized"),401
    return jsonify(run_referral_audit("cron"))

# ══════════════════════════════════════════════════════════════════════════════
#  ADMIN
# ══════════════════════════════════════════════════════════════════════════════
@app.route("/admin/login",methods=["GET","POST"])
@rate_limit(lambda:f"login:{request.remote_addr}",max_t=5,rate=0.05)
def admin_login():
    error=""
    if request.method=="POST":
        u=request.form.get("username",""); p=request.form.get("password","")
        if hmac.compare_digest(u,ADMIN_USER) and _check_pass(p):
            session.update({"admin":True,"csrf_token":secrets.token_hex(32)}); session.permanent=True
            _audit("admin_login",actor=u); return redirect(url_for("admin_dashboard"))
        error="Invalid credentials"; _audit("admin_login_fail",actor=u)
    return render_template("login.html",error=error)

@app.route("/admin/logout")
def admin_logout(): session.clear(); return redirect(url_for("admin_login"))

@app.route("/admin")
@login_required
def admin_dashboard(): return render_template("admin.html")

@app.route("/admin/api/stats")
@login_required
def api_stats():
    today=datetime.date.today().isoformat()
    return jsonify(
        products=(query("SELECT COUNT(*) AS c FROM products WHERE active=1",fetchone=True) or {}).get("c",0),
        purchases=(query("SELECT COUNT(*) AS c FROM purchases",fetchone=True) or {}).get("c",0),
        confirmed=(query("SELECT COUNT(*) AS c FROM purchases WHERE confirmed=1",fetchone=True) or {}).get("c",0),
        revenue=float((query("SELECT COALESCE(SUM(amount_usd),0) AS s FROM purchases WHERE confirmed=1",fetchone=True) or {}).get("s",0)),
        today_sales=(query(f"SELECT COUNT(*) AS c FROM purchases WHERE confirmed=1 AND confirmed_at LIKE {PH}",(today+"%",),fetchone=True) or {}).get("c",0),
        pending_payouts=float((query("SELECT COALESCE(SUM(amount),0) AS s FROM payouts WHERE status='pending'",fetchone=True) or {}).get("s",0)),
    )

@app.route("/admin/api/products")
@login_required
def api_products():
    return jsonify(products=query("SELECT id,name,description,category,filename,active,price,created_at FROM products ORDER BY created_at DESC",fetchall=True) or [])

@app.route("/admin/api/products/upload",methods=["POST"])
@login_required
def api_upload_product():
    name=request.form.get("name","").strip()
    if not name: return jsonify(error="Name required"),400
    pid=str(uuid.uuid4()); f=request.files.get("file"); filename=filedata=None
    if f and f.filename: filename=f.filename; filedata=f.read()
    query(f"INSERT INTO products(id,name,description,category,filename,filedata,active,price,created_at) VALUES({PH},{PH},{PH},{PH},{PH},{PH},1,{PH},{PH})",
          (pid,name,request.form.get("description",""),request.form.get("category","EA"),filename,filedata,
           float(request.form.get("price",PRICE_USD) or PRICE_USD),now_iso()),commit=True)
    _audit("product_upload",pid,f"name={name}",actor="admin"); return jsonify(id=pid,name=name,has_file=bool(filedata))

@app.route("/admin/api/products/toggle",methods=["POST"])
@_admin_post
def api_toggle_product():
    pid=(request.get_json(force=True,silent=True) or {}).get("id","")
    row=query(f"SELECT active FROM products WHERE id={PH}",(pid,),fetchone=True)
    if not row: return jsonify(error="Not found"),404
    nv=0 if row["active"] else 1
    query(f"UPDATE products SET active={PH} WHERE id={PH}",(nv,pid),commit=True)
    _audit("product_toggle",pid,f"active={bool(nv)}",actor="admin"); return jsonify(active=bool(nv))

@app.route("/admin/api/products/delete",methods=["POST"])
@_admin_post
def api_delete_product():
    pid=(request.get_json(force=True,silent=True) or {}).get("id","")
    query(f"DELETE FROM products WHERE id={PH}",(pid,),commit=True)
    _audit("product_delete",pid,actor="admin"); return jsonify(ok=True)

@app.route("/admin/api/purchases")
@login_required
def api_purchases():
    return jsonify(purchases=query(
        "SELECT pu.id,pu.email,pu.txn_id,pu.download_token,pu.downloads_used,pu.max_downloads,"
        "pu.confirmed,pu.amount_usd,pu.coupon_code,pu.referral_code,pu.created_at,pr.name AS product_name "
        "FROM purchases pu LEFT JOIN products pr ON pu.product_id=pr.id ORDER BY pu.created_at DESC",fetchall=True) or [])

@app.route("/admin/api/purchases/reset",methods=["POST"])
@_admin_post
def api_reset_downloads():
    pid=(request.get_json(force=True,silent=True) or {}).get("id","")
    query(f"UPDATE purchases SET downloads_used=0 WHERE id={PH}",(pid,),commit=True)
    _audit("reset_downloads",pid,actor="admin"); return jsonify(ok=True)

@app.route("/admin/api/purchases/extend",methods=["POST"])
@_admin_post
def api_extend_downloads():
    data=request.get_json(force=True,silent=True) or {}; pid=data.get("id","")
    extra=min(int(data.get("extra",3)),20)
    query(f"UPDATE purchases SET max_downloads=max_downloads+{PH} WHERE id={PH}",(extra,pid),commit=True)
    _audit("extend_downloads",pid,f"+{extra}",actor="admin"); return jsonify(ok=True)

@app.route("/admin/api/licenses")
@login_required
def api_licenses():
    return jsonify(licenses=query("SELECT key,expiry,account,plan,email,created_at FROM licenses ORDER BY created_at DESC",fetchall=True) or [])

@app.route("/admin/api/licenses/create",methods=["POST"])
@_admin_post
def api_create_license():
    data=request.get_json(force=True,silent=True) or {}; key=str(uuid.uuid4()).upper()
    days=max(1,min(int(data.get("days",31)),36500))
    expiry=(datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(days=days)).isoformat()
    query(f"INSERT INTO licenses(key,expiry,plan,email,created_at) VALUES({PH},{PH},{PH},{PH},{PH})",
          (key,expiry,data.get("plan","monthly"),data.get("email",""),now_iso()),commit=True)
    _audit("create_license",key,actor="admin"); return jsonify(key=key,expiry=expiry)

@app.route("/admin/api/licenses/revoke",methods=["POST"])
@_admin_post
def api_revoke_license():
    key=(request.get_json(force=True,silent=True) or {}).get("key","")
    past=(datetime.datetime.now(datetime.timezone.utc)-datetime.timedelta(seconds=1)).isoformat()
    query(f"UPDATE licenses SET expiry={PH} WHERE key={PH}",(past,key),commit=True)
    _audit("revoke_license",key,actor="admin"); return jsonify(ok=True)

@app.route("/admin/api/coupons")
@login_required
def api_coupons():
    return jsonify(coupons=query("SELECT * FROM coupons ORDER BY created_at DESC",fetchall=True) or [])

@app.route("/admin/api/coupons/create",methods=["POST"])
@_admin_post
def api_create_coupon():
    data=request.get_json(force=True,silent=True) or {}
    code=data.get("code","").strip().upper() or secrets.token_hex(4).upper()
    query(f"INSERT INTO coupons(code,discount_pct,discount_usd,max_uses,uses,active,expires_at,created_at)"
          f" VALUES({PH},{PH},{PH},{PH},0,1,{PH},{PH})",
          (code,max(0,min(100,int(data.get("discount_pct",0)))),max(0.0,float(data.get("discount_usd",0))),
           max(0,int(data.get("max_uses",0))),data.get("expires_at") or None,now_iso()),commit=True)
    _audit("create_coupon",code,actor="admin"); return jsonify(code=code)

@app.route("/admin/api/coupons/delete",methods=["POST"])
@_admin_post
def api_delete_coupon():
    code=(request.get_json(force=True,silent=True) or {}).get("code","").upper()
    query(f"UPDATE coupons SET active=0 WHERE code={PH}",(code,),commit=True); return jsonify(ok=True)

@app.route("/admin/api/referrals")
@login_required
def api_referrals():
    rows=query(
        "SELECT r.*, "
        f"COALESCE((SELECT SUM(amount) FROM payouts WHERE referral_code=r.code AND status='pending'),0) AS pending_payout,"
        f"COALESCE((SELECT SUM(amount) FROM payouts WHERE referral_code=r.code AND status='paid'),0) AS total_paid "
        "FROM referrals r ORDER BY r.total_sales DESC",fetchall=True) or []
    return jsonify(referrals=rows)

@app.route("/admin/api/referrals/create",methods=["POST"])
@_admin_post
def api_create_referral():
    data=request.get_json(force=True,silent=True) or {}
    code=data.get("code","").strip().upper() or secrets.token_hex(4).upper()
    query(f"INSERT INTO referrals(code,name,email,paypal_email,commission_usd,active,created_at)"
          f" VALUES({PH},{PH},{PH},{PH},{PH},1,{PH})",
          (code,data.get("name",""),data.get("email",""),data.get("paypal_email",""),REFERRAL_FEE_USD,now_iso()),commit=True)
    _audit("create_referral",code,actor="admin"); return jsonify(code=code,link=f"{STORE_URL}/?ref={code}")

@app.route("/admin/api/referrals/toggle",methods=["POST"])
@_admin_post
def api_toggle_referral():
    code=(request.get_json(force=True,silent=True) or {}).get("code","")
    row=query(f"SELECT active FROM referrals WHERE code={PH}",(code,),fetchone=True)
    if not row: return jsonify(error="Not found"),404
    nv=0 if row["active"] else 1
    query(f"UPDATE referrals SET active={PH} WHERE code={PH}",(nv,code),commit=True); return jsonify(active=bool(nv))

@app.route("/admin/api/payouts")
@login_required
def api_payouts():
    return jsonify(payouts=query("SELECT * FROM payouts ORDER BY created_at DESC",fetchall=True) or [])

@app.route("/admin/api/payouts/trigger-audit",methods=["POST"])
@_admin_post
def api_trigger_audit():
    return jsonify(run_referral_audit(f"admin:{ADMIN_USER}"))

@app.route("/admin/api/payouts/mark-paid",methods=["POST"])
@_admin_post
def api_mark_payout_paid():
    data=request.get_json(force=True,silent=True) or {}; pid=int(data.get("id",0))
    notes=str(data.get("notes",""))[:500]
    query(f"UPDATE payouts SET status='paid',paid_at={PH},notes=COALESCE(notes,'')||' | '||{PH} WHERE id={PH}",
          (now_iso(),notes,pid),commit=True)
    _audit("payout_paid",str(pid),notes,actor="admin"); return jsonify(ok=True)

@app.route("/admin/api/audit")
@login_required
def api_audit():
    limit=min(int(request.args.get("limit",500)),2000)
    return jsonify(logs=query(f"SELECT * FROM audit_log ORDER BY id DESC LIMIT {PH}",(limit,),fetchall=True) or [])

@app.route("/health")
def health():
    db_ok=redis_ok=True
    try: query("SELECT 1",fetchone=True)
    except: db_ok=False
    if _rc:
        try: _rc.ping()
        except: redis_ok=False
    status="ok" if (db_ok and (not _rc or redis_ok)) else "degraded"
    return jsonify(status=status,db=db_ok,redis=redis_ok if _rc else "not_configured")

if __name__=="__main__":
    from migrations import run_migrations
    run_migrations()
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT",5000)),debug=False)


# ══════════════════════════════════════════════════════════════════════════════
#  BOT THREAD — runs the Telegram bot inside the web process (free tier hack)
#  Set RUN_BOT=true in environment to enable.
# ══════════════════════════════════════════════════════════════════════════════
def _start_bot_thread():
    import threading, importlib
    def _run():
        try:
            bot_mod = importlib.import_module("bot")
            bot_mod.main()
        except Exception as e:
            log.error("Bot thread crashed: %s", e)
    t = threading.Thread(target=_run, name="telegram-bot", daemon=True)
    t.start()
    log.info("Telegram bot started in background thread.")

if os.environ.get("RUN_BOT", "false").lower() == "true":
    _start_bot_thread()
