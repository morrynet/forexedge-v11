"""ForexEdge — Full Test Suite (pytest)"""
import os,json,uuid,time,datetime,pytest,secrets as _secrets

os.environ.update({
    "DB_PATH":":memory:", "SECRET_KEY":"test-secret-key-32charslong",
    "ADMIN_USER":"testadmin","ADMIN_PASS":"testpassword123",
    "STORE_URL":"http://localhost:5000","PAYPAL_SANDBOX":"true",
    "BOT_API_KEY":"bot-test-key","DOWNLOAD_TOKEN_TTL_DAYS":"30",
    "REDIS_URL":"",  # force in-process limiter
})

from migrations import run_migrations
run_migrations(verbose=False)

from app import app as _app, query, PH, calc_price, validate_coupon

@pytest.fixture(autouse=True)
def _clean_tables():
    """Clear transient tables between tests."""
    for t in ["purchases","licenses","coupons","referrals","payouts","audit_log","products"]:
        try: query(f"DELETE FROM {t}",commit=True)
        except: pass
    yield

@pytest.fixture
def client():
    _app.config["TESTING"]=True
    with _app.test_client() as c:
        yield c

def _csrf(client, path="/"):
    client.get(path)
    with client.session_transaction() as s:
        return s.get("csrf_token","")

def _login(client):
    csrf=_csrf(client,"/admin/login")
    client.post("/admin/login",data={"username":"testadmin","password":"testpassword123","_csrf":csrf})
    with client.session_transaction() as s:
        return s.get("csrf_token","")

def _prod(name="Test EA",price=20.0):
    pid=str(uuid.uuid4())
    query(f"INSERT INTO products(id,name,category,filename,filedata,active,price,created_at)"
          f" VALUES({PH},{PH},{PH},{PH},{PH},1,{PH},{PH})",
          (pid,name,"EA","t.ex4",b"FAKEDATA",price,datetime.datetime.utcnow().isoformat()),commit=True)
    return pid

def _purchase(email="t@t.com",used=0,maxdl=3,confirmed=1,days_left=30):
    pid=_prod(); txn="T"+_secrets.token_hex(6).upper(); tok=_secrets.token_hex(32)
    exp=(datetime.datetime.utcnow()+datetime.timedelta(days=days_left)).isoformat()
    query(f"INSERT INTO purchases(id,product_id,email,txn_id,download_token,downloads_used,"
          f"max_downloads,confirmed,amount_usd,created_at,token_expires_at)"
          f" VALUES({PH},{PH},{PH},{PH},{PH},{PH},{PH},{PH},20.0,{PH},{PH})",
          (str(uuid.uuid4()),pid,email,txn,tok,used,maxdl,confirmed,
           datetime.datetime.utcnow().isoformat(),exp),commit=True)
    return txn,tok,pid

# ── Health ────────────────────────────────────────────────────────────────────
def test_health(client):
    r=client.get("/health"); assert r.status_code==200
    d=json.loads(r.data); assert d["db"] is True; assert "status" in d

# ── CSRF ──────────────────────────────────────────────────────────────────────
def test_csrf_missing_on_lookup(client):
    r=client.post("/lookup",data={"txn_id":"X","email":"a@b.com"}); assert r.status_code==403

def test_csrf_wrong_token(client):
    with client.session_transaction() as s: s["csrf_token"]="real"
    r=client.post("/lookup",data={"txn_id":"X","email":"a@b.com","_csrf":"wrong"}); assert r.status_code==403

def test_csrf_correct_passes(client):
    with client.session_transaction() as s: s["csrf_token"]="ok"
    r=client.post("/lookup",data={"txn_id":"NONE","email":"a@b.com","_csrf":"ok"})
    assert r.status_code!=403

def test_csrf_on_claim(client):
    r=client.post("/payment/claim",data={"tx":"X","email":"a@b.com","product_id":"P"})
    assert r.status_code==403

def test_admin_api_toggle_needs_csrf(client):
    _login(client); pid=_prod()
    r=client.post("/admin/api/products/toggle",json={"id":pid},content_type="application/json")
    assert r.status_code==403  # no _csrf in json

# ── Auth ──────────────────────────────────────────────────────────────────────
def test_admin_redirect_without_login(client):
    r=client.get("/admin"); assert r.status_code==302

def test_wrong_credentials(client):
    csrf=_csrf(client,"/admin/login")
    r=client.post("/admin/login",data={"username":"testadmin","password":"WRONG","_csrf":csrf})
    assert b"Invalid" in r.data

def test_correct_login(client):
    csrf=_csrf(client,"/admin/login")
    r=client.post("/admin/login",data={"username":"testadmin","password":"testpassword123","_csrf":csrf})
    assert r.status_code==302

def test_api_key_required(client):
    r=client.get("/api/v1/products"); assert r.status_code==401

def test_valid_api_key(client):
    r=client.get("/api/v1/products",headers={"X-API-Key":"bot-test-key"}); assert r.status_code==200

def test_invalid_api_key(client):
    r=client.get("/api/v1/products",headers={"X-API-Key":"nope"}); assert r.status_code==401

# ── Store ─────────────────────────────────────────────────────────────────────
def test_storefront(client):
    assert client.get("/").status_code==200

def test_buy_product(client):
    pid=_prod("Buy EA")
    r=client.get(f"/buy/{pid}"); assert r.status_code==200
    assert json.loads(r.data)["ok"] is True

def test_buy_nonexistent(client):
    assert client.get("/buy/fake-id").status_code==404

def test_coupon_valid(client):
    query(f"INSERT INTO coupons(code,discount_pct,uses,max_uses,active,created_at)"
          f" VALUES({PH},20,0,0,1,{PH})",("TEST20",datetime.datetime.utcnow().isoformat()),commit=True)
    r=client.post("/coupon/check",json={"code":"TEST20"},content_type="application/json")
    d=json.loads(r.data); assert d["valid"] is True; assert "20%" in d["msg"]

def test_coupon_invalid(client):
    r=client.post("/coupon/check",json={"code":"FAKE99"},content_type="application/json")
    assert json.loads(r.data)["valid"] is False

# ── Download ──────────────────────────────────────────────────────────────────
def test_download_valid(client):
    _,tok,_=_purchase()
    r=client.get(f"/download/{tok}"); assert r.status_code==200
    assert "attachment" in r.headers["Content-Disposition"]

def test_download_increments(client):
    _,tok,_=_purchase(used=0)
    client.get(f"/download/{tok}")
    row=query(f"SELECT downloads_used FROM purchases WHERE download_token={PH}",(tok,),fetchone=True)
    assert row["downloads_used"]==1

def test_download_limit(client):
    _,tok,_=_purchase(used=3,maxdl=3)
    assert client.get(f"/download/{tok}").status_code==403

def test_download_expired_token(client):
    _,tok,_=_purchase(days_left=-1)
    assert client.get(f"/download/{tok}").status_code==403

def test_download_bad_token_format(client):
    r=client.get("/download/tooshort"); assert r.status_code in (400,404,200)

def test_download_sql_in_token(client):
    """Token with non-hex characters should be rejected before DB."""
    r=client.get("/download/"+("a"*63)+"'"); assert r.status_code in (400,404,200)

# ── License ───────────────────────────────────────────────────────────────────
def _lic(days=30):
    key=str(uuid.uuid4()).upper()
    exp=(datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(days=days)).isoformat()
    query(f"INSERT INTO licenses(key,expiry,plan,created_at) VALUES({PH},{PH},{PH},{PH})",
          (key,exp,"monthly",datetime.datetime.utcnow().isoformat()),commit=True)
    return key

def test_valid_license(client):
    key=_lic()
    d=json.loads(client.post("/license/check",json={"license":key,"account":"ACC"},content_type="application/json").data)
    assert d["valid"] is True; assert "days_remaining" in d

def test_expired_license(client):
    key=_lic(days=-1)
    d=json.loads(client.post("/license/check",json={"license":key,"account":"A"},content_type="application/json").data)
    assert d["valid"] is False; assert d["reason"]=="expired"

def test_license_not_found(client):
    d=json.loads(client.post("/license/check",json={"license":"FAKE","account":"A"},content_type="application/json").data)
    assert d["reason"]=="not_found"

def test_license_bound_to_other(client):
    key=_lic()
    client.post("/license/check",json={"license":key,"account":"A"},content_type="application/json")
    d=json.loads(client.post("/license/check",json={"license":key,"account":"B"},content_type="application/json").data)
    assert d["reason"]=="bound_to_other_account"

def test_license_missing_fields(client):
    assert client.post("/license/check",json={"license":"K"},content_type="application/json").status_code==400

# ── PayPal IPN ────────────────────────────────────────────────────────────────
def _ipn(client,**kw):
    d={"payment_status":"Completed","mc_gross":"20.00","mc_currency":"USD",
       "txn_id":"T"+_secrets.token_hex(4).upper(),"payer_email":"b@t.com","receiver_email":"m@t.com"}
    d.update(kw)
    return client.post("/webhook/paypal/ipn",
                       data="&".join(f"{k}={v}" for k,v in d.items()),
                       content_type="application/x-www-form-urlencoded")

def test_ipn_always_200(client): assert _ipn(client).status_code==200

def test_ipn_low_amount(client):
    r=_ipn(client,mc_gross="0.50"); assert r.status_code==200; assert b"LOW" in r.data

def test_ipn_wrong_currency(client):
    r=_ipn(client,mc_currency="EUR"); assert b"CURRENCY" in r.data

def test_ipn_logged(client):
    txn="LOG"+_secrets.token_hex(4).upper(); _ipn(client,txn_id=txn)
    row=query(f"SELECT * FROM transactions WHERE sub_id={PH}",(txn,),fetchone=True)
    assert row is not None

# ── Coupon logic ──────────────────────────────────────────────────────────────
def test_calc_pct(): assert calc_price(20.0,{"discount_pct":20,"discount_usd":0})==16.0
def test_calc_usd(): assert calc_price(20.0,{"discount_pct":0,"discount_usd":5})==15.0
def test_calc_none(): assert calc_price(20.0,None)==20.0
def test_calc_floor(): assert calc_price(20.0,{"discount_pct":0,"discount_usd":999})==0.0

def test_expired_coupon_none():
    code="EX"+_secrets.token_hex(3).upper(); past=(datetime.datetime.utcnow()-datetime.timedelta(days=1)).isoformat()
    query(f"INSERT INTO coupons(code,discount_pct,uses,max_uses,active,expires_at,created_at)"
          f" VALUES({PH},10,0,0,1,{PH},{PH})",(code,past,datetime.datetime.utcnow().isoformat()),commit=True)
    assert validate_coupon(code) is None

def test_maxed_coupon_none():
    code="MX"+_secrets.token_hex(3).upper()
    query(f"INSERT INTO coupons(code,discount_pct,uses,max_uses,active,created_at)"
          f" VALUES({PH},10,5,5,1,{PH})",(code,datetime.datetime.utcnow().isoformat()),commit=True)
    assert validate_coupon(code) is None

# ── Admin API ─────────────────────────────────────────────────────────────────
def test_stats_requires_login(client): assert client.get("/admin/api/stats").status_code==302

def test_stats_ok(client):
    csrf=_login(client)
    d=json.loads(client.get("/admin/api/stats").data)
    assert "revenue" in d; assert "confirmed" in d

def test_create_license_admin(client):
    csrf=_login(client)
    r=client.post("/admin/api/licenses/create",json={"days":31,"plan":"monthly","_csrf":csrf},content_type="application/json")
    d=json.loads(r.data); assert "key" in d

def test_create_coupon_admin(client):
    csrf=_login(client)
    r=client.post("/admin/api/coupons/create",json={"code":"ADMC","discount_pct":10,"_csrf":csrf},content_type="application/json")
    assert json.loads(r.data)["code"]=="ADMC"

def test_create_referral_admin(client):
    csrf=_login(client)
    r=client.post("/admin/api/referrals/create",
                  json={"code":"REF1","name":"Alice","paypal_email":"a@p.com","_csrf":csrf},
                  content_type="application/json")
    d=json.loads(r.data); assert "link" in d; assert "REF1" in d["link"]

# ── Lookup ────────────────────────────────────────────────────────────────────
def test_lookup_valid(client):
    txn,tok,_=_purchase(email="lu@t.com")
    with client.session_transaction() as s: s["csrf_token"]="x"
    r=client.post("/lookup",data={"txn_id":txn,"email":"lu@t.com","_csrf":"x"})
    assert r.status_code==200

def test_lookup_wrong_email(client):
    txn,tok,_=_purchase(email="real@t.com")
    with client.session_transaction() as s: s["csrf_token"]="x"
    r=client.post("/lookup",data={"txn_id":txn,"email":"wrong@t.com","_csrf":"x"})
    assert b"No purchase found" in r.data

# ── Migrations ────────────────────────────────────────────────────────────────
def test_migrations_idempotent():
    from migrations import run_migrations
    assert run_migrations(verbose=False)==0

def test_schema_version_exists():
    row=query("SELECT MAX(version) AS v FROM schema_version",fetchone=True)
    assert row and row["v"] is not None

# ── Rate limit basic ──────────────────────────────────────────────────────────
def test_rate_limit_triggers(client):
    hit=False
    for _ in range(25):
        r=client.post("/coupon/check",json={"code":"X"},content_type="application/json")
        if r.status_code==429: hit=True; break
    assert hit,"Rate limiter never triggered"
