"""
ناوا ۴٫۴ — افزوده‌های سرور:
- ربات نگهبان کلیدهای هوش مصنوعی (استخر چند کلید، تست هر ۵ دقیقه، چرخش، به‌روزرسانی مدل از /models، هشدار)
- صدای کردی از سرور: /api/tts/ku  (Meta MMS-TTS، ONNX int8، تنبل و کش‌شده)
- پاداش تماشای آگهی = دقیقه‌ی پرو: /api/reward/ad
- شمارنده و تبلیغ سایت: /api/site/hit ، /api/ads/site
- سلامت: /health  (برای پینگ GitHub Actions)
کلیدها در kv['key_pool'] می‌مانند و هرگز در /api/config یا هیچ پاسخ عمومی نمی‌آیند.
"""
import asyncio, io, json, os, re, secrets, struct, threading, time
from pathlib import Path

import httpx
from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response

CHECK_EVERY = 300          # ۵ دقیقه
QUOTA_COOLDOWN = 30 * 60   # کلید سهمیه‌تمام: ۳۰ دقیقه کنار
ERROR_COOLDOWN = 5 * 60
STARTED = time.time()

PRESETS = {
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "gemini-2.0-flash"),
    "groq": ("https://api.groq.com/openai/v1", "llama-3.3-70b-versatile"),
    "openrouter": ("https://openrouter.ai/api/v1", "meta-llama/llama-3.3-70b-instruct:free"),
    "openai": ("https://api.openai.com/v1", "gpt-4o-mini"),
    "deepseek": ("https://api.deepseek.com/v1", "deepseek-chat"),
    "mistral": ("https://api.mistral.ai/v1", "mistral-small-latest"),
    "together": ("https://api.together.xyz/v1", "meta-llama/Llama-3.3-70B-Instruct-Turbo-Free"),
    "custom": ("", ""),
}
# ترجیح مدل وقتی مدل فعلی از فهرست /models حذف شده باشد (سریع و ارزان اول)
PREFER = [
    r"gemini-2\.5-flash$", r"gemini-2\.0-flash$", r"gemini-.*flash(?!.*(image|tts|live|thinking))",
    r"llama-3\.3-70b-versatile", r"llama-3\.1-8b-instant", r"gpt-4o-mini$", r"gpt-4\.1-mini$",
    r"deepseek-chat$", r"mistral-small-latest$", r":free$", r"llama", r"gpt", r"qwen",
]
_client = httpx.AsyncClient(timeout=httpx.Timeout(20, connect=6))
_rr = {"i": 0}
_check_lock = asyncio.Lock()


def _m():
    from app import main as m
    return m


def mask(k: str) -> str:
    k = k or ""
    return (k[:4] + "…" + k[-4:]) if len(k) > 10 else "••••"


# ------------------------------------------------------------------ ذخیره‌ی استخر
def load_pool() -> list:
    m = _m()
    with m.db() as c:
        r = c.execute("SELECT v FROM kv WHERE k='key_pool'").fetchone()
    try:
        return json.loads(r["v"]) if r else []
    except Exception:
        return []


def save_pool(pool: list):
    m = _m()
    with m.db() as c:
        c.execute("INSERT OR REPLACE INTO kv VALUES('key_pool', ?)", (json.dumps(pool, ensure_ascii=False),))


def alert(msg: str, level: str = "warn"):
    m = _m()
    try:
        with m.db() as c:
            r = c.execute("SELECT v FROM kv WHERE k='key_alerts'").fetchone()
            arr = json.loads(r["v"]) if r else []
            arr.insert(0, {"ts": int(time.time() * 1000), "level": level, "msg": msg})
            c.execute("INSERT OR REPLACE INTO kv VALUES('key_alerts', ?)", (json.dumps(arr[:60], ensure_ascii=False),))
    except Exception as e:
        print("[alert]", e, flush=True)


def alerts() -> list:
    m = _m()
    with m.db() as c:
        r = c.execute("SELECT v FROM kv WHERE k='key_alerts'").fetchone()
    try:
        return json.loads(r["v"]) if r else []
    except Exception:
        return []


def public_entry(e: dict) -> dict:
    """نسخه‌ی بی‌خطر برای پنل: کلید فقط ماسک‌شده"""
    d = {k: v for k, v in e.items() if k != "key"}
    d["key_mask"] = mask(e.get("key", ""))
    d["models"] = (e.get("models") or [])[:80]
    return d


def _base(e: dict) -> str:
    return (e.get("base_url") or PRESETS.get(e.get("provider", ""), ("", ""))[0] or "").rstrip("/")


def _model(e: dict) -> str:
    return (e.get("model") or PRESETS.get(e.get("provider", ""), ("", ""))[1] or "").strip()


def detect_provider(key: str) -> str:
    k = (key or "").strip()
    if k.startswith("AIza"): return "gemini"
    if k.startswith("gsk_"): return "groq"
    if k.startswith("sk-or-"): return "openrouter"
    if k.startswith("sk-"): return "openai"
    return "custom"


def pick_model(models: list, current: str) -> str:
    if current and current in models:
        return current
    for pat in PREFER:
        for mm in models:
            if re.search(pat, mm):
                return mm
    return models[0] if models else current


# ------------------------------------------------------------------ تست یک کلید
async def check_entry(e: dict) -> dict:
    base, key = _base(e), (e.get("key") or "").strip()
    now = time.time()
    e["last_check"] = int(now * 1000)
    if not base or not key:
        e.update(status="bad", last_error="آدرس یا کلید خالی است")
        return e
    h = {"Authorization": f"Bearer {key}"}
    prev = e.get("status")
    # ۱) فهرست مدل‌ها
    try:
        r = await _client.get(f"{base}/models", headers=h)
        if r.status_code == 200:
            data = r.json().get("data") or r.json().get("models") or []
            ids = []
            for x in data:
                i = x.get("id") or x.get("name") or ""
                i = i.replace("models/", "")
                if i and not re.search(r"embed|whisper|tts|dall-e|image|moderation|audio|transcribe|guard|vision-preview", i):
                    ids.append(i)
            if ids:
                e["models"] = sorted(set(ids))[:200]
                newm = pick_model(e["models"], _model(e))
                if newm and newm != _model(e):
                    alert(f"مدل کلید «{e.get('label') or e.get('provider')}» از {_model(e) or '—'} به {newm} تغییر کرد.", "info")
                    e["model"] = newm
        elif r.status_code in (401, 403):
            e.update(status="bad", last_error=f"کلید نامعتبر یا مسدود ({r.status_code})", fails=e.get("fails", 0) + 1)
            if prev != "bad":
                alert(f"کلید «{e.get('label') or e.get('provider')}» ({mask(key)}) نامعتبر شد و کنار گذاشته شد.", "error")
            return e
    except Exception:
        pass
    # ۲) یک پرسش خیلی کوتاه
    body = {"model": _model(e), "messages": [{"role": "user", "content": "say ok"}], "max_tokens": 5}
    if "api.openai.com" in base:
        body.pop("max_tokens"); body["max_completion_tokens"] = 16
    t0 = time.time()
    try:
        r = await _client.post(f"{base}/chat/completions", json=body, headers=h)
        # مدل پیدا نشد → تا ۴ مدل دیگر از فهرست را همین حالا امتحان کن
        tried = {_model(e)}
        while r.status_code == 404 and e.get("models") and len(tried) < 5:
            nm = pick_model([x for x in e["models"] if x not in tried], "")
            if not nm or nm in tried:
                break
            tried.add(nm); e["model"] = nm; body["model"] = nm
            t0 = time.time()
            r = await _client.post(f"{base}/chat/completions", json=body, headers=h)
        ms = int((time.time() - t0) * 1000)
        if r.status_code == 200:
            e.update(status="ok", last_error="", latency_ms=ms, fails=0, cooldown_until=0, ok_at=int(now * 1000))
            if prev in ("bad", "quota", "error"):
                alert(f"کلید «{e.get('label') or e.get('provider')}» دوباره سالم شد.", "info")
        elif r.status_code in (401, 403):
            e.update(status="bad", last_error=f"کلید نامعتبر ({r.status_code})", fails=e.get("fails", 0) + 1)
            if prev != "bad":
                alert(f"کلید «{e.get('label') or e.get('provider')}» ({mask(key)}) نامعتبر است و کنار گذاشته شد. کلید تازه لازم است.", "error")
        elif r.status_code in (402, 429):
            e.update(status="quota", last_error=f"سهمیه تمام یا محدودیت سرعت ({r.status_code})",
                     cooldown_until=int((now + QUOTA_COOLDOWN) * 1000), fails=e.get("fails", 0) + 1)
            if prev != "quota":
                alert(f"سهمیه‌ی کلید «{e.get('label') or e.get('provider')}» تمام شد؛ ۳۰ دقیقه کنار می‌رود.", "warn")
        elif r.status_code == 404:
            e.update(status="error", last_error=f"مدل {_model(e)} پیدا نشد: {r.text[:140]}", fails=e.get("fails", 0) + 1,
                     cooldown_until=int((now + ERROR_COOLDOWN) * 1000))
            if e.get("models"):
                e["model"] = pick_model([x for x in e["models"] if x != _model(e)], "")
        else:
            e.update(status="error", last_error=f"خطای {r.status_code}: {r.text[:120]}", fails=e.get("fails", 0) + 1,
                     cooldown_until=int((now + ERROR_COOLDOWN) * 1000))
    except Exception as ex:
        e.update(status="error", last_error=f"اتصال نشد: {type(ex).__name__}", fails=e.get("fails", 0) + 1,
                 cooldown_until=int((now + ERROR_COOLDOWN) * 1000))
    return e


async def check_all() -> list:
    async with _check_lock:
        pool = load_pool()
        live = [e for e in pool if e.get("enabled", True)]
        await asyncio.gather(*(check_entry(e) for e in live), return_exceptions=True)
        # هم‌زمانی: ممکن است وسط تست، پنل چیزی را عوض کرده باشد؛ فقط وضعیت را روی نسخه‌ی تازه بنویس
        fresh = load_pool()
        by = {e.get("id"): e for e in pool}
        for f in fresh:
            o = by.get(f.get("id"))
            if o and f.get("key") == o.get("key"):
                for k in ("status", "last_error", "latency_ms", "fails", "cooldown_until", "last_check", "ok_at", "models", "model"):
                    if k in o:
                        f[k] = o[k]
        save_pool(fresh)
        healthy = [e for e in fresh if e.get("enabled", True) and e.get("status") == "ok"]
        if fresh and not healthy:
            last = alerts()[:1]
            if not last or "هیچ کلید سالمی" not in last[0]["msg"] or time.time() * 1000 - last[0]["ts"] > 3600_000:
                alert("هیچ کلید سالمی در استخر نیست؛ ناوا روی مغز رایگان (LLM7/Pollinations) کار می‌کند. یک کلید تازه اضافه کنید.", "error")
        return fresh


async def guardian_loop():
    await asyncio.sleep(20)
    while True:
        try:
            if load_pool():
                await check_all()
        except Exception as ex:
            print("[guardian]", ex, flush=True)
        await asyncio.sleep(CHECK_EVERY)


def pool_targets() -> list:
    """کلیدهای سالم برای /api/ai، به ترتیب چرخشی (کمترین تأخیر جلوتر). [(url, headers, model, id)]"""
    now = time.time() * 1000
    pool = [e for e in load_pool() if e.get("enabled", True) and e.get("key")
            and e.get("status") in ("ok", "unknown", None) and (e.get("cooldown_until") or 0) < now]
    if not pool:
        return []
    pool.sort(key=lambda e: (int(e.get("priority") or 5), e.get("latency_ms") or 9999))
    _rr["i"] = (_rr["i"] + 1) % len(pool)
    pool = pool[_rr["i"]:] + pool[:_rr["i"]] if len(pool) > 1 and all(int(e.get("priority") or 5) == int(pool[0].get("priority") or 5) for e in pool) else pool
    return [(f"{_base(e)}/chat/completions", {"Authorization": f"Bearer {e['key'].strip()}"}, _model(e), e.get("id")) for e in pool]


def report_failure(eid: str, status_code: int = 0):
    """/api/ai وقتی کلید شکست خورد: فوری کنار بگذار تا نگهبان دوباره تست کند"""
    try:
        pool = load_pool()
        for e in pool:
            if e.get("id") == eid:
                now = time.time()
                if status_code in (401, 403):
                    e.update(status="bad", last_error=f"در استفاده رد شد ({status_code})")
                    alert(f"کلید «{e.get('label') or e.get('provider')}» در استفاده رد شد ({status_code}).", "error")
                elif status_code in (402, 429):
                    e.update(status="quota", cooldown_until=int((now + QUOTA_COOLDOWN) * 1000), last_error=f"سهمیه ({status_code})")
                else:
                    e.update(cooldown_until=int((now + 60) * 1000), fails=e.get("fails", 0) + 1)
                e["uses_fail"] = e.get("uses_fail", 0) + 1
        save_pool(pool)
    except Exception as ex:
        print("[report_failure]", ex, flush=True)


def report_ok(eid: str):
    try:
        pool = load_pool()
        for e in pool:
            if e.get("id") == eid:
                e["uses"] = e.get("uses", 0) + 1
        save_pool(pool)
    except Exception:
        pass


# ------------------------------------------------------------------ صدای کردی (سرور)
MODELS_BASE = os.environ.get("KU_TTS_BASE", "https://github.com/seakurd/Nava/raw/models/tts")
_tts = {"lang": None, "sess": None, "vocab": None}
_tts_lock = threading.Lock()
_tts_sem = asyncio.Semaphore(1)
KU_DIGITS_CKB = ["سفر", "یەک", "دوو", "سێ", "چوار", "پێنج", "شەش", "حەوت", "هەشت", "نۆ"]
KU_DIGITS_KMR = ["sifir", "yek", "du", "sê", "çar", "pênc", "şeş", "heft", "heşt", "neh"]


def normalize_ku(text: str, lang: str) -> str:
    t = text.strip()
    t = re.sub(r"[\u200c\u200f\u200e]", " ", t)
    # ارقام فارسی/عربی → لاتین، سپس رقم‌به‌رقم به کلمه (مدل رقم نمی‌شناسد)
    t = t.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
    words = KU_DIGITS_CKB if lang == "ckb" else KU_DIGITS_KMR
    t = re.sub(r"\d", lambda mm: " " + words[int(mm.group())] + " ", t)
    if lang == "ckb":
        t = t.replace("ک", "ك").replace("ي", "ی").replace("ى", "ی").replace("ة", "ە").replace("ه\u200c", "ە")
    else:
        t = t.lower()
    return re.sub(r"\s+", " ", t).strip()


def _wav(samples, sr=16000) -> bytes:
    import numpy as np
    pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()
    return b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, sr, sr * 2, 2, 16) + b"data" + struct.pack("<I", len(pcm)) + pcm


def _ensure_model(lang: str):
    m = _m()
    d = m.DATA / "ku_tts" / lang
    d.mkdir(parents=True, exist_ok=True)
    for f in ("vocab.json", "model.int8.onnx"):
        p = d / f
        if not p.exists() or p.stat().st_size < 100:
            with httpx.Client(timeout=120, follow_redirects=True) as cl:
                r = cl.get(f"{MODELS_BASE}/{lang}/{f}")
                r.raise_for_status()
                tmp = p.with_suffix(".part"); tmp.write_bytes(r.content); tmp.rename(p)
    return d


def synth_ku(text: str, lang: str) -> bytes:
    import numpy as np
    import onnxruntime as ort
    with _tts_lock:
        if _tts["lang"] != lang:
            _tts["sess"] = None   # فقط یک مدل در حافظه (رم Render محدود است)
            d = _ensure_model(lang)
            so = ort.SessionOptions(); so.intra_op_num_threads = 2
            _tts["sess"] = ort.InferenceSession(str(d / "model.int8.onnx"), so, providers=["CPUExecutionProvider"])
            _tts["vocab"] = {k: v for k, v in json.loads((d / "vocab.json").read_text("utf-8")).items() if len(k) == 1}
            _tts["lang"] = lang
        sess, vocab = _tts["sess"], _tts["vocab"]
        t = normalize_ku(text, lang)
        out = []
        for chunk in re.split(r"(?<=[.!?؟])\s+", t):
            ids = [vocab[ch] for ch in chunk if ch in vocab]
            if len(ids) < 2:
                continue
            seq = [0] * (len(ids) * 2 + 1)
            for i, v in enumerate(ids):
                seq[i * 2 + 1] = v
            w = sess.run(None, {"input_ids": np.array([seq], dtype=np.int64)})[0][0]
            out.append(w); out.append(np.zeros(2400, dtype=np.float32))
        if not out:
            raise ValueError("empty")
        return _wav(np.concatenate(out))


# ------------------------------------------------------------------ ثبت مسیرها
def setup(app):
    m = _m()

    @app.on_event("startup")
    async def _start_guardian():
        asyncio.create_task(guardian_loop())

    @app.get("/health")
    def health():
        pool = load_pool()
        return {"ok": True, "uptime_s": int(time.time() - STARTED),
                "keys_ok": sum(1 for e in pool if e.get("enabled", True) and e.get("status") == "ok"),
                "keys_total": len(pool), "version": str(m.get_config().get("latest_name") or "")}

    @app.head("/health")
    def health_head():
        return Response(status_code=200)

    # ---------- صدای کردی
    @app.get("/api/tts/ku")
    async def tts_ku(text: str = "", lang: str = "ckb"):
        if lang not in ("ckb", "kmr"):
            raise HTTPException(400, "lang")
        text = (text or "").strip()[:400]
        if not text:
            raise HTTPException(400, "text")
        cache = m.TTS_CACHE / "ku"
        cache.mkdir(exist_ok=True)
        import hashlib
        f = cache / (hashlib.sha1(f"{lang}|{text}".encode()).hexdigest() + ".wav")
        if f.exists():
            return Response(f.read_bytes(), media_type="audio/wav")
        async with _tts_sem:
            try:
                data = await asyncio.to_thread(synth_ku, text, lang)
            except Exception as ex:
                print("[tts_ku]", ex, flush=True)
                raise HTTPException(503, "kurdish tts unavailable")
        try:
            f.write_bytes(data)
        except Exception:
            pass
        return Response(data, media_type="audio/wav")

    # ---------- پاداش آگهی
    @app.post("/api/reward/ad")
    async def reward_ad(req: Request):
        j = await req.json()
        device = str(j.get("device", ""))[:64]
        if not device:
            raise HTTPException(400, "device")
        cfg = m.get_config()
        minutes = int(cfg.get("reward_ad_minutes", 30))
        cap = int(cfg.get("reward_ad_daily", 4))
        if not cfg.get("reward_ad_enabled", True) or minutes <= 0:
            return {"ok": False, "msg": "پاداش آگهی فعلاً خاموش است."}
        day = time.strftime("%Y%m%d", time.gmtime())
        k, kl = f"rw_{day}_{device}", f"rwl_{device}"
        with m.db() as c:
            n = int(m._kv_get(c, k))
            last = int(m._kv_get(c, kl))
            nowms = m.now_ms()
            if n >= cap:
                return {"ok": False, "msg": f"سقف پاداش امروز ({cap} آگهی) پر شد. فردا دوباره.", "left": 0}
            if nowms - last < 45_000:
                return {"ok": False, "msg": "کمی صبر کنید و دوباره امتحان کنید.", "left": cap - n}
            m.touch_device(c, device)
            m.extend_pro(c, device, minutes / 1440.0, "reward_ad")
            c.execute("INSERT OR REPLACE INTO kv VALUES(?, ?)", (k, str(n + 1)))
            c.execute("INSERT OR REPLACE INTO kv VALUES(?, ?)", (kl, str(nowms)))
            dk = "rw_total_" + day
            c.execute("INSERT OR REPLACE INTO kv VALUES(?, ?)", (dk, str(int(m._kv_get(c, dk)) + 1)))
            r = c.execute("SELECT * FROM devices WHERE id=?", (device,)).fetchone()
            me = m.me_json(r, c) if r else {}
        return {"ok": True, "minutes": minutes, "left": cap - n - 1, "me": me,
                "msg": f"{minutes} دقیقه ناوا پرو هدیه گرفتید 🎉"}

    # ---------- سایت: شمارنده و تبلیغ
    @app.post("/api/site/hit")
    async def site_hit(req: Request):
        try:
            j = await req.json()
        except Exception:
            j = {}
        lang = str(j.get("lang", ""))[:4]
        day = time.strftime("%Y%m%d", time.gmtime())
        with m.db() as c:
            tot = int(m._kv_get(c, "site_hits")) + 1
            c.execute("INSERT OR REPLACE INTO kv VALUES('site_hits', ?)", (str(tot),))
            dn = int(m._kv_get(c, "site_" + day)) + 1
            c.execute("INSERT OR REPLACE INTO kv VALUES(?, ?)", ("site_" + day, str(dn)))
            if lang in ("fa", "ckb", "kmr", "en"):
                lk = "site_lang_" + lang
                c.execute("INSERT OR REPLACE INTO kv VALUES(?, ?)", (lk, str(int(m._kv_get(c, lk)) + 1)))
            dl = int(m._kv_get(c, "downloads"))
        return JSONResponse({"visits": tot, "today": dn, "downloads": dl}, headers={"Access-Control-Allow-Origin": "*"})

    @app.options("/api/site/hit")
    def site_hit_opt():
        return Response(headers={"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "POST, GET",
                                 "Access-Control-Allow-Headers": "Content-Type"})

    @app.get("/api/site/stats")
    def site_stats():
        with m.db() as c:
            return JSONResponse({"visits": int(m._kv_get(c, "site_hits")), "downloads": int(m._kv_get(c, "downloads"))},
                                headers={"Access-Control-Allow-Origin": "*"})

    @app.get("/api/ads/site")
    def ads_site():
        cfg = m.get_config()
        ad = cfg.get("site_ad") or {}
        out = {"enabled": bool(ad.get("enabled")), "title": ad.get("title", ""), "body": ad.get("body", ""),
               "image_url": ad.get("image_url", ""), "click_url": ad.get("click_url", ""), "cta": ad.get("cta", ""),
               "adsense_client": cfg.get("adsense_client", "") if cfg.get("adsense_enabled") else "",
               "adsense_slot": cfg.get("adsense_slot", "") if cfg.get("adsense_enabled") else ""}
        return JSONResponse(out, headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "max-age=300"})

    # ---------- پنل: استخر کلید
    @app.get("/admin/api/keys")
    def admin_keys(_=Depends(m.admin)):
        pool = load_pool()
        return {"keys": [public_entry(e) for e in pool], "alerts": alerts()[:30],
                "providers": {k: {"base": v[0], "model": v[1]} for k, v in PRESETS.items()},
                "healthy": sum(1 for e in pool if e.get("enabled", True) and e.get("status") == "ok")}

    @app.post("/admin/api/keys")
    async def admin_add_key(req: Request, _=Depends(m.admin)):
        j = await req.json()
        pool = load_pool()
        eid = str(j.get("id") or "")
        e = next((x for x in pool if x.get("id") == eid), None) if eid else None
        if e is None:
            key = str(j.get("key") or "").strip()
            if len(key) < 8:
                raise HTTPException(400, "کلید کوتاه است")
            if any(x.get("key") == key for x in pool):
                raise HTTPException(400, "این کلید قبلاً اضافه شده")
            prov = str(j.get("provider") or "") or detect_provider(key)
            e = {"id": secrets.token_hex(5), "provider": prov, "key": key, "status": "unknown",
                 "added": int(time.time() * 1000), "enabled": True}
            pool.append(e)
        else:
            if j.get("key"):
                e["key"] = str(j["key"]).strip(); e["status"] = "unknown"
        for k in ("label", "base_url", "model", "provider"):
            if k in j and j[k] is not None and (k != "provider" or j[k]):
                e[k] = str(j[k]).strip()
        if "enabled" in j:
            e["enabled"] = bool(j["enabled"])
        if "priority" in j:
            e["priority"] = int(j["priority"] or 5)
        await check_entry(e)
        save_pool(pool)
        return {"ok": True, "key": public_entry(e)}

    @app.delete("/admin/api/keys/{eid}")
    def admin_del_key(eid: str, _=Depends(m.admin)):
        pool = [e for e in load_pool() if e.get("id") != eid]
        save_pool(pool)
        return {"ok": True}

    @app.post("/admin/api/keys/check")
    async def admin_check_keys(_=Depends(m.admin)):
        pool = await check_all()
        return {"keys": [public_entry(e) for e in pool], "alerts": alerts()[:30]}

    @app.delete("/admin/api/keys_alerts")
    def admin_clear_alerts(_=Depends(m.admin)):
        with m.db() as c:
            c.execute("DELETE FROM kv WHERE k='key_alerts'")
        return {"ok": True}

    @app.get("/admin/api/site")
    def admin_site(_=Depends(m.admin)):
        t = time.gmtime()
        with m.db() as c:
            days = []
            for i in range(14):
                d = time.strftime("%Y%m%d", time.gmtime(time.time() - i * 86400))
                days.append({"day": d, "visits": int(m._kv_get(c, "site_" + d)), "downloads": int(m._kv_get(c, "dl_" + d)),
                             "rewards": int(m._kv_get(c, "rw_total_" + d))})
            langs = {l: int(m._kv_get(c, "site_lang_" + l)) for l in ("fa", "ckb", "kmr", "en")}
            return {"visits": int(m._kv_get(c, "site_hits")), "downloads": int(m._kv_get(c, "downloads")), "days": days, "langs": langs}
