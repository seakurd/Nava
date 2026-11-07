"""
نگه‌داری دائمی دیتای ناوا روی هاست‌های رایگان (مثل Render) که دیسکشان با هر ری‌استارت پاک می‌شود.
اگر DATABASE_URL (Postgres، مثلاً Neon رایگان) تنظیم باشد:
  - موقع شروع، آخرین نسخه‌ی nava.db از Postgres بازیابی می‌شود؛
  - هر ۲۰ ثانیه اگر تغییری بوده، یک کپی امن از SQLite در Postgres ذخیره می‌شود.
"""
import os, sqlite3, threading, time, hashlib
from pathlib import Path

URL = os.environ.get("DATABASE_URL", "").strip()
_PW = os.environ.get("DB_PASSWORD", "").strip()
if URL and _PW and "[YOUR-PASSWORD]" in URL:
    from urllib.parse import quote as _q
    URL = URL.replace("[YOUR-PASSWORD]", _q(_PW, safe=""))
_last_hash = None


def _conn():
    import psycopg
    return psycopg.connect(URL, autocommit=True, connect_timeout=15)


def restore(db_path: Path):
    global _last_hash
    if not URL:
        return
    try:
        with _conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS nava_blob (k TEXT PRIMARY KEY, v BYTEA, ts BIGINT)")
            r = c.execute("SELECT v FROM nava_blob WHERE k='sqlite'").fetchone()
        if r and r[0]:
            data = bytes(r[0])
            db_path.write_bytes(data)
            _last_hash = hashlib.sha1(data).hexdigest()
            print(f"[persist] restored {len(data)} bytes", flush=True)
    except Exception as e:
        print("[persist] restore failed:", e, flush=True)


def _snapshot(db_path: Path) -> bytes:
    tmp = db_path.with_suffix(".snap")
    src = sqlite3.connect(db_path); dst = sqlite3.connect(tmp)
    try:
        src.backup(dst)
    finally:
        dst.close(); src.close()
    data = tmp.read_bytes(); tmp.unlink(missing_ok=True)
    return data


def save(db_path: Path, lock=None):
    global _last_hash
    if not URL or not db_path.exists():
        return
    try:
        if lock:
            with lock:
                data = _snapshot(db_path)
        else:
            data = _snapshot(db_path)
        h = hashlib.sha1(data).hexdigest()
        if h == _last_hash:
            return
        with _conn() as c:
            c.execute("INSERT INTO nava_blob(k,v,ts) VALUES('sqlite',%s,%s) ON CONFLICT (k) DO UPDATE SET v=EXCLUDED.v, ts=EXCLUDED.ts",
                      (data, int(time.time())))
        _last_hash = h
    except Exception as e:
        print("[persist] save failed:", e, flush=True)


def start(db_path: Path, lock=None):
    if not URL:
        return
    def loop():
        while True:
            time.sleep(20)
            save(db_path, lock)
    threading.Thread(target=loop, daemon=True).start()
