"""
سرور ناوا — مغز برنامه
- تنظیمات راه‌دور، تبلیغ‌های داخلی، دعوت دوستان، اشتراک «ناوا پرو» (کد + درگاه زرین‌پال)،
  صدای ابری (edge-tts)، پاسخ‌های سفارشی، آمار، و پنل مدیریت محافظت‌شده با رمز.
اجرا:  uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
import asyncio, hashlib, json, os, secrets, sqlite3, string, time, threading
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
GITHUB_CLIENT_ID = os.environ.get("GITHUB_CLIENT_ID", "").strip()
GITHUB_CLIENT_SECRET = os.environ.get("GITHUB_CLIENT_SECRET", "").strip()
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
ZARINPAL_SANDBOX = os.environ.get("ZARINPAL_SANDBOX", "0") == "1"
ZP = "https://sandbox.zarinpal.com" if ZARINPAL_SANDBOX else "https://payment.zarinpal.com"
DAY = 86400_000  # میلی‌ثانیه

VOICES = {"darya": "fa-IR-DilaraNeural", "payam": "fa-IR-FaridNeural"}

DEFAULT_CONFIG = {
    "ads_enabled": True, "ad_every_n": 1, "ad_skip_seconds": 5,
    "referral_days": 3, "pro_invites": 5,
    # ۴٫۳: هدیه‌ها (همه خودکار روی سرور)
    "signup_bonus_hours": 24,                # ثبت‌نام تازه = ۲۴ ساعت پرو (یک‌بار به ازای دستگاه و حساب)
    "referral_bonus_hours": 24,              # هر دعوت موفق = ۲۴ ساعت پرو برای هر دو نفر
    "referral_milestone": 13,                # هر ۱۳ دعوت موفق =
    "referral_milestone_days": 30,           # ۱ ماه پرو هدیه
    "referral_ip_daily": 3,                  # سقف ثبت کد دعوت از یک IP در روز
    "referral_new_device_days": 7,           # فقط دستگاه‌های تازه (ضدتقلب) می‌توانند کد دعوت ثبت کنند
    # پرداخت
    "zarinpal_merchant": "",                 # خالی = متغیر محیطی ZARINPAL_MERCHANT یا حالت آزمایشی (sandbox)
    "zarinpal_sandbox_when_empty": True,     # بدون مرچنت: درگاه آزمایشی زرین‌پال برای تست
    "card_pay_enabled": True,
    # تبلیغ
    "ad_after_hard_command": True,           # اول فرمان، بعد یک آگهی (کاربر غیرپرو)
    "tapsell": {"enabled": True, "app_key": "qjqmeaicskpngkdaamhppsogogeabglhftmagiratpbockqojqntnpfjbjjshpekhcfsno",
                "rewarded_zone": "6aefa7a0e3648228d112d959", "banner_zone": "6aefa7bc7bbc2b44c58a87b9"},
    # ورود با گوگل/گیت‌هاب: با گذاشتن شناسه‌ها روشن می‌شود
    "google_client_id": "", "github_client_id": "", "github_client_secret": "",
    "accounts_enabled": True,
    # افزونه‌ها
    "plugins_off": [],                       # شناسه‌ی افزونه‌های داخلی خاموش
    "plugins": [],                           # افزونه‌های سفارشی: [{"id","title","emoji","desc","url","method","tool":{name,description,parameters},"pro","enabled"}]
    # فکر عمیق
    "thinking_free_model": "gpt-oss:20b",    # مدل استدلالی رایگان LLM7 برای «فکر عمیق» بدون کلید
    # نسخه‌ی ادمین
    "admin_latest_name": "2.3.0", "admin_latest_code": 6,
    # ۴٫۴: تبلیغ و پاداش
    "reward_ad_enabled": True, "reward_ad_minutes": 30, "reward_ad_daily": 4,   # تماشای کامل ویدیو = دقیقه‌ی پرو
    "banner_enabled": True, "banner_pages": ["home", "chat", "tools", "settings"],
    "admob_enabled": False, "admob_app_id": "", "admob_banner": "", "admob_rewarded": "",   # نیاز: حساب AdMob + بیلد با SDK گوگل
    "adsense_enabled": False, "adsense_client": "", "adsense_slot": "",                  # سایت: نیاز به تأیید دامنه در AdSense
    "site_ad": {"enabled": True, "title": "ناوا پرو", "body": "بدون آگهی، مغز سریع و همه‌ی امکانات؛ ماهی ۱۲٬۰۰۰ تومان.",
                "image_url": "", "click_url": "https://nava-server-irc5.onrender.com/dl", "cta": "دانلود ناوا"},
    "site_url": "https://seakurd.github.io/Nava/",
    "kurdish_tts_base": "",                  # خالی = شاخه‌ی models مخزن
    "payment_enabled": True,
    "card_number": "", "card_holder": "پیام باب", "sheba": "IR580560611828006998292102",   # پرداخت کارت‌به‌کارت (تأیید دستی در پنل)
    "ai_endpoint": "",                       # خالی = مغز پیش‌فرض داخل اپ
    # کلید هوش مصنوعی سرور (فقط روی سرور می‌ماند و هرگز به اپ فرستاده نمی‌شود)
    "ai_provider": "free",                   # free | openai | gemini | custom
    "ai_key": "", "ai_model": "", "ai_base_url": "",
    "ai_pro_only": False,
    "latest_name": "4.4.0",
    "maintenance": False, "maintenance_message": "",  # حالت تعمیر: اپ پیام را نشان می‌دهد                  # نام نسخه‌ی APK روی شاخه‌ی download (برای /dl)                    # true = مغز سریع سرور فقط برای کاربران پرو
    "free_images_per_day": 10,               # سقف تصویرسازی رایگان روزانه در اپ (پرو نامحدود)
    "personas": [],                          # دستیارهای تخصصی اضافه: [{"id","title","emoji","prompt","pro"}]
    "plans": [
        {"id": "monthly", "title": "یک‌ماهه", "price": 12000, "days": 30, "badge": "پرطرفدار"},
        {"id": "quarterly", "title": "سه‌ماهه", "price": 33000, "days": 90, "badge": "۸٪ تخفیف"},
        {"id": "yearly", "title": "یک‌ساله", "price": 120000, "days": 365, "badge": "۲ ماه رایگان"},
    ],
    "features": {"answers": True, "cloud_tts": True},
    "announcement": {"active": False, "id": "", "title": "", "text": "", "url": ""},
    "update": {"latest_version": 0, "min_version": 0, "url": "", "message": ""},
    "welcome": "", "download_url": "https://nava-server-irc5.onrender.com/dl", "support_url": "",
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
        CREATE TABLE IF NOT EXISTS ai_log (ts INT, ok INT, ms INT, src TEXT);
        CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, login TEXT UNIQUE, pass_hash TEXT, salt TEXT,
            name TEXT DEFAULT '', provider TEXT DEFAULT 'password', provider_id TEXT DEFAULT '', created INT,
            pro_until INT DEFAULT 0, pro_plan TEXT DEFAULT '', bonus_given INT DEFAULT 0, last_login INT DEFAULT 0, banned INT DEFAULT 0);
        CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, user_id INT, device TEXT, created INT);
        CREATE TABLE IF NOT EXISTS login_fail (ip TEXT, ts INT);
        CREATE TABLE IF NOT EXISTS rewards (device TEXT, user_id INT, kind TEXT, hours INT, ts INT);
        CREATE TABLE IF NOT EXISTS manual_pay (id INTEGER PRIMARY KEY AUTOINCREMENT, device TEXT, plan TEXT, amount INT,
            last4 TEXT, ref TEXT, status TEXT, created INT, decided INT);
        """)


init_db()


def _migrate():
    with db() as c:
        cols = {r["name"] for r in c.execute("PRAGMA table_info(devices)")}
        for col, typ in (("user_id", "INT DEFAULT 0"), ("signup_bonus", "INT DEFAULT 0"), ("ip", "TEXT DEFAULT ''"), ("milestones", "INT DEFAULT 0")):
            if col not in cols:
                c.execute(f"ALTER TABLE devices ADD COLUMN {col} {typ}")


_migrate()
START_TIME = time.time()
LATEST_DEFAULT = "4.4.0"
persist.start(DB, _lock)
now_ms = lambda: int(time.time() * 1000)


def get_config() -> dict:
    with db() as c:
        r = c.execute("SELECT v FROM kv WHERE k='config'").fetchone()
    cfg = dict(DEFAULT_CONFIG)
    if r:
        raw = json.loads(r["v"])
        changed = _rescue_keys(raw)
        if not raw.get("_dl_v2"):
            # یک‌بار: لینک دانلود از طریق شمارنده‌ی /dl
            base = PUBLIC_URL or "https://nava-server-irc5.onrender.com"
            raw["download_url"] = base + "/dl"
            raw.setdefault("latest_name", LATEST_DEFAULT)
            raw["_dl_v2"] = True
            changed = True
        if not raw.get("_v43"):
            # یک‌بار: قیمت‌گذاری و هدیه‌های ۴٫۳، نسخه‌ی تازه
            for k in ("plans", "signup_bonus_hours", "referral_bonus_hours", "referral_milestone", "referral_milestone_days", "tapsell"):
                raw[k] = DEFAULT_CONFIG[k]
            raw["latest_name"] = LATEST_DEFAULT
            raw["admin_latest_name"] = DEFAULT_CONFIG["admin_latest_name"]; raw["admin_latest_code"] = DEFAULT_CONFIG["admin_latest_code"]
            up = dict(raw.get("update") or {})
            up["latest_version"] = max(int(up.get("latest_version") or 0), 11)
            up.setdefault("message", "")
            if not up["message"]:
                up["message"] = "ناوا ۴٫۳ آمد: حساب کاربری، افزونه‌ها، فکر عمیق، لایو پایدار و پرو ارزان‌تر."
            raw["update"] = up
            raw["payment_enabled"] = True   # درگاه (آزمایشی تا وقتی مرچنت گذاشته شود)
            raw["_v43"] = True
            changed = True
        if not raw.get("_v44"):
            raw["latest_name"] = LATEST_DEFAULT
            raw["admin_latest_name"] = DEFAULT_CONFIG["admin_latest_name"]; raw["admin_latest_code"] = DEFAULT_CONFIG["admin_latest_code"]
            up = dict(raw.get("update") or {})
            up["latest_version"] = max(int(up.get("latest_version") or 0), 12)
            up["message"] = "ناوا ۴٫۴ آمد: سورانی و کرمانجی، صدای کردی، بیدارباش هوشمند بدون بوق، پاداش پرو با تماشای آگهی."
            raw["update"] = up
            raw["_v44"] = True
            changed = True
        if changed:
            save_config(raw)
        cfg.update(raw)
    cfg["payment_enabled"] = bool(cfg.get("payment_enabled")) and bool(zp_merchant(cfg)[0])
    # شناسه‌های ورود: متغیر محیطی Render بر پنل مقدم است
    if GITHUB_CLIENT_ID:
        cfg["github_client_id"] = GITHUB_CLIENT_ID
    if GITHUB_CLIENT_SECRET:
        cfg["github_client_secret"] = GITHUB_CLIENT_SECRET
    if GOOGLE_CLIENT_ID:
        cfg["google_client_id"] = GOOGLE_CLIENT_ID
    return cfg


def zp_merchant(cfg: dict):
    """(merchant, sandbox?) — مرچنت واقعی از متغیر محیطی یا پنل؛ بدون آن، درگاه آزمایشی."""
    m = (ZARINPAL_MERCHANT or str(cfg.get("zarinpal_merchant") or "")).strip()
    if m:
        return m, ZARINPAL_SANDBOX
    if cfg.get("zarinpal_sandbox_when_empty", True):
        return "00000000-0000-0000-0000-000000000000", True
    return "", False


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


def me_json(r, c=None) -> dict:
    out = {"invited": r["invited"], "ad_free_until": r["ad_free_until"], "pro_until": r["pro_until"],
           "pro_plan": r["pro_plan"] or "", "code": r["code"] or "", "referred_by": bool(r["referred_by"])}
    try:
        if c is not None and r["user_id"]:
            u = c.execute("SELECT * FROM users WHERE id=?", (r["user_id"],)).fetchone()
            if u:
                out["pro_until"] = max(out["pro_until"], u["pro_until"])
                out["account"] = {"id": u["id"], "login": u["login"], "name": u["name"], "provider": u["provider"]}
    except Exception:
        pass
    return out



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
    for k in ("ai_key", "ai_base_url", "ai_model", "ai_provider", "github_client_secret", "zarinpal_merchant", "plugins"):
        cfg.pop(k, None)
    cfg["zarinpal_sandbox"] = zp_merchant(get_config())[1]
    cfg["login_google"] = bool(cfg.get("google_client_id"))
    cfg["login_github"] = bool(cfg.get("github_client_id"))
    ep = str(cfg.get("ai_endpoint") or "").strip()
    if not ep.lower().startswith(("http://", "https://")) or _looks_secret(ep):
        cfg["ai_endpoint"] = ""
    return cfg


def _ip(req: Request) -> str:
    f = req.headers.get("x-forwarded-for", "")
    return (f.split(",")[0].strip() if f else (req.client.host if req.client else ""))[:64]


@app.post("/api/me")
async def api_me(req: Request):
    j = await req.json()
    tok = str(j.get("token", "") or req.headers.get("X-Nava-Token", ""))
    ip = _ip(req)
    with db() as c:
        r = touch_device(c, str(j.get("device", "")), str(j.get("code", ""))[:12].upper())
        if ip and not r["ip"]:
            c.execute("UPDATE devices SET ip=? WHERE id=?", (ip, r["id"]))
        if tok:
            sess = c.execute("SELECT user_id FROM sessions WHERE token=?", (tok,)).fetchone()
            if sess and r["user_id"] != sess["user_id"]:
                _link_device(c, r["id"], sess["user_id"])
                r = c.execute("SELECT * FROM devices WHERE id=?", (r["id"],)).fetchone()
        return me_json(r, c)


@app.post("/api/referral/claim")
async def api_claim(req: Request):
    """کد دعوت: هر دو نفر ۲۴ ساعت پرو؛ هر ۱۳ دعوت = ۱ ماه پرو. ضدتقلب: هر دستگاه یک بار، دستگاه تازه، سقف IP."""
    j = await req.json()
    device = str(j.get("device", "")); code = str(j.get("code", "")).strip().upper()
    ip = _ip(req)
    cfg = get_config()
    hours = int(cfg.get("referral_bonus_hours", 24)); milestone = max(1, int(cfg.get("referral_milestone", 13)))
    m_days = int(cfg.get("referral_milestone_days", 30)); ip_daily = int(cfg.get("referral_ip_daily", 3))
    new_days = int(cfg.get("referral_new_device_days", 7))
    with db() as c:
        me = touch_device(c, device)
        if me["referred_by"]:
            return {"ok": False, "message": "شما قبلاً یک کد دعوت ثبت کرده‌اید."}
        inviter = c.execute("SELECT * FROM devices WHERE code=?", (code,)).fetchone()
        if not inviter:
            return {"ok": False, "message": "این کد دعوت پیدا نشد."}
        if inviter["id"] == device or (inviter["user_id"] and inviter["user_id"] == me["user_id"]):
            return {"ok": False, "message": "کد خودتان را نمی‌شود ثبت کرد."}
        if new_days > 0 and me["first_seen"] and me["first_seen"] < now_ms() - new_days * DAY:
            return {"ok": False, "message": f"کد دعوت فقط تا {new_days} روز بعد از نصب قابل ثبت است."}
        if ip and inviter["ip"] and ip == inviter["ip"]:
            return {"ok": False, "message": "دعوت از همان شبکه‌ی دعوت‌کننده پذیرفته نمی‌شود."}
        day_ago = now_ms() - DAY
        if c.execute("SELECT COUNT(*) n FROM claims_ip WHERE ip=? AND ts>?", (ip, day_ago)).fetchone()["n"] >= ip_daily:
            return {"ok": False, "message": "امروز از این شبکه کد زیادی ثبت شده؛ فردا دوباره امتحان کنید."}
        t = now_ms()
        c.execute("UPDATE devices SET referred_by=? WHERE id=?", (inviter["id"], device))
        c.execute("UPDATE devices SET invited=invited+1 WHERE id=?", (inviter["id"],))
        c.execute("INSERT INTO claims_ip VALUES(?,?)", (ip, t))
        extend_pro(c, device, hours / 24, "invite")
        extend_pro(c, inviter["id"], hours / 24, "invite")
        c.execute("INSERT INTO rewards VALUES(?,?,?,?,?)", (device, 0, "invited", hours, t))
        c.execute("INSERT INTO rewards VALUES(?,?,?,?,?)", (inviter["id"], 0, "inviter", hours, t))
        inv = c.execute("SELECT invited, milestones FROM devices WHERE id=?", (inviter["id"],)).fetchone()
        reached = inv["invited"] // milestone
        if reached > (inv["milestones"] or 0):
            extend_pro(c, inviter["id"], m_days, "milestone")
            c.execute("UPDATE devices SET milestones=? WHERE id=?", (reached, inviter["id"]))
            c.execute("INSERT INTO rewards VALUES(?,?,?,?,?)", (inviter["id"], 0, "milestone", m_days * 24, t))
        me = c.execute("SELECT * FROM devices WHERE id=?", (device,)).fetchone()
        return {"ok": True, "message": f"ثبت شد! شما و دوستتان هر کدام {hours} ساعت ناوا پرو هدیه گرفتید.", **me_json(me, c)}


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


def extend_pro(c, device: str, days: float, plan: str):
    """پرو را برای دستگاه (و حساب متصل به آن) تمدید می‌کند."""
    add = int(days * DAY)
    c.execute("UPDATE devices SET pro_until=MAX(pro_until, ?)+?, pro_plan=? WHERE id=?", (now_ms(), add, plan, device))
    r = c.execute("SELECT user_id, pro_until FROM devices WHERE id=?", (device,)).fetchone()
    if r and r["user_id"]:
        c.execute("UPDATE users SET pro_until=MAX(pro_until, ?), pro_plan=? WHERE id=?", (r["pro_until"], plan, r["user_id"]))
        # همه‌ی دستگاه‌های همین حساب
        c.execute("UPDATE devices SET pro_until=MAX(pro_until, ?) WHERE user_id=?", (r["pro_until"], r["user_id"]))


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
LLM7 = "https://api.llm7.io/v1/chat/completions"
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


_last_status = {"code": 0}


async def _proxy(url: str, headers: dict, body: dict):
    _last_status["code"] = 0
    """درخواست را می‌فرستد؛ اگر stream بود جواب را تکه‌تکه رد می‌کند. خطا قبل از شروع = None."""
    if body.get("stream"):
        req = _ai_client.build_request("POST", url, json=body, headers=headers)
        r = await _ai_client.send(req, stream=True)
        if r.status_code != 200:
            _last_status["code"] = r.status_code
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
        _last_status["code"] = r.status_code
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
    t0 = time.time()
    cfg = get_config()
    t = ai_target(cfg)
    if t and cfg.get("ai_pro_only") and not _is_pro(device):
        t = None
    targets = []
    if not (cfg.get("ai_pro_only") and not _is_pro(device)):
        targets = v44.pool_targets()          # ۴٫۴: استخر کلیدهای سالم (نگهبان)
    if t:
        targets.append((*t, None))
    for url, headers, model, pool_id in targets:
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
        ok = False
        for v in variants:
            try:
                res = await _proxy(url, headers, v)
                if res is not None:
                    _ai_log(True, t0, "pool" if pool_id else "key")
                    if pool_id:
                        v44.report_ok(pool_id)
                    return res
            except Exception:
                pass
        if pool_id:
            v44.report_failure(pool_id, _last_status.get("code", 0))
    # مغز رایگان: LLM7 (سریع، ابزار) ← Pollinations (کند ولی همیشگی)
    free = {k: body[k] for k in ALLOWED_KEYS if k in body}
    for i, model in enumerate(("default", "fast")):
        if i:
            await asyncio.sleep(1.1)   # LLM7 رایگان: حداکثر ۱ درخواست در ثانیه
        try:
            res = await _proxy(LLM7, {}, {**free, "model": model})
            if res is not None:
                _ai_log(True, t0, "llm7")
                return res
        except Exception:
            pass
    pbody = dict(body); pbody["private"] = True; pbody.setdefault("model", "openai-fast")
    try:
        res = await _proxy(POLLINATIONS, {}, pbody)
        if res is not None:
            _ai_log(True, t0, "pollinations")
            return res
    except Exception:
        pass
    _ai_log(False, t0, "none")
    raise HTTPException(502, "ai unavailable")


def _ai_log(ok: bool, t0: float, src: str):
    try:
        with db() as c:
            c.execute("INSERT INTO ai_log VALUES(?,?,?,?)", (now_ms(), 1 if ok else 0, int((time.time() - t0) * 1000), src))
            if ok is False or secrets.randbelow(50) == 0:
                c.execute("DELETE FROM ai_log WHERE ts<?", (now_ms() - 40 * DAY,))
    except Exception:
        pass


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
    if event not in ("impression", "click", "complete", "skip"):
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


BASE_URL = PUBLIC_URL or "https://nava-server-irc5.onrender.com"


def _zp_base(sandbox: bool) -> str:
    return "https://sandbox.zarinpal.com" if sandbox else "https://payment.zarinpal.com"


def _pay_page(title: str, msg: str, ok: bool, deep: str) -> HTMLResponse:
    color = "#16a34a" if ok else "#dc2626"
    return HTMLResponse(f"""<!doctype html><html lang=fa dir=rtl><meta name=viewport content="width=device-width,initial-scale=1">
<title>{title}</title><body style="font-family:Vazirmatn,Tahoma,sans-serif;background:linear-gradient(135deg,#7c3aed,#4f46e5,#06b6d4);min-height:100vh;margin:0;display:flex;align-items:center;justify-content:center">
<div style="background:#fff;border-radius:24px;padding:32px;max-width:360px;text-align:center"><h2 style="color:{color}">{title}</h2><p>{msg}</p>
<a href="{deep}" style="display:inline-block;margin-top:12px;background:#7c3aed;color:#fff;padding:14px 22px;border-radius:14px;text-decoration:none">بازگشت به ناوا</a></div>
<script>setTimeout(function(){{location.href="{deep}"}},1200)</script></body></html>""")


@app.get("/pay/start")
async def pay_start(device: str, plan: str):
    cfg = get_config(); p = _plan(plan)
    merchant, sandbox = zp_merchant(cfg)
    if not cfg["payment_enabled"] or not p or not merchant:
        return _pay_page("پرداخت فعال نیست", "پرداخت اینترنتی هنوز روشن نشده است؛ از کارت‌به‌کارت استفاده کنید.", False, "nava://pro?status=off")
    with db() as c:
        touch_device(c, device)
    amount_rial = int(p["price"]) * 10
    try:
        async with httpx.AsyncClient(timeout=20) as h:
            r = await h.post(f"{_zp_base(sandbox)}/pg/v4/payment/request.json", headers={"Accept": "application/json"}, json={
                "merchant_id": merchant, "amount": amount_rial, "currency": "IRR",
                "callback_url": f"{BASE_URL}/pay/callback", "description": f"اشتراک ناوا پرو - {p['title']}",
                "metadata": {"order_id": device[:30]}})
        data = (r.json() or {}).get("data") or {}
    except Exception:
        data = {}
    if data.get("code") != 100:
        return _pay_page("خطا در اتصال به درگاه", "لطفاً کمی بعد دوباره امتحان کنید.", False, "nava://pro?status=error")
    auth = data["authority"]
    with db() as c:
        c.execute("INSERT INTO payments VALUES(?,?,?,?,?,?,?,?)", (auth, device, plan, amount_rial, "pending", "sandbox" if sandbox else "", now_ms(), 0))
    return RedirectResponse(f"{_zp_base(sandbox)}/pg/StartPay/{auth}")


async def _zp_verify(pay, cfg):
    """(data, network_ok) — تأیید پرداخت با زرین‌پال؛ با یک بار تلاش دوباره."""
    sandbox = str(pay["ref_id"] or "").startswith("sandbox")
    merchant = "00000000-0000-0000-0000-000000000000" if sandbox else zp_merchant(cfg)[0]
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=20) as h:
                r = await h.post(f"{_zp_base(sandbox)}/pg/v4/payment/verify.json", headers={"Accept": "application/json"},
                                 json={"merchant_id": merchant, "amount": pay["amount"], "authority": pay["authority"]})
            j = r.json() or {}
            return (j.get("data") or {}) | {"_errors": j.get("errors")}, True
        except Exception:
            await asyncio.sleep(1.5)
    return {}, False


def _mark_paid(c, pay, ref: str, plans: dict) -> bool:
    cur = c.execute("UPDATE payments SET status='paid', ref_id=?, verified=? WHERE authority=? AND status!='paid'",
                    (ref, now_ms(), pay["authority"]))
    if cur.rowcount:
        p = plans.get(pay["plan"]) or {"days": 30}
        extend_pro(c, pay["device"], int(p["days"]), pay["plan"])
        return True
    return False


@app.get("/api/pay/verify_info")
def pay_verify_info(authority: str):
    """برای تأیید از سمت اپ (داخل ایران) وقتی سرور به زرین‌پال نمی‌رسد."""
    cfg = get_config()
    with db() as c:
        pay = c.execute("SELECT * FROM payments WHERE authority=?", (authority,)).fetchone()
    if not pay or pay["status"] not in ("pending", "verify_pending", "client_ok"):
        return {"ok": False}
    sandbox = str(pay["ref_id"] or "").startswith("sandbox")
    return {"ok": True, "amount": pay["amount"], "sandbox": sandbox,
            "merchant_id": "00000000-0000-0000-0000-000000000000" if sandbox else zp_merchant(cfg)[0],
            "verify_url": f"{_zp_base(sandbox)}/pg/v4/payment/verify.json"}


@app.post("/api/pay/client_verify")
async def pay_client_verify(req: Request):
    """اپ نتیجه‌ی verify زرین‌پال را می‌فرستد. پرو موقت ۲۴ ساعته فوری فعال می‌شود؛
    سرور در پس‌زمینه دوباره با زرین‌پال تأیید می‌کند و بعد مدت کامل را می‌دهد (یا مدیر با یک لمس تأیید می‌کند)."""
    j = await req.json()
    auth = str(j.get("authority", ""))[:64]; device = str(j.get("device", ""))[:64]
    code = int(j.get("code", 0) or 0); ref = str(j.get("ref_id", ""))[:40]
    cfg = get_config(); plans = {p["id"]: p for p in cfg.get("plans", [])}
    data, net_ok = await _zp_verify_by_auth(auth, cfg)
    with db() as c:
        pay = c.execute("SELECT * FROM payments WHERE authority=?", (auth,)).fetchone()
        if not pay or pay["device"] != device:
            return {"ok": False, "message": "پرداخت پیدا نشد."}
        if pay["status"] == "paid":
            me = c.execute("SELECT * FROM devices WHERE id=?", (device,)).fetchone()
            return {"ok": True, "message": "پرو فعال است ✓", **me_json(me, c)}
        if net_ok and data.get("code") in (100, 101):
            _mark_paid(c, pay, str(data.get("ref_id", ref)), plans)
            me = c.execute("SELECT * FROM devices WHERE id=?", (device,)).fetchone()
            return {"ok": True, "message": "پرداخت تأیید و ناوا پرو فعال شد ✓", **me_json(me, c)}
        if code not in (100, 101):
            c.execute("UPDATE payments SET status='failed' WHERE authority=?", (auth,))
            return {"ok": False, "message": "پرداخت تأیید نشد."}
        if pay["status"] != "client_ok":
            keep = ("sandbox:" if str(pay["ref_id"] or "").startswith("sandbox") else "") + (ref or "")
            c.execute("UPDATE payments SET status='client_ok', ref_id=? WHERE authority=?", (keep or pay["ref_id"], auth))
            extend_pro(c, device, 1, "pending_verify")
        me = c.execute("SELECT * FROM devices WHERE id=?", (device,)).fetchone()
        return {"ok": True, "provisional": True, "message": "پرداخت ثبت شد؛ ناوا پرو فعال است و تأیید نهایی خودکار انجام می‌شود.", **me_json(me, c)}


async def _zp_verify_by_auth(auth: str, cfg: dict):
    with db() as c:
        pay = c.execute("SELECT * FROM payments WHERE authority=?", (auth,)).fetchone()
    if not pay:
        return {}, False
    return await _zp_verify(pay, cfg)


async def _verify_loop():
    """هر ۱۰ دقیقه پرداخت‌های تأییدنشده را دوباره با زرین‌پال بررسی می‌کند."""
    while True:
        await asyncio.sleep(600)
        try:
            cfg = get_config(); plans = {p["id"]: p for p in cfg.get("plans", [])}
            with db() as c:
                rows = c.execute("SELECT * FROM payments WHERE status IN ('verify_pending','client_ok') AND created>?", (now_ms() - 3 * DAY,)).fetchall()
            for pay in rows:
                data, ok = await _zp_verify(pay, cfg)
                if ok and data.get("code") in (100, 101):
                    with db() as c:
                        _mark_paid(c, pay, str(data.get("ref_id", "")), plans)
        except Exception as e:
            print("[verify_loop]", e, flush=True)


@app.on_event("startup")
async def _start_verify_loop():
    asyncio.create_task(_verify_loop())


@app.get("/pay/callback")
async def pay_callback(Authority: str = "", Status: str = ""):
    cfg = get_config()
    plans = {p["id"]: p for p in cfg.get("plans", [])}
    with db() as c:
        pay = c.execute("SELECT * FROM payments WHERE authority=?", (Authority,)).fetchone()
    if not pay:
        return _pay_page("پرداخت پیدا نشد", "", False, "nava://pro?status=error")
    if pay["status"] == "paid":
        return _pay_page("پرداخت موفق ✓", "اشتراک شما فعال است.", True, "nava://pro?status=ok")
    if Status != "OK":
        with db() as c:
            c.execute("UPDATE payments SET status='canceled' WHERE authority=?", (Authority,))
        return _pay_page("پرداخت انجام نشد", "مبلغی کسر نشده یا تا ۷۲ ساعت برمی‌گردد.", False, "nava://pro?status=cancel")
    sandbox = pay["ref_id"] == "sandbox"
    data, net_ok = await _zp_verify(pay, cfg)
    if not net_ok:
        # سرور (خارج از ایران) به زرین‌پال نرسید: اپ از داخل ایران تأیید می‌کند و سرور بعداً دوباره بررسی می‌کند
        with db() as c:
            c.execute("UPDATE payments SET status='verify_pending' WHERE authority=? AND status='pending'", (Authority,))
        return _pay_page("در حال تأیید پرداخت…", "ناوا را باز کنید تا پرداخت از داخل ایران تأیید و پرو فعال شود.", True,
                         f"nava://pay?authority={Authority}&status=OK")
    if data.get("code") in (100, 101):
        with db() as c:
            # فقط یک بار اعمال شود
            _mark_paid(c, pay, ("sandbox:" if sandbox else "") + str(data.get("ref_id", "")), plans)
        note = " (پرداخت آزمایشی)" if sandbox else ""
        return _pay_page("پرداخت موفق ✓", f"ناوا پرو فعال شد{note}. کد پیگیری: {data.get('ref_id', '')}", True, f"nava://pro?status=ok&ref={data.get('ref_id', '')}")
    with db() as c:
        c.execute("UPDATE payments SET status='failed' WHERE authority=?", (Authority,))
    return _pay_page("تأیید پرداخت ناموفق", "اگر مبلغ کسر شده، تا ۷۲ ساعت برمی‌گردد.", False, "nava://pro?status=error")


# ------------------------------------------------------------------ دانلود و آمار عمومی
def _kv_get(c, k, default="0"):
    r = c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return r["v"] if r else default


@app.get("/dl")
def download(request: Request):
    """شمارنده‌ی دانلود + ریدایرکت به آخرین APK روی شاخه‌ی download."""
    latest = str(get_config().get("latest_name") or LATEST_DEFAULT)
    day = time.strftime("%Y%m%d", time.gmtime())
    try:
        with db() as c:
            n = int(_kv_get(c, "downloads")) + 1
            c.execute("INSERT OR REPLACE INTO kv VALUES('downloads', ?)", (str(n),))
            dn = int(_kv_get(c, "dl_" + day)) + 1
            c.execute("INSERT OR REPLACE INTO kv VALUES(?, ?)", ("dl_" + day, str(dn)))
    except Exception:
        pass
    return RedirectResponse(f"https://github.com/seakurd/Nava/raw/download/Nava-{latest}.apk", status_code=302)


@app.get("/api/stats")
def public_stats():
    """فقط اعداد بی‌خطر برای صفحه‌ی معرفی."""
    t = now_ms()
    latest = str(get_config().get("latest_name") or LATEST_DEFAULT)   # بیرون از قفل db (قفل بازگشتی نیست)
    with db() as c:
        q = lambda s, *a: c.execute(s, a).fetchone()[0]
        return {"downloads": int(_kv_get(c, "downloads")), "devices": q("SELECT COUNT(*) FROM devices"),
                "active_7d": q("SELECT COUNT(*) FROM devices WHERE last_seen>?", t - 7 * DAY),
                "latest_version": latest}


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
    key = cfg.get("ai_key") or ""; gh = cfg.get("github_client_secret") or ""
    cfg["ai_key"] = ""; cfg["github_client_secret"] = ""
    m, sb = zp_merchant(cfg)
    return cfg | {"_zarinpal_ready": bool(m) and not sb, "_zarinpal_sandbox": sb, "_zarinpal_env": bool(ZARINPAL_MERCHANT),
                  "_ai_key_hint": ("…" + key[-4:]) if key else "", "_github_secret_set": bool(gh)}


@app.put("/admin/api/config")
async def admin_put_config(req: Request, _=Depends(admin)):
    j = await req.json()
    for k in list(j):
        if k.startswith("_") and k not in ("_dl_v2", "_v43"):
            j.pop(k, None)
    old = get_config()
    j.setdefault("_dl_v2", True); j.setdefault("_v43", True)
    # کلیدها فقط نوشتنی: خالی = قبلی بماند؛ "-" = پاک شود
    for k in ("ai_key", "github_client_secret"):
        v = (j.get(k) or "").strip()
        j[k] = "" if v == "-" else (v or old.get(k, ""))
    _rescue_keys(j)
    save_config(j)
    return {"ok": True}


@app.post("/admin/api/ai_test")
async def admin_ai_test(_=Depends(admin)):
    """آزمایش مغز: زمان پاسخ و یک جمله‌ی نمونه."""
    cfg = get_config()
    t = ai_target(cfg)
    body = {"messages": [{"role": "user", "content": "در یک جمله‌ی کوتاه فارسی سلام کن."}], "max_tokens": 60}
    url, headers = (t[0], t[1]) if t else (LLM7, {})
    body["model"] = t[2] if t else "default"
    if t and "api.openai.com" in url:
        body["max_completion_tokens"] = body.pop("max_tokens")
    start = time.time()
    try:
        r = await _ai_client.post(url, json=body, headers=headers)
        if r.status_code == 429:
            await asyncio.sleep(1.5)
            r = await _ai_client.post(url, json=body, headers=headers)
        ms = int((time.time() - start) * 1000)
        if r.status_code != 200:
            return {"ok": False, "ms": ms, "provider": cfg.get("ai_provider"), "error": r.text[:300]}
        text = r.json()["choices"][0]["message"]["content"]
        return {"ok": True, "ms": ms, "provider": cfg.get("ai_provider") if t else "free (llm7)", "text": (text or "")[:200]}
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


@app.delete("/admin/api/devices/{device}/pro")
def admin_revoke(device: str, _=Depends(admin)):
    with db() as c:
        c.execute("UPDATE devices SET pro_until=0, pro_plan='' WHERE id=?", (device,))
    return {"ok": True}


@app.post("/admin/api/backup")
def admin_backup(_=Depends(admin)):
    if not persist.enabled():
        return {"ok": False, "message": "بکاپ خارجی تنظیم نشده است."}
    persist.save(DB, _lock, force=True)
    return {"ok": not persist.last_error, "last_ok": persist.last_ok, "error": persist.last_error, "target": persist.enabled()}


@app.get("/admin/api/report")
def admin_report(_=Depends(admin)):
    """گزارش کامل لحظه‌ای برای ناوا ادمین (بدون هیچ کلیدی)."""
    t = now_ms()
    cfg = get_config()
    with db() as c:
        q = lambda s, *a: c.execute(s, a).fetchone()[0]
        days = []
        for i in range(29, -1, -1):
            start = (int(time.time()) // 86400 - i) * 86400 * 1000
            end = start + DAY
            days.append({"day": time.strftime("%m/%d", time.gmtime(start / 1000)),
                         "installs": q("SELECT COUNT(*) FROM devices WHERE first_seen>=? AND first_seen<?", start, end),
                         "active": q("SELECT COUNT(DISTINCT device) FROM events WHERE ts>=? AND ts<?", start, end),
                         "downloads": int(_kv_get(c, "dl_" + time.strftime("%Y%m%d", time.gmtime(start / 1000))))})
        ai_day = c.execute("SELECT COUNT(*) n, COALESCE(SUM(ok),0) ok, COALESCE(AVG(CASE WHEN ok=1 THEN ms END),0) ms FROM ai_log WHERE ts>?", (t - DAY,)).fetchone()
        ai_week = c.execute("SELECT COUNT(*) n, COALESCE(SUM(ok),0) ok, COALESCE(AVG(CASE WHEN ok=1 THEN ms END),0) ms FROM ai_log WHERE ts>?", (t - 7 * DAY,)).fetchone()
        ai_src = [dict(r) for r in c.execute("SELECT src, COUNT(*) n FROM ai_log WHERE ts>? GROUP BY src ORDER BY n DESC", (t - 7 * DAY,))]
        ad = lambda ev, since: q("SELECT COUNT(*) FROM ad_events WHERE event=? AND ts>?", ev, since)
        report = {
            "generated_at": t,
            "downloads": {"total": int(_kv_get(c, "downloads")), "admin_total": int(_kv_get(c, "downloads_admin")),
                          "today": int(_kv_get(c, "dl_" + time.strftime("%Y%m%d", time.gmtime())))},
            "devices": {
                "total": q("SELECT COUNT(*) FROM devices"),
                "new_1d": q("SELECT COUNT(*) FROM devices WHERE first_seen>?", t - DAY),
                "new_7d": q("SELECT COUNT(*) FROM devices WHERE first_seen>?", t - 7 * DAY),
                "new_30d": q("SELECT COUNT(*) FROM devices WHERE first_seen>?", t - 30 * DAY),
                "dau": q("SELECT COUNT(*) FROM devices WHERE last_seen>?", t - DAY),
                "wau": q("SELECT COUNT(*) FROM devices WHERE last_seen>?", t - 7 * DAY),
                "mau": q("SELECT COUNT(*) FROM devices WHERE last_seen>?", t - 30 * DAY),
            },
            "versions": [dict(r) for r in c.execute("SELECT version, COUNT(*) n FROM devices GROUP BY version ORDER BY version DESC LIMIT 12")],
            "pro": {
                "active": q("SELECT COUNT(*) FROM devices WHERE pro_until>?", t),
                "expiring_7d": q("SELECT COUNT(*) FROM devices WHERE pro_until>? AND pro_until<?", t, t + 7 * DAY),
                "expired_30d": q("SELECT COUNT(*) FROM devices WHERE pro_until>0 AND pro_until<? AND pro_until>?", t, t - 30 * DAY),
                "by_plan": [dict(r) for r in c.execute("SELECT pro_plan plan, COUNT(*) n FROM devices WHERE pro_until>? GROUP BY pro_plan", (t,))],
            },
            "referrals": {"total": q("SELECT COALESCE(SUM(invited),0) FROM devices"),
                          "referred_devices": q("SELECT COUNT(*) FROM devices WHERE referred_by IS NOT NULL AND referred_by!=''")},
            "ads": {"enabled": bool(cfg.get("ads_enabled", True)), "every_n": cfg.get("ad_every_n", 1),
                    "impressions_1d": ad("impression", t - DAY), "clicks_1d": ad("click", t - DAY), "skips_1d": ad("skip", t - DAY),
                    "completes_1d": ad("complete", t - DAY),
                    "impressions_7d": ad("impression", t - 7 * DAY), "clicks_7d": ad("click", t - 7 * DAY), "skips_7d": ad("skip", t - 7 * DAY),
                    "active_ads": q("SELECT COUNT(*) FROM ads WHERE active=1")},
            "accounts": {"total": q("SELECT COUNT(*) FROM users"), "new_7d": q("SELECT COUNT(*) FROM users WHERE created>?", t - 7 * DAY),
                         "google": q("SELECT COUNT(*) FROM users WHERE provider='google'"), "github": q("SELECT COUNT(*) FROM users WHERE provider='github'"),
                         "linked_devices": q("SELECT COUNT(*) FROM devices WHERE user_id>0")},
            "rewards_7d": [dict(r) for r in c.execute("SELECT kind, COUNT(*) n, SUM(hours) hours FROM rewards WHERE ts>? GROUP BY kind", (t - 7 * DAY,))],
            "codes": {"created": q("SELECT COUNT(*) FROM codes"), "used": q("SELECT COUNT(*) FROM codes WHERE used_by IS NOT NULL")},
            "payments": {"manual_pending": q("SELECT COUNT(*) FROM manual_pay WHERE status='pending'"),
                         "online_unverified": q("SELECT COUNT(*) FROM payments WHERE status IN ('client_ok','verify_pending')"),
                         "manual_approved": q("SELECT COUNT(*) FROM manual_pay WHERE status='approved'"),
                         "paid_total": q("SELECT COUNT(*) FROM payments WHERE status='paid'"),
                         "revenue_toman": q("SELECT COALESCE(SUM(amount),0)/10 FROM payments WHERE status='paid'")},
            "commands": {"today": q("SELECT COUNT(*) FROM events WHERE ts>?", t - DAY),
                         "top_7d": [dict(r) for r in c.execute("SELECT kind, COUNT(*) n FROM events WHERE ts>? GROUP BY kind ORDER BY n DESC LIMIT 10", (t - 7 * DAY,))]},
            "ai": {"requests_1d": ai_day["n"], "errors_1d": ai_day["n"] - ai_day["ok"], "avg_ms_1d": int(ai_day["ms"] or 0),
                   "requests_7d": ai_week["n"], "errors_7d": ai_week["n"] - ai_week["ok"], "avg_ms_7d": int(ai_week["ms"] or 0),
                   "sources_7d": ai_src, "provider": cfg.get("ai_provider", "free"), "model": cfg.get("ai_model", ""),
                   "key_set": bool(cfg.get("ai_key")), "pro_only": bool(cfg.get("ai_pro_only"))},
            "daily": days,
        }
    report["server"] = {
        "uptime_sec": int(time.time() - START_TIME),
        "db_bytes": DB.stat().st_size if DB.exists() else 0,
        "backup_target": persist.enabled(), "last_backup": persist.last_ok, "backup_error": persist.last_error,
        "latest_name": str(cfg.get("latest_name") or LATEST_DEFAULT),
        "min_version": (cfg.get("update") or {}).get("min_version", 0),
        "latest_version": (cfg.get("update") or {}).get("latest_version", 0),
        "maintenance": bool(cfg.get("maintenance")),
    }
    return report


@app.get("/admin/api/manual")
def admin_manual(_=Depends(admin)):
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM manual_pay ORDER BY (status='pending') DESC, created DESC LIMIT 200")]


@app.post("/admin/api/manual/{pid}/{decision}")
def admin_manual_decide(pid: int, decision: str, _=Depends(admin)):
    if decision not in ("approve", "reject"):
        raise HTTPException(400)
    plans = {p["id"]: p for p in get_config().get("plans", [])}   # بیرون از قفل db تا قفل بن‌بست نشود
    with db() as c:
        r = c.execute("SELECT * FROM manual_pay WHERE id=?", (pid,)).fetchone()
        if not r or r["status"] != "pending":
            return {"ok": False}
        if decision == "approve":
            p = plans.get(r["plan"]) or {"days": 30}
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


# ------------------------------------------------------------------ حساب کاربری (۴٫۳)
def _hash(pw: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 120_000).hex()


def _norm_login(login: str) -> str:
    l = (login or "").strip().lower()
    digits = "".join(ch for ch in l.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")) if ch.isdigit() or ch == "+")
    if "@" not in l and len(digits) >= 10:
        d = digits.lstrip("+")
        if d.startswith("98"):
            d = "0" + d[2:]
        if len(d) == 10 and d.startswith("9"):
            d = "0" + d
        return d
    return l


def _valid_login(l: str) -> bool:
    return bool(_re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", l)) or bool(_re.fullmatch(r"\+?\d{10,15}", l))


def _link_device(c, device: str, user_id: int):
    """دستگاه را به حساب وصل می‌کند و پرو را بین آن‌ها یکی می‌کند."""
    d = c.execute("SELECT pro_until FROM devices WHERE id=?", (device,)).fetchone()
    u = c.execute("SELECT pro_until FROM users WHERE id=?", (user_id,)).fetchone()
    if not d or not u:
        return
    best = max(d["pro_until"], u["pro_until"])
    c.execute("UPDATE devices SET user_id=?, pro_until=? WHERE id=?", (user_id, best, device))
    c.execute("UPDATE users SET pro_until=?, last_login=? WHERE id=?", (best, now_ms(), user_id))


def _signup_bonus(c, device: str, user_id: int, cfg: dict) -> int:
    """۲۴ ساعت پرو برای ثبت‌نام تازه؛ فقط یک بار به ازای دستگاه و یک بار به ازای حساب."""
    hours = int(cfg.get("signup_bonus_hours", 24))
    if hours <= 0:
        return 0
    d = c.execute("SELECT signup_bonus FROM devices WHERE id=?", (device,)).fetchone()
    u = c.execute("SELECT bonus_given FROM users WHERE id=?", (user_id,)).fetchone()
    if not d or not u or d["signup_bonus"] or u["bonus_given"]:
        return 0
    c.execute("UPDATE devices SET signup_bonus=1 WHERE id=?", (device,))
    c.execute("UPDATE users SET bonus_given=1 WHERE id=?", (user_id,))
    extend_pro(c, device, hours / 24, "signup")
    c.execute("INSERT INTO rewards VALUES(?,?,?,?,?)", (device, user_id, "signup", hours, now_ms()))
    return hours


def _session(c, user_id: int, device: str) -> str:
    tok = secrets.token_urlsafe(32)
    c.execute("INSERT INTO sessions VALUES(?,?,?,?)", (tok, user_id, device, now_ms()))
    return tok


def _auth_result(c, device: str, user_id: int, cfg: dict, created: bool, msg: str) -> dict:
    _link_device(c, device, user_id)
    bonus = _signup_bonus(c, device, user_id, cfg)
    tok = _session(c, user_id, device)
    me = c.execute("SELECT * FROM devices WHERE id=?", (device,)).fetchone()
    if bonus:
        msg += f" 🎁 {bonus} ساعت ناوا پرو هدیه گرفتید."
    return {"ok": True, "token": tok, "created": created, "bonus_hours": bonus, "message": msg, **me_json(me, c)}


def _too_many_fails(c, ip: str) -> bool:
    return c.execute("SELECT COUNT(*) n FROM login_fail WHERE ip=? AND ts>?", (ip, now_ms() - 3600_000)).fetchone()["n"] >= 12


@app.post("/api/auth/register")
async def auth_register(req: Request):
    j = await req.json()
    cfg = get_config()
    if not cfg.get("accounts_enabled", True):
        return {"ok": False, "message": "ثبت‌نام موقتاً بسته است."}
    device = str(j.get("device", ""))[:64]; login = _norm_login(str(j.get("login", "")))
    pw = str(j.get("password", "")); name = str(j.get("name", "")).strip()[:40]
    if not _valid_login(login):
        return {"ok": False, "message": "ایمیل یا شماره‌ی موبایل درست نیست."}
    if len(pw) < 6:
        return {"ok": False, "message": "رمز باید دست‌کم ۶ نویسه باشد."}
    with db() as c:
        touch_device(c, device)
        if c.execute("SELECT 1 FROM users WHERE login=?", (login,)).fetchone():
            return {"ok": False, "message": "این حساب قبلاً ساخته شده؛ وارد شوید."}
        salt = secrets.token_hex(8)
        cur = c.execute("INSERT INTO users(login, pass_hash, salt, name, provider, created) VALUES(?,?,?,?,?,?)",
                        (login, _hash(pw, salt), salt, name, "password", now_ms()))
        return _auth_result(c, device, cur.lastrowid, cfg, True, "حساب ساخته شد ✓")


@app.post("/api/auth/login")
async def auth_login(req: Request):
    j = await req.json()
    cfg = get_config(); ip = _ip(req)
    device = str(j.get("device", ""))[:64]; login = _norm_login(str(j.get("login", ""))); pw = str(j.get("password", ""))
    with db() as c:
        touch_device(c, device)
        if _too_many_fails(c, ip):
            return {"ok": False, "message": "تلاش زیاد بود؛ یک ساعت دیگر امتحان کنید."}
        u = c.execute("SELECT * FROM users WHERE login=?", (login,)).fetchone()
        if not u or not u["pass_hash"] or not secrets.compare_digest(u["pass_hash"], _hash(pw, u["salt"] or "")):
            c.execute("INSERT INTO login_fail VALUES(?,?)", (ip, now_ms()))
            return {"ok": False, "message": "ایمیل/شماره یا رمز اشتباه است."}
        if u["banned"]:
            return {"ok": False, "message": "این حساب مسدود است."}
        return _auth_result(c, device, u["id"], cfg, False, "وارد شدید ✓")


@app.post("/api/auth/logout")
async def auth_logout(req: Request):
    j = await req.json()
    with db() as c:
        c.execute("DELETE FROM sessions WHERE token=?", (str(j.get("token", "")),))
        c.execute("UPDATE devices SET user_id=0 WHERE id=?", (str(j.get("device", ""))[:64],))
    return {"ok": True}


def _oauth_user(c, provider: str, pid: str, login: str, name: str) -> int:
    u = c.execute("SELECT id FROM users WHERE provider=? AND provider_id=?", (provider, pid)).fetchone()
    if u:
        return u["id"]
    login = _norm_login(login) or f"{provider}:{pid}"
    u = c.execute("SELECT id FROM users WHERE login=?", (login,)).fetchone()
    if u:   # ایمیل همان حساب رمزدار است: متصل کن
        c.execute("UPDATE users SET provider_id=?, name=COALESCE(NULLIF(name,''),?) WHERE id=?", (pid, name, u["id"]))
        return u["id"]
    cur = c.execute("INSERT INTO users(login, pass_hash, salt, name, provider, provider_id, created) VALUES(?,?,?,?,?,?,?)",
                    (login, "", "", name[:40], provider, pid, now_ms()))
    return cur.lastrowid


@app.post("/api/auth/google")
async def auth_google(req: Request):
    """ورود با حساب گوگل (Credential Manager): id_token را با گوگل بررسی می‌کند."""
    j = await req.json()
    cfg = get_config()
    cid = str(cfg.get("google_client_id") or "").strip()
    if not cid:
        return {"ok": False, "message": "ورود با گوگل هنوز روشن نشده است."}
    device = str(j.get("device", ""))[:64]; tok = str(j.get("id_token", ""))
    try:
        async with httpx.AsyncClient(timeout=15) as h:
            r = await h.get("https://oauth2.googleapis.com/tokeninfo", params={"id_token": tok})
        info = r.json() if r.status_code == 200 else {}
    except Exception:
        info = {}
    if info.get("aud") != cid or not info.get("sub") or info.get("email_verified") not in ("true", True):
        return {"ok": False, "message": "ورود با گوگل تأیید نشد."}
    with db() as c:
        touch_device(c, device)
        uid = _oauth_user(c, "google", info["sub"], info.get("email", ""), info.get("name", ""))
        return _auth_result(c, device, uid, cfg, False, "با گوگل وارد شدید ✓")


@app.get("/auth/github/start")
def github_start(device: str):
    cfg = get_config()
    cid = str(cfg.get("github_client_id") or "").strip()
    if not cid or not cfg.get("github_client_secret"):
        return _pay_page("ورود با گیت‌هاب روشن نیست", "مدیر هنوز آن را فعال نکرده است.", False, "nava://auth?status=off")
    state = secrets.token_urlsafe(16)
    with db() as c:
        c.execute("INSERT OR REPLACE INTO kv VALUES(?,?)", ("gh_state_" + state, device[:64] + "|" + str(now_ms())))
    from urllib.parse import urlencode
    q = urlencode({"client_id": cid, "redirect_uri": f"{BASE_URL}/auth/github/callback", "scope": "read:user user:email", "state": state})
    return RedirectResponse("https://github.com/login/oauth/authorize?" + q)


@app.get("/auth/github/callback")
async def github_callback(code: str = "", state: str = ""):
    cfg = get_config()
    with db() as c:
        row = c.execute("SELECT v FROM kv WHERE k=?", ("gh_state_" + state,)).fetchone()
        c.execute("DELETE FROM kv WHERE k=?", ("gh_state_" + state,))
    if not row or not code:
        return _pay_page("ورود ناموفق", "دوباره امتحان کنید.", False, "nava://auth?status=error")
    device, ts = row["v"].split("|")
    if now_ms() - int(ts) > 15 * 60_000:
        return _pay_page("مهلت ورود تمام شد", "دوباره امتحان کنید.", False, "nava://auth?status=error")
    try:
        async with httpx.AsyncClient(timeout=20) as h:
            t = await h.post("https://github.com/login/oauth/access_token", headers={"Accept": "application/json"},
                             data={"client_id": cfg.get("github_client_id"), "client_secret": cfg.get("github_client_secret"), "code": code})
            at = (t.json() or {}).get("access_token", "")
            hdr = {"Authorization": f"Bearer {at}", "Accept": "application/vnd.github+json", "User-Agent": "nava"}
            u = (await h.get("https://api.github.com/user", headers=hdr)).json()
            email = u.get("email") or ""
            if not email:
                ems = (await h.get("https://api.github.com/user/emails", headers=hdr)).json()
                email = next((e["email"] for e in ems if isinstance(e, dict) and e.get("primary") and e.get("verified")), "")
    except Exception:
        return _pay_page("ورود ناموفق", "به گیت‌هاب وصل نشد.", False, "nava://auth?status=error")
    if not u.get("id"):
        return _pay_page("ورود ناموفق", "گیت‌هاب اجازه نداد.", False, "nava://auth?status=error")
    with db() as c:
        touch_device(c, device)
        uid = _oauth_user(c, "github", str(u["id"]), email, u.get("name") or u.get("login") or "")
        res = _auth_result(c, device, uid, cfg, False, "با گیت‌هاب وارد شدید ✓")
    return _pay_page("ورود موفق ✓", res["message"], True, f"nava://auth?status=ok&token={res['token']}")


# ------------------------------------------------------------------ افزونه‌ها (۴٫۳)
BUILTIN_PLUGINS = [
    {"id": "weather", "title": "آب‌وهوا", "emoji": "🌤", "desc": "هوای امروز و ۳ روز آینده‌ی هر شهر", "builtin": True},
    {"id": "news", "title": "اخبار", "emoji": "📰", "desc": "تازه‌ترین خبرهای فارسی درباره‌ی هر موضوع", "builtin": True},
    {"id": "rates", "title": "ارز و طلا", "emoji": "💵", "desc": "قیمت لحظه‌ای دلار (تتر)، طلا، بیت‌کوین", "builtin": True},
    {"id": "translate", "title": "ترجمه", "emoji": "🌍", "desc": "ترجمه‌ی دقیق بین فارسی، انگلیسی، کردی، عربی و…", "builtin": True},
    {"id": "websearch", "title": "جستجوی وب", "emoji": "🔎", "desc": "جستجوی اینترنت برای اطلاعات تازه", "builtin": True},
    {"id": "prayer", "title": "اوقات شرعی", "emoji": "🕌", "desc": "اذان صبح، ظهر، مغرب و… برای هر شهر", "builtin": True},
    {"id": "calendar", "title": "تقویم", "emoji": "📅", "desc": "تاریخ شمسی/قمری/میلادی، تبدیل تاریخ و رویداد در تقویم", "builtin": True},
    {"id": "images", "title": "تصویرساز", "emoji": "🎨", "desc": "کشیدن تصویر با هوش مصنوعی", "builtin": True},
    {"id": "calculator", "title": "ماشین‌حساب", "emoji": "🧮", "desc": "محاسبه‌ی دقیق", "builtin": True},
]


def _public_plugins(cfg: dict) -> list:
    off = set(cfg.get("plugins_off") or [])
    out = [dict(p, enabled=p["id"] not in off) for p in BUILTIN_PLUGINS]
    for p in cfg.get("plugins") or []:
        if not isinstance(p, dict) or not p.get("id") or not p.get("url"):
            continue
        tool = p.get("tool") or {}
        out.append({"id": "c_" + str(p["id"]), "title": p.get("title", p["id"]), "emoji": p.get("emoji", "🧩"),
                    "desc": p.get("desc", ""), "builtin": False, "enabled": bool(p.get("enabled", True)), "pro": bool(p.get("pro", False)),
                    "url": p["url"], "method": (p.get("method") or "POST").upper(), "tool": tool})
    return out


@app.get("/api/plugins")
def api_plugins():
    return {"plugins": _public_plugins(get_config())}


_plug_client = httpx.AsyncClient(timeout=httpx.Timeout(15, connect=6), follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 NavaServer"})


@app.get("/api/plugin/news")
async def plug_news(q: str = ""):
    """خبرهای فارسی (Google News RSS) از سرور، برای جاهایی که مستقیم باز نمی‌شود."""
    from urllib.parse import quote
    url = (f"https://news.google.com/rss/search?q={quote(q)}&hl=fa&gl=IR&ceid=IR:fa" if q.strip()
           else "https://news.google.com/rss?hl=fa&gl=IR&ceid=IR:fa")
    try:
        r = await _plug_client.get(url)
        items = _re.findall(r"<item>.*?<title>(.*?)</title>.*?<link>(.*?)</link>.*?<pubDate>(.*?)</pubDate>.*?(?:<source[^>]*>(.*?)</source>)?.*?</item>", r.text, _re.S)
        unesc = lambda t: t.replace("&amp;", "&").replace("&quot;", '"').replace("&#39;", "'").replace("<![CDATA[", "").replace("]]>", "")
        return {"ok": True, "items": [{"title": unesc(t), "link": l, "date": d, "source": unesc(s or "")} for t, l, d, s in items[:12]]}
    except Exception as e:
        return {"ok": False, "items": [], "error": str(e)[:100]}


@app.get("/api/plugin/rates")
async def plug_rates():
    """قیمت‌ها از نوبیتکس (ریال): تتر≈دلار، طلای پکس‌گلد (یک اونس)، بیت‌کوین، اتریوم."""
    out = {}
    try:
        r = await _plug_client.get("https://apiv2.nobitex.ir/market/stats?srcCurrency=usdt,btc,eth,paxg&dstCurrency=rls")
        st = (r.json() or {}).get("stats", {})
        for k, name in (("usdt-rls", "dollar_tether"), ("btc-rls", "bitcoin"), ("eth-rls", "ethereum"), ("paxg-rls", "gold_ounce_paxg")):
            v = st.get(k) or {}
            if v.get("latest"):
                out[name] = {"toman": int(float(v["latest"]) / 10), "change_24h_percent": v.get("dayChange")}
    except Exception:
        pass
    try:
        if "gold_ounce_paxg" in out:
            g = out["gold_ounce_paxg"]["toman"]
            out["gold_18k_per_gram_estimate"] = {"toman": int(g / 31.1035 * 0.75)}
    except Exception:
        pass
    return {"ok": bool(out), "source": "nobitex.ir", "rates": out, "ts": now_ms()}


@app.get("/api/plugin/prayer")
async def plug_prayer(city: str = "Tehran", country: str = "Iran"):
    try:
        r = await _plug_client.get("https://api.aladhan.com/v1/timingsByCity", params={"city": city, "country": country, "method": 7})
        d = (r.json() or {}).get("data", {})
        t = d.get("timings", {})
        return {"ok": bool(t), "city": city, "date_hijri": (d.get("date", {}).get("hijri", {}) or {}).get("date"),
                "timings": {k: t.get(k) for k in ("Fajr", "Sunrise", "Dhuhr", "Asr", "Sunset", "Maghrib", "Isha", "Midnight")}}
    except Exception as e:
        return {"ok": False, "error": str(e)[:100]}


# ------------------------------------------------------------------ دانلود ادمین و نسخه‌ها
@app.get("/dl/admin")
def download_admin():
    name = str(get_config().get("admin_latest_name") or "2.2.0")
    with db() as c:
        n = int(_kv_get(c, "downloads_admin")) + 1
        c.execute("INSERT OR REPLACE INTO kv VALUES('downloads_admin', ?)", (str(n),))
    return RedirectResponse(f"https://github.com/seakurd/Nava/raw/download/NavaAdmin-{name}.apk", status_code=302)


@app.get("/api/versions")
def api_versions():
    cfg = get_config(); up = cfg.get("update") or {}
    return {"app": {"name": cfg.get("latest_name"), "code": up.get("latest_version", 0), "min": up.get("min_version", 0), "url": f"{BASE_URL}/dl"},
            "admin": {"name": cfg.get("admin_latest_name"), "code": cfg.get("admin_latest_code", 0), "url": f"{BASE_URL}/dl/admin"}}


# ------------------------------------------------------------------ مدیریت حساب‌ها و افزونه‌ها (پنل)
@app.get("/admin/api/users")
def admin_users(q: str = "", _=Depends(admin)):
    with db() as c:
        if q.strip():
            like = f"%{q.strip().lower()}%"
            rows = c.execute("SELECT id, login, name, provider, created, pro_until, pro_plan, last_login, banned FROM users WHERE login LIKE ? OR name LIKE ? ORDER BY last_login DESC LIMIT 60", (like, like))
        else:
            rows = c.execute("SELECT id, login, name, provider, created, pro_until, pro_plan, last_login, banned FROM users ORDER BY last_login DESC LIMIT 60")
        out = []
        for r in rows:
            d = dict(r); d["devices"] = c.execute("SELECT COUNT(*) FROM devices WHERE user_id=?", (r["id"],)).fetchone()[0]
            out.append(d)
        return out


@app.post("/admin/api/users/{uid}/{action}")
async def admin_user_action(uid: int, action: str, req: Request, _=Depends(admin)):
    j = await req.json() if (await req.body()) else {}
    with db() as c:
        if action == "pro":
            days = float(j.get("days", 30))
            c.execute("UPDATE users SET pro_until=MAX(pro_until, ?)+?, pro_plan='gift' WHERE id=?", (now_ms(), int(days * DAY), uid))
            u = c.execute("SELECT pro_until FROM users WHERE id=?", (uid,)).fetchone()
            if u:
                c.execute("UPDATE devices SET pro_until=MAX(pro_until, ?) WHERE user_id=?", (u["pro_until"], uid))
        elif action == "revoke":
            c.execute("UPDATE users SET pro_until=0, pro_plan='' WHERE id=?", (uid,))
            c.execute("UPDATE devices SET pro_until=0, pro_plan='' WHERE user_id=?", (uid,))
        elif action in ("ban", "unban"):
            c.execute("UPDATE users SET banned=? WHERE id=?", (1 if action == "ban" else 0, uid))
            if action == "ban":
                c.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
        else:
            raise HTTPException(400)
    return {"ok": True}


@app.get("/admin/api/payments")
def admin_payments(_=Depends(admin)):
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM payments ORDER BY (status IN ('client_ok','verify_pending')) DESC, created DESC LIMIT 100")]


@app.post("/admin/api/payments/{authority}/approve")
def admin_payment_approve(authority: str, _=Depends(admin)):
    plans = {p["id"]: p for p in get_config().get("plans", [])}
    with db() as c:
        pay = c.execute("SELECT * FROM payments WHERE authority=?", (authority,)).fetchone()
        if not pay:
            return {"ok": False}
        _mark_paid(c, pay, str(pay["ref_id"] or "") + ":admin", plans)
    return {"ok": True}


@app.get("/admin/api/plugins")
def admin_plugins(_=Depends(admin)):
    cfg = get_config()
    return {"builtin": BUILTIN_PLUGINS, "off": cfg.get("plugins_off") or [], "custom": cfg.get("plugins") or []}


# ------------------------------------------------------------------ نسخه‌ی آیفون و وب (PWA)
from fastapi.staticfiles import StaticFiles

@app.get("/app")
def app_redirect():
    return RedirectResponse("/app/")

app.mount("/app", StaticFiles(directory=Path(__file__).parent / "web", html=True), name="web")


@app.get("/")
def root():
    return RedirectResponse("/app/")


# ------------------------------------------------------------------ ۴٫۴
from app import v44
v44.setup(app)
