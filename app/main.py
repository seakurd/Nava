"""
سرور ناوا — مغز برنامه
- تنظیمات راه‌دور، تبلیغ‌های داخلی، دعوت دوستان، اشتراک «ناوا پرو» (کد + درگاه زرین‌پال)،
  صدای ابری (edge-tts)، پاسخ‌های سفارشی، آمار، و پنل مدیریت محافظت‌شده با رمز.
اجرا:  uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
import hashlib, json, os, secrets, sqlite3, string, time, threading
from contextlib import contextmanager
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

DATA = Path(os.environ.get("NAVA_DATA", "/data"))
DATA.mkdir(parents=True, exist_ok=True)
DB = DATA / "nava.db"
from app import persist
persist.restore(DB)
TTS_CACHE = DATA / "tts"; TTS_CACHE.mkdir(exist_ok=True)
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
ZARINPAL_MERCHANT = os.environ.get("ZARINPAL_MERCHANT", "")
ZARINPAL_SANDBOX = os.environ.get("ZARINPAL_SANDBOX", "0") == "1"
ZP = "https://sandbox.zarinpal.com" if ZARINPAL_SANDBOX else "https://payment.zarinpal.com"
DAY = 86400_000  # میلی‌ثانیه

VOICES = {"darya": "fa-IR-DilaraNeural", "payam": "fa-IR-FaridNeural"}

DEFAULT_CONFIG = {
    "ads_enabled": True, "ad_every_n": 1, "ad_skip_seconds": 5,
    "referral_days": 3, "pro_invites": 5,
    "payment_enabled": False,
    "card_number": "", "card_holder": "پیام باب", "sheba": "IR580560611828006998292102",   # پرداخت کارت‌به‌کارت (تأیید دستی در پنل)
    "ai_endpoint": "",                       # خالی = مغز پیش‌فرض داخل اپ
    # کلید هوش مصنوعی سرور (فقط روی سرور می‌ماند و هرگز به اپ فرستاده نمی‌شود)
    "ai_provider": "free",                   # free | openai | gemini | custom
    "ai_key": "", "ai_model": "", "ai_base_url": "",
    "ai_pro_only": False,                    # true = مغز سریع سرور فقط برای کاربران پرو
    "free_images_per_day": 10,               # سقف تصویرسازی رایگان روزانه در اپ (پرو نامحدود)
    "personas": [],                          # دستیارهای تخصصی اضافه: [{"id","title","emoji","prompt","pro"}]
    "plans": [
        {"id": "monthly", "title": "یک‌ماهه", "price": 49000, "days": 30, "badge": ""},
        {"id": "quarterly", "title": "سه‌ماهه", "price": 129000, "days": 90, "badge": "۱۲٪ تخفیف"},
        {"id": "yearly", "title": "یک‌ساله", "price": 390000, "days": 365, "badge": "پرطرفدار · ۳۳٪ تخفیف"},
    ],
    "features": {"answers": True, "cloud_tts": True},
    "announcement": {"active": False, "id": "", "title": "", "text": "", "url": ""},
    "update": {"latest_version": 0, "min_version": 0, "url": "", "message": ""},
    "welcome": "", "download_url": "", "support_url": "",
    "jokes": [],
    "custom_commands": [],
    "answers": [],  # [{"q": ["ساعت کاری"], "title": "...", "text": "...", "url": ""}]
}

app = FastAPI(title="Nava Server", docs_url=None, redoc_url=None)
_lock = threading.Lock()


# ------------------------------------------------------------------ پایگاه داده
@contextmanager
def db():
    with _lock:
        c = sqlite3.connect(DB)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()


def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS devices (id TEXT PRIMARY KEY, code TEXT UNIQUE, invited INT DEFAULT 0,
            ad_free_until INT DEFAULT 0, pro_until INT DEFAULT 0, pro_plan TEXT DEFAULT '', referred_by TEXT,
            version INT DEFAULT 0, first_seen INT, last_seen INT);
        CREATE TABLE IF NOT EXISTS ads (id TEXT PRIMARY KEY, title TEXT, body TEXT, cta TEXT, image_url TEXT,
            video_url TEXT, click_url TEXT, duration INT DEFAULT 5, weight INT DEFAULT 1, active INT DEFAULT 1,
            advertiser TEXT DEFAULT '', budget_impressions INT DEFAULT 0, created INT);
        CREATE TABLE IF NOT EXISTS ad_events (ad TEXT, device TEXT, event TEXT, ts INT);
        CREATE TABLE IF NOT EXISTS events (device TEXT, type TEXT, kind TEXT, ts INT);
        CREATE TABLE IF NOT EXISTS codes (code TEXT PRIMARY KEY, days INT, plan TEXT, used_by TEXT, used_at INT, note TEXT, created INT);
        CREATE TABLE IF NOT EXISTS payments (authority TEXT PRIMARY KEY, device TEXT, plan TEXT, amount INT,
            status TEXT, ref_id TEXT, created INT, verified INT);
        CREATE TABLE IF NOT EXISTS claims_ip (ip TEXT, ts INT);
        CREATE TABLE IF NOT EXISTS manual_pay (id INTEGER PRIMARY KEY AUTOINCREMENT, device TEXT, plan TEXT, amount INT,
            last4 TEXT, ref TEXT, status TEXT, created INT, decided INT);
        """)


init_db()
persist.start(DB, _lock)
now_ms = lambda: int(time.time() * 1000)


def get_config() -> dict:
    with db() as c:
        r = c.execute("SELECT v FROM kv WHERE k='config'").fetchone()
    cfg = dict(DEFAULT_CONFIG)
    if r:
        raw = json.loads(r["v"])
        if _rescue_keys(raw):
            save_config(raw)
        cfg.update(raw)
    cfg["payment_enabled"] = bool(cfg.get("payment_enabled")) and bool(ZARINPAL_MERCHANT)
    return cfg


def save_config(cfg: dict):
    with db() as c:
        c.execute("INSERT OR REPLACE INTO kv VALUES('config', ?)", (json.dumps(cfg, ensure_ascii=False),))


def touch_device(c, device: str, code: str = "", version: int = 0):
    if not device or len(device) > 64:
        raise HTTPException(400, "bad device")
    r = c.execute("SELECT * FROM devices WHERE id=?", (device,)).fetchone()
    t = now_ms()
    if not r:
        c.execute("INSERT INTO devices(id, code, first_seen, last_seen, version) VALUES(?,?,?,?,?)",
                  (device, code or None, t, t, version))
    else:
        c.execute("UPDATE devices SET last_seen=?, version=MAX(version, ?) WHERE id=?", (t, version, device))
        if code and not r["code"]:
            try:
                c.execute("UPDATE devices SET code=? WHERE id=?", (code, device))
            except sqlite3.IntegrityError:
                pass
    return c.execute("SELECT * FROM devices WHERE id=?", (device,)).fetchone()


def me_json(r) -> dict:
    return {"invited": r["invited"], "ad_free_until": r["ad_free_until"], "pro_until": r["pro_until"],
            "pro_plan": r["pro_plan"] or "", "code": r["code"] or "", "referred_by": bool(r["referred_by"])}



import re as _re
_SECRET_RE = _re.compile(r"(sk-[A-Za-z0-9_\-]{16,}|AIza[0-9A-Za-z_\-]{20,})")


def _looks_secret(v: str) -> bool:
    return bool(_SECRET_RE.search(v or ""))


def _rescue_keys(cfg: dict) -> bool:
    """کلیدی که اشتباهی در فیلد ai_endpoint گذاشته شده را به ai_key منتقل می‌کند."""
    ep = str(cfg.get("ai_endpoint") or "")
    m = _SECRET_RE.search(ep)
    if not m:
        return False
    if not (cfg.get("ai_key") or "").strip():
        cfg["ai_key"] = m.group(1)
        if m.group(1).startswith("AIza"):
            cfg["ai_provider"] = "gemini"
        elif not cfg.get("ai_provider") or cfg.get("ai_provider") == "free":
            cfg["ai_provider"] = "openai"
    cfg["ai_endpoint"] = ""
    return True

# ------------------------------------------------------------------ API برنامه
@app.get("/api/config")
def api_config(device: str = "", v: int = 0):
    if device:
        with db() as c:
            touch_device(c, device, version=v)
    cfg = get_config()
    cfg.pop("answers", None)
    for k in ("ai_key", "ai_base_url", "ai_model", "ai_provider"):
        cfg.pop(k, None)
    ep = str(cfg.get("ai_endpoint") or "").strip()
    if not ep.lower().startswith(("http://", "https://")) or _looks_secret(ep):
        cfg["ai_endpoint"] = ""
    return cfg


@app.post("/api/me")
async def api_me(req: Request):
    j = await req.json()
    with db() as c:
        r = touch_device(c, str(j.get("device", "")), str(j.get("code", ""))[:12].upper())
        return me_json(r)


@app.post("/api/referral/claim")
async def api_claim(req: Request):
    j = await req.json()
    device = str(j.get("device", "")); code = str(j.get("code", "")).strip().upper()
    ip = req.client.host if req.client else ""
    cfg = get_config(); days = int(cfg.get("referral_days", 3))
    with db() as c:
        me = touch_device(c, device)
        if me["referred_by"]:
            return {"ok": False, "message": "شما قبلاً یک کد دعوت ثبت کرده‌اید."}
        inviter = c.execute("SELECT * FROM devices WHERE code=?", (code,)).fetchone()
        if not inviter:
            return {"ok": False, "message": "این کد دعوت پیدا نشد."}
        if inviter["id"] == device:
            return {"ok": False, "message": "کد خودتان را نمی‌شود ثبت کرد."}
        day_ago = now_ms() - DAY
        if c.execute("SELECT COUNT(*) n FROM claims_ip WHERE ip=? AND ts>?", (ip, day_ago)).fetchone()["n"] >= 3:
            return {"ok": False, "message": "امروز از این شبکه کد زیادی ثبت شده؛ فردا دوباره امتحان کنید."}
        t = now_ms(); bonus = days * DAY
        c.execute("UPDATE devices SET referred_by=?, ad_free_until=MAX(ad_free_until, ?)+? WHERE id=?",
                  (inviter["id"], t, bonus, device))
        c.execute("UPDATE devices SET invited=invited+1, ad_free_until=MAX(ad_free_until, ?)+? WHERE id=?",
                  (t, bonus, inviter["id"]))
        c.execute("INSERT INTO claims_ip VALUES(?,?)", (ip, t))
        me = c.execute("SELECT * FROM devices WHERE id=?", (device,)).fetchone()
        return {"ok": True, "message": f"ثبت شد! {days} روز بدون تبلیغ هدیه گرفتید.", **me_json(me)}


@app.post("/api/pro/activate")
async def api_activate(req: Request):
    j = await req.json()
    device = str(j.get("device", "")); code = str(j.get("code", "")).strip().upper()
    with db() as c:
        touch_device(c, device)
        r = c.execute("SELECT * FROM codes WHERE code=?", (code,)).fetchone()
        if not r:
            return {"ok": False, "message": "کد معتبر نیست."}
        if r["used_by"] and r["used_by"] != device:
            return {"ok": False, "message": "این کد قبلاً استفاده شده است."}
        if r["used_by"] == device:
            me = c.execute("SELECT * FROM devices WHERE id=?", (device,)).fetchone()
            return {"ok": True, "message": "این کد قبلاً برای همین گوشی فعال شده.", **me_json(me)}
        extend_pro(c, device, int(r["days"]), r["plan"] or "code")
        c.execute("UPDATE codes SET used_by=?, used_at=? WHERE code=?", (device, now_ms(), code))
        me = c.execute("SELECT * FROM devices WHERE id=?", (device,)).fetchone()
        return {"ok": True, "message": f"ناوا پرو برای {r['days']} روز فعال شد!", **me_json(me)}


def extend_pro(c, device: str, days: int, plan: str):
    c.execute("UPDATE devices SET pro_until=MAX(pro_until, ?)+?, pro_plan=? WHERE id=?",
              (now_ms(), days * DAY, plan, device))


@app.post("/api/pay/manual")
async def api_manual_pay(req: Request):
    j = await req.json()
    device = str(j.get("device", ""))[:64]; plan = str(j.get("plan", ""))
    ref = "".join(ch for ch in str(j.get("ref", "")) if ch.isalnum())[:40]
    last4 = "".join(ch for ch in str(j.get("last4", "")) if ch.isdigit())[-4:]
    p = _plan(plan)
    if not device or not p or len(ref) < 4:
        return {"ok": False, "message": "اطلاعات پرداخت کامل نیست."}
    with db() as c:
        touch_device(c, device)
        if c.execute("SELECT COUNT(*) n FROM manual_pay WHERE device=? AND status='pending'", (device,)).fetchone()["n"] >= 3:
            return {"ok": False, "message": "پرداخت‌های قبلی شما در حال بررسی است."}
        if c.execute("SELECT 1 FROM manual_pay WHERE ref=?", (ref,)).fetchone():
            return {"ok": False, "message": "این کد پیگیری قبلاً ثبت شده است."}
        c.execute("INSERT INTO manual_pay(device,plan,amount,last4,ref,status,created,decided) VALUES(?,?,?,?,?,?,?,0)",
                  (device, plan, int(p["price"]), last4, ref, "pending", now_ms()))
    return {"ok": True, "message": "پرداخت ثبت شد؛ پس از تأیید، ناوا پرو خودکار فعال می‌شود."}


AI_PRESETS = {
    "openai": ("https://api.openai.com/v1", "gpt-6-luna"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "gemini-3.5-flash-lite"),
    "openrouter": ("https://openrouter.ai/api/v1", "openai/gpt-6-luna"),
    "groq": ("https://api.groq.com/openai/v1", "llama-3.3-70b-versatile"),
    "deepseek": ("https://api.deepseek.com/v1", "deepseek-chat"),
}
POLLINATIONS = "https://text.pollinations.ai/openai"
_ai_client = httpx.AsyncClient(timeout=httpx.Timeout(40, connect=6),
                               limits=httpx.Limits(max_keepalive_connections=20, keepalive_expiry=120))
ALLOWED_KEYS = ("messages", "temperature", "max_tokens", "stream", "tools", "tool_choice", "top_p", "response_format")


def ai_target(cfg: dict):
    """(url, headers, model) برای کلید سرور، یا None اگر کلیدی تنظیم نشده."""
    key = (cfg.get("ai_key") or "").strip()
    prov = cfg.get("ai_provider") or "free"
    if not key or prov == "free":
        return None
    base, model = AI_PRESETS.get(prov, ("", ""))
    base = (cfg.get("ai_base_url") or base or "").strip().rstrip("/")
    model = (cfg.get("ai_model") or model or "").strip()
    if not base or not model:
        return None
    return f"{base}/chat/completions", {"Authorization": f"Bearer {key}"}, model


async def _proxy(url: str, headers: dict, body: dict):
    """درخواست را می‌فرستد؛ اگر stream بود جواب را تکه‌تکه رد می‌کند. خطا قبل از شروع = None."""
    if body.get("stream"):
        req = _ai_client.build_request("POST", url, json=body, headers=headers)
        r = await _ai_client.send(req, stream=True)
        if r.status_code != 200:
            await r.aclose()
            return None

        async def gen():
            try:
                async for chunk in r.aiter_raw():
                    yield chunk
            finally:
                await r.aclose()
        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    r = await _ai_client.post(url, json=body, headers=headers)
    if r.status_code != 200:
        return None
    return Response(content=r.content, status_code=200, media_type="application/json")


def _is_pro(device: str) -> bool:
    if not device:
        return False
    with db() as c:
        r = c.execute("SELECT pro_until FROM devices WHERE id=?", (device[:64],)).fetchone()
    return bool(r and r["pro_until"] > now_ms())


@app.post("/api/ai")
async def api_ai(request: Request, device: str = ""):
    """پل هوش مصنوعی برای اپ. اگر مدیر کلید گذاشته باشد از آن (سریع)، وگرنه Pollinations رایگان.
    اگر ai_pro_only روشن باشد، کلید سرور فقط برای دستگاه‌های پرو استفاده می‌شود."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "bad json")
    cfg = get_config()
    t = ai_target(cfg)
    if t and cfg.get("ai_pro_only") and not _is_pro(device):
        t = None
    if t:
        url, headers, model = t
        clean = {k: body[k] for k in ALLOWED_KEYS if k in body}
        clean["model"] = model
        variants = [clean]
        if "api.openai.com" in url:
            # مدل‌های جدید OpenAI: max_completion_tokens و بدون temperature
            v = dict(clean); mt = v.pop("max_tokens", None); v.pop("temperature", None)
            if mt:
                v["max_completion_tokens"] = mt
            variants = [v]
        minimal = {k: clean[k] for k in ("model", "messages", "stream", "tools", "tool_choice") if k in clean}
        variants.append(minimal)
        for v in variants:
            try:
                res = await _proxy(url, headers, v)
                if res is not None:
                    return res
            except Exception:
                pass
    body["private"] = True
    try:
        res = await _proxy(POLLINATIONS, {}, body)
        if res is not None:
            return res
    except Exception:
        pass
    raise HTTPException(502, "ai unavailable")


@app.get("/api/ads/next")
def api_next_ad(device: str = ""):
    cfg = get_config()
    if not cfg.get("ads_enabled", True):
        return {"ad": None}
    with db() as c:
        d = c.execute("SELECT * FROM devices WHERE id=?", (device,)).fetchone()
        if d and (d["pro_until"] > now_ms() or d["ad_free_until"] > now_ms()):
            return {"ad": None}
        rows = c.execute("""SELECT a.*, (SELECT COUNT(*) FROM ad_events e WHERE e.ad=a.id AND e.event='impression') imp
                            FROM ads a WHERE active=1""").fetchall()
    rows = [r for r in rows if not r["budget_impressions"] or r["imp"] < r["budget_impressions"]]
    if not rows:
        return {"ad": None}
    import random
    r = random.choices(rows, weights=[max(1, r["weight"]) for r in rows])[0]
    return {"ad": {k: r[k] for k in ("id", "title", "body", "cta", "image_url", "video_url", "click_url", "duration")}}


@app.post("/api/ads/{ad_id}/{event}")
async def api_ad_event(ad_id: str, event: str, req: Request):
    if event not in ("impression", "click", "complete"):
        raise HTTPException(404)
    j = await req.json()
    with db() as c:
        c.execute("INSERT INTO ad_events VALUES(?,?,?,?)", (ad_id, str(j.get("device", ""))[:64], event, now_ms()))
    return {"ok": True}


@app.post("/api/events")
async def api_events(req: Request):
    j = await req.json()
    with db() as c:
        c.execute("INSERT INTO events VALUES(?,?,?,?)",
                  (str(j.get("device", ""))[:64], str(j.get("type", ""))[:32], str(j.get("kind", ""))[:64], now_ms()))
    return {"ok": True}


def _norm(s: str) -> str:
    return (s or "").replace("ي", "ی").replace("ك", "ک").replace("‌", " ").strip().lower()


@app.get("/api/ask")
def api_ask(q: str = ""):
    """پاسخ‌های سفارشی از پنل؛ اگر نبود 404 تا برنامه خودش از ویکی‌پدیا و Open-Meteo بپرسد."""
    nq = _norm(q)
    for a in get_config().get("answers", []):
        if any(_norm(p) and _norm(p) in nq for p in a.get("q", [])):
            return {"title": a.get("title", ""), "text": a.get("text", ""), "url": a.get("url", ""), "source": "ناوا"}
    raise HTTPException(404)


@app.get("/api/tts")
async def api_tts(text: str, voice: str = "darya", rate: int = 0):
    if not get_config().get("features", {}).get("cloud_tts", True):
        raise HTTPException(404)
    text = text.strip()[:600]
    if not text:
        raise HTTPException(400)
    v = VOICES.get(voice, VOICES["darya"]); rate = max(-50, min(80, rate))
    key = hashlib.md5(f"{v}|{rate}|{text}".encode()).hexdigest()
    f = TTS_CACHE / f"{key}.mp3"
    if not f.exists():
        import edge_tts
        tmp = f.with_suffix(".part")
        await edge_tts.Communicate(text, v, rate=f"{rate:+d}%").save(str(tmp))
        tmp.rename(f)
    return FileResponse(f, media_type="audio/mpeg", headers={"Cache-Control": "public, max-age=604800"})


# ------------------------------------------------------------------ پرداخت (زرین‌پال)
def _plan(pid: str):
    return next((p for p in get_config()["plans"] if p["id"] == pid), None)


def _page(title: str, msg: str, ok: bool) -> HTMLResponse:
    color = "#16a34a" if ok else "#dc2626"
    return HTMLResponse(f"""<!doctype html><html lang=fa dir=rtl><meta name=viewport content="width=device-width,initial-scale=1">
<title>{title}</title><body style="font-family:Vazirmatn,Tahoma,sans-serif;background:linear-gradient(135deg,#7c3aed,#4f46e5,#06b6d4);min-height:100vh;margin:0;display:flex;align-items:center;justify-content:center">
<div style="background:#fff;border-radius:24px;padding:32px;max-width:360px;text-align:center"><h2 style="color:{color}">{title}</h2><p>{msg}</p>
<p style="color:#64748b">به برنامه‌ی ناوا برگردید.</p></div></body></html>""")


@app.get("/pay/start")
async def pay_start(device: str, plan: str):
    cfg = get_config(); p = _plan(plan)
    if not cfg["payment_enabled"] or not p:
        return _page("پرداخت فعال نیست", "پرداخت اینترنتی هنوز روشن نشده است.", False)
    with db() as c:
        touch_device(c, device)
    amount_rial = int(p["price"]) * 10
    async with httpx.AsyncClient(timeout=15) as h:
        r = await h.post(f"{ZP}/pg/v4/payment/request.json", json={
            "merchant_id": ZARINPAL_MERCHANT, "amount": amount_rial, "currency": "IRR",
            "callback_url": f"{PUBLIC_URL}/pay/callback", "description": f"اشتراک ناوا پرو - {p['title']}"})
    data = (r.json() or {}).get("data") or {}
    if data.get("code") != 100:
        return _page("خطا در اتصال به درگاه", "لطفاً کمی بعد دوباره امتحان کنید.", False)
    auth = data["authority"]
    with db() as c:
        c.execute("INSERT INTO payments VALUES(?,?,?,?,?,?,?,?)", (auth, device, plan, amount_rial, "pending", "", now_ms(), 0))
    return RedirectResponse(f"{ZP}/pg/StartPay/{auth}")


@app.get("/pay/callback")
async def pay_callback(Authority: str = "", Status: str = ""):
    with db() as c:
        pay = c.execute("SELECT * FROM payments WHERE authority=?", (Authority,)).fetchone()
    if not pay:
        return _page("پرداخت پیدا نشد", "", False)
    if pay["status"] == "paid":
        return _page("پرداخت موفق", "اشتراک شما فعال است.", True)
    if Status != "OK":
        with db() as c:
            c.execute("UPDATE payments SET status='canceled' WHERE authority=?", (Authority,))
        return _page("پرداخت انجام نشد", "مبلغی کسر نشده یا تا ۷۲ ساعت برمی‌گردد.", False)
    async with httpx.AsyncClient(timeout=15) as h:
        r = await h.post(f"{ZP}/pg/v4/payment/verify.json", json={
            "merchant_id": ZARINPAL_MERCHANT, "amount": pay["amount"], "authority": Authority})
    data = (r.json() or {}).get("data") or {}
    if data.get("code") in (100, 101):
        p = _plan(pay["plan"]) or {"days": 30}
        with db() as c:
            # فقط یک بار اعمال شود
            cur = c.execute("UPDATE payments SET status='paid', ref_id=?, verified=? WHERE authority=? AND status!='paid'",
                            (str(data.get("ref_id", "")), now_ms(), Authority))
            if cur.rowcount:
                extend_pro(c, pay["device"], int(p["days"]), pay["plan"])
        return _page("پرداخت موفق ✓", f"ناوا پرو فعال شد. کد پیگیری: {data.get('ref_id', '')}", True)
    with db() as c:
        c.execute("UPDATE payments SET status='failed' WHERE authority=?", (Authority,))
    return _page("تأیید پرداخت ناموفق", "اگر مبلغ کسر شده، تا ۷۲ ساعت برمی‌گردد.", False)


# ------------------------------------------------------------------ پنل مدیریت
security = HTTPBasic()


def admin(cred: HTTPBasicCredentials = Depends(security)):
    if not ADMIN_PASSWORD:
        raise HTTPException(503, "ADMIN_PASSWORD تنظیم نشده است.")
    ok = secrets.compare_digest(cred.username, ADMIN_USER) and secrets.compare_digest(cred.password, ADMIN_PASSWORD)
    if not ok:
        raise HTTPException(401, headers={"WWW-Authenticate": "Basic"})
    return True


@app.get("/admin", response_class=HTMLResponse)
def admin_page(_=Depends(admin)):
    return FileResponse(Path(__file__).parent / "admin.html")


@app.get("/admin/api/config")
def admin_get_config(_=Depends(admin)):
    cfg = get_config()
    key = cfg.get("ai_key") or ""
    cfg["ai_key"] = ""
    return cfg | {"_zarinpal_ready": bool(ZARINPAL_MERCHANT),
                  "_ai_key_hint": ("…" + key[-4:]) if key else ""}


@app.put("/admin/api/config")
async def admin_put_config(req: Request, _=Depends(admin)):
    j = await req.json()
    j.pop("_zarinpal_ready", None)
    j.pop("_ai_key_hint", None)
    # کلید خالی = همان کلید قبلی بماند؛ برای پاک کردن، "-" بفرستید
    old_key = get_config().get("ai_key", "")
    k = (j.get("ai_key") or "").strip()
    j["ai_key"] = "" if k == "-" else (k or old_key)
    _rescue_keys(j)
    save_config(j)
    return {"ok": True}


@app.post("/admin/api/ai_test")
async def admin_ai_test(_=Depends(admin)):
    """آزمایش مغز: زمان پاسخ و یک جمله‌ی نمونه."""
    cfg = get_config()
    t = ai_target(cfg)
    body = {"messages": [{"role": "user", "content": "در یک جمله‌ی کوتاه فارسی سلام کن."}], "max_tokens": 60}
    url, headers = (t[0], t[1]) if t else (POLLINATIONS, {})
    body["model"] = t[2] if t else "openai-fast"
    start = time.time()
    try:
        r = await _ai_client.post(url, json=body, headers=headers)
        ms = int((time.time() - start) * 1000)
        if r.status_code != 200:
            return {"ok": False, "ms": ms, "provider": cfg.get("ai_provider"), "error": r.text[:300]}
        text = r.json()["choices"][0]["message"]["content"]
        return {"ok": True, "ms": ms, "provider": cfg.get("ai_provider") if t else "free", "text": text[:200]}
    except Exception as e:
        return {"ok": False, "ms": int((time.time() - start) * 1000), "error": str(e)[:300]}


@app.get("/admin/api/ads")
def admin_ads(_=Depends(admin)):
    with db() as c:
        rows = c.execute("""SELECT a.*,
            (SELECT COUNT(*) FROM ad_events e WHERE e.ad=a.id AND e.event='impression') impressions,
            (SELECT COUNT(*) FROM ad_events e WHERE e.ad=a.id AND e.event='click') clicks,
            (SELECT COUNT(*) FROM ad_events e WHERE e.ad=a.id AND e.event='complete') completes
            FROM ads a ORDER BY created DESC""").fetchall()
    return [dict(r) for r in rows]


@app.post("/admin/api/ads")
async def admin_save_ad(req: Request, _=Depends(admin)):
    j = await req.json()
    aid = j.get("id") or secrets.token_hex(6)
    with db() as c:
        c.execute("""INSERT OR REPLACE INTO ads(id,title,body,cta,image_url,video_url,click_url,duration,weight,active,advertiser,budget_impressions,created)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?,COALESCE((SELECT created FROM ads WHERE id=?),?))""",
                  (aid, j.get("title", ""), j.get("body", ""), j.get("cta", ""), j.get("image_url", ""), j.get("video_url", ""),
                   j.get("click_url", ""), int(j.get("duration") or 5), int(j.get("weight") or 1), 1 if j.get("active", True) else 0,
                   j.get("advertiser", ""), int(j.get("budget_impressions") or 0), aid, now_ms()))
    return {"ok": True, "id": aid}


@app.delete("/admin/api/ads/{aid}")
def admin_del_ad(aid: str, _=Depends(admin)):
    with db() as c:
        c.execute("DELETE FROM ads WHERE id=?", (aid,))
    return {"ok": True}


@app.post("/admin/api/codes")
async def admin_make_codes(req: Request, _=Depends(admin)):
    j = await req.json()
    n = max(1, min(500, int(j.get("count", 1)))); days = max(1, int(j.get("days", 30)))
    alpha = string.ascii_uppercase.replace("O", "").replace("I", "") + "23456789"
    out = []
    with db() as c:
        for _ in range(n):
            code = "NAVA-" + "".join(secrets.choice(alpha) for _ in range(4)) + "-" + "".join(secrets.choice(alpha) for _ in range(4))
            c.execute("INSERT INTO codes VALUES(?,?,?,?,?,?,?)", (code, days, j.get("plan", "code"), None, None, j.get("note", ""), now_ms()))
            out.append(code)
    return {"codes": out}


@app.get("/admin/api/codes")
def admin_codes(_=Depends(admin)):
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM codes ORDER BY created DESC LIMIT 300")]


@app.post("/admin/api/devices/{device}/pro")
async def admin_grant(device: str, req: Request, _=Depends(admin)):
    j = await req.json()
    with db() as c:
        touch_device(c, device)
        extend_pro(c, device, int(j.get("days", 30)), "gift")
    return {"ok": True}


@app.get("/admin/api/devices")
def admin_devices(q: str = "", _=Depends(admin)):
    """جستجوی دستگاه با شناسه یا کد دعوت؛ برای هدیه دادن پرو از پنل/اپ ادمین."""
    with db() as c:
        if q:
            like = f"%{q.strip()}%"
            rows = c.execute("SELECT * FROM devices WHERE id LIKE ? OR code LIKE ? ORDER BY last_seen DESC LIMIT 50", (like, like.upper()))
        else:
            rows = c.execute("SELECT * FROM devices ORDER BY last_seen DESC LIMIT 50")
        return [dict(r) for r in rows]


@app.get("/admin/api/manual")
def admin_manual(_=Depends(admin)):
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM manual_pay ORDER BY (status='pending') DESC, created DESC LIMIT 200")]


@app.post("/admin/api/manual/{pid}/{decision}")
def admin_manual_decide(pid: int, decision: str, _=Depends(admin)):
    if decision not in ("approve", "reject"):
        raise HTTPException(400)
    with db() as c:
        r = c.execute("SELECT * FROM manual_pay WHERE id=?", (pid,)).fetchone()
        if not r or r["status"] != "pending":
            return {"ok": False}
        if decision == "approve":
            p = _plan(r["plan"]) or {"days": 30}
            extend_pro(c, r["device"], int(p["days"]), r["plan"])
            c.execute("INSERT OR IGNORE INTO payments VALUES(?,?,?,?,?,?,?,?)",
                      ("card-" + str(pid), r["device"], r["plan"], int(r["amount"]) * 10, "paid", r["ref"], now_ms(), now_ms()))
        c.execute("UPDATE manual_pay SET status=?, decided=? WHERE id=?", ("approved" if decision == "approve" else "rejected", now_ms(), pid))
    return {"ok": True}


@app.get("/admin/api/stats")
def admin_stats(_=Depends(admin)):
    t = now_ms()
    with db() as c:
        q = lambda s, *a: c.execute(s, a).fetchone()[0]
        return {
            "devices": q("SELECT COUNT(*) FROM devices"),
            "active_1d": q("SELECT COUNT(*) FROM devices WHERE last_seen>?", t - DAY),
            "pending_cards": q("SELECT COUNT(*) FROM manual_pay WHERE status='pending'"),
            "on_latest": q("SELECT COUNT(*) FROM devices WHERE version>=(SELECT MAX(version) FROM devices)"),
            "active_7d": q("SELECT COUNT(*) FROM devices WHERE last_seen>?", t - 7 * DAY),
            "new_7d": q("SELECT COUNT(*) FROM devices WHERE first_seen>?", t - 7 * DAY),
            "pro_active": q("SELECT COUNT(*) FROM devices WHERE pro_until>?", t),
            "referrals": q("SELECT COALESCE(SUM(invited),0) FROM devices"),
            "commands_1d": q("SELECT COUNT(*) FROM events WHERE type='command' AND ts>?", t - DAY),
            "impressions_1d": q("SELECT COUNT(*) FROM ad_events WHERE event='impression' AND ts>?", t - DAY),
            "clicks_1d": q("SELECT COUNT(*) FROM ad_events WHERE event='click' AND ts>?", t - DAY),
            "revenue_toman": q("SELECT COALESCE(SUM(amount),0)/10 FROM payments WHERE status='paid'"),
            "payments": [dict(r) for r in c.execute("SELECT * FROM payments ORDER BY created DESC LIMIT 30")],
            "top_kinds": [dict(r) for r in c.execute(
                "SELECT kind, COUNT(*) n FROM events WHERE ts>? GROUP BY kind ORDER BY n DESC LIMIT 15", (t - 7 * DAY,))],
        }


# ------------------------------------------------------------------ نسخه‌ی آیفون و وب (PWA)
from fastapi.staticfiles import StaticFiles

@app.get("/app")
def app_redirect():
    return RedirectResponse("/app/")

app.mount("/app", StaticFiles(directory=Path(__file__).parent / "web", html=True), name="web")


@app.get("/")
def root():
    return RedirectResponse("/app/")
