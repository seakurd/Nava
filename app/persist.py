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
GH_TOKEN = os.environ.get("GH_TOKEN", "").strip()
GH_REPO = os.environ.get("GH_REPO", "").strip()          # مثلاً seakurd/nava-data (خصوصی)
GH_PATH = os.environ.get("GH_PATH", "nava.db")
_gh_sha = None
if not URL and not GH_TOKEN:
    pass


def _gh(method, data=None):
    import json, urllib.request
    url = f"https://api.github.com/repos/{GH_REPO}/contents/{GH_PATH}"
    req = urllib.request.Request(url, method=method, data=json.dumps(data).encode() if data else None,
        headers={"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json",
                 "User-Agent": "nava-server"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"{}")


def _gh_get():
    import base64, json, urllib.request, urllib.error
    global _gh_sha
    try:
        meta = _gh("GET")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    _gh_sha = meta.get("sha")
    req = urllib.request.Request(f"https://api.github.com/repos/{GH_REPO}/git/blobs/{_gh_sha}",
        headers={"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github.raw+json",
                 "User-Agent": "nava-server"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def _gh_put(data: bytes):
    import base64
    global _gh_sha
    body = {"message": "nava db backup", "content": base64.b64encode(data).decode()}
    if _gh_sha:
        body["sha"] = _gh_sha
    res = _gh("PUT", body)
    _gh_sha = res.get("content", {}).get("sha", _gh_sha)


def _conn():
    import psycopg
    return psycopg.connect(URL, autocommit=True, connect_timeout=15)


def restore(db_path: Path):
    global _last_hash
    if not URL and GH_TOKEN and GH_REPO:
        try:
            data = _gh_get()
            if data:
                db_path.write_bytes(data)
                _last_hash = hashlib.sha1(data).hexdigest()
                print(f"[persist] restored {len(data)} bytes from github", flush=True)
        except Exception as e:
            print("[persist] github restore failed:", e, flush=True)
        return
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
    if not (URL or (GH_TOKEN and GH_REPO)) or not db_path.exists():
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
        if not URL:
            _gh_put(data)
            _last_hash = h
            return
        with _conn() as c:
            c.execute("INSERT INTO nava_blob(k,v,ts) VALUES('sqlite',%s,%s) ON CONFLICT (k) DO UPDATE SET v=EXCLUDED.v, ts=EXCLUDED.ts",
                      (data, int(time.time())))
        _last_hash = h
    except Exception as e:
        print("[persist] save failed:", e, flush=True)


def start(db_path: Path, lock=None):
    if not (URL or (GH_TOKEN and GH_REPO)):
        return
    every = 20 if URL else 90
    def loop():
        while True:
            time.sleep(every)
            save(db_path, lock)
    import atexit
    atexit.register(save, db_path, lock)
    threading.Thread(target=loop, daemon=True).start()
