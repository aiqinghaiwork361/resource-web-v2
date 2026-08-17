#!/usr/bin/env python3
"""
网盘资源浏览 Web 应用 v3.0
Flask + MySQL，支持连接池、admin认证、搜索限流
"""
import os
import re
import time
import hashlib
import secrets
import html as html_mod
from functools import wraps
from datetime import datetime

import pymysql
import pymysql.cursors
import requests as http_requests
from flask import Flask, jsonify, request, send_from_directory, session, redirect
from requests_oauthlib import OAuth2Session
import json
import bcrypt
import hashlib

from flask_wtf.csrf import CSRFProtect, generate_csrf

# ==================== Redis 缓存 ====================
_redis_client = None

def get_redis():
    global _redis_client
    if _redis_client is None:
        import redis
        _redis_client = redis.Redis(
            host=os.environ.get("REDIS_HOST", "127.0.0.1"),
            port=int(os.environ.get("REDIS_PORT", "6379")),
            db=int(os.environ.get("REDIS_DB", "0")),
            password=os.environ.get("REDIS_PASSWORD") or _get_setting("redis_password"),
            socket_timeout=2,
            socket_connect_timeout=2,
        )
    return _redis_client

def _make_cache_key(*args):
    """生成 Redis 缓存 key，保留第一个参数作为可扫描的前缀。

    例如 _make_cache_key("resources", q, page) → res_web:resources:<md5>
    前缀化后 _invalidate_resource_cache 才能用 scan_iter("res_web:resources:*")
    精确清掉对应缓存（旧版本 key 无前缀，失效逻辑形同虚设）。
    """
    raw = "|".join(str(a) for a in args)
    prefix = str(args[0]) if args else "k"
    return f"res_web:{prefix}:" + hashlib.md5(raw.encode()).hexdigest()


def _json_default(o):
    """JSON 序列化兜底：datetime/date/Decimal 等非原生类型转字符串。

    注意: 缓存写入用的是标准库 json.dumps，它不像 Flask 的 jsonify 那样
    支持 datetime。缺少这个 default 会让 json.dumps 抛 TypeError，
    进而被调用处的 except 静默吞掉，导致缓存永远写不进去。
    """
    import datetime as _dt
    import decimal as _dec
    if isinstance(o, (_dt.datetime, _dt.date, _dt.time)):
        return o.isoformat()
    if isinstance(o, _dt.timedelta):
        return o.total_seconds()
    if isinstance(o, _dec.Decimal):
        return float(o)
    if isinstance(o, (bytes, bytearray)):
        return o.decode("utf-8", "replace")
    return str(o)


def _invalidate_resource_cache():
    """导入/删除资源后清缓存：resources 列表 + stats/keywords/hot_searches 统计缓存"""
    try:
        r = get_redis()
        # 清资源列表缓存
        for key in r.scan_iter("res_web:resources:*"):
            r.delete(key)
        # 清统计类缓存（导入后 total/types/keywords/hot 都会变）
        for prefix in ("res_web:stats:", "res_web:keywords:", "res_web:hot_searches:"):
            for key in r.scan_iter(prefix + "*"):
                r.delete(key)
    except Exception:
        pass

# ==================== 密码哈希工具（bcrypt） ====================
def hash_password(password: str) -> str:
    """使用 bcrypt 对密码进行哈希"""
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

def verify_password(password: str, stored_hash: str) -> bool:
    """验证密码，兼容旧版 SHA256 哈希（迁移期）"""
    if not stored_hash:
        return False
    # 新格式：bcrypt 哈希以 $2b$ 开头
    if stored_hash.startswith('$2b$') or stored_hash.startswith('$2a$'):
        return bcrypt.checkpw(password.encode('utf-8'), stored_hash.encode('utf-8'))
    # 旧格式：SHA256 hex（64位十六进制字符串）
    if len(stored_hash) == 64 and all(c in '0123456789abcdef' for c in stored_hash.lower()):
        old_hash = hashlib.sha256(password.encode()).hexdigest()
        if old_hash == stored_hash:
            # 验证成功，自动迁移到 bcrypt
            return True
        return False
    return False

app = Flask(__name__)
# secret_key 在 _get_setting() 定义后设置（见下方）
# 与 :5001 生产站隔离 Cookie，避免同主机登录会话互相覆盖
app.config['SESSION_COOKIE_NAME'] = os.environ.get('SESSION_COOKIE_NAME', 'session')
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

# ==================== CSRF 保护 ====================
csrf = CSRFProtect(app)
# 允许 API 端点通过 Header 传递 CSRF token
app.config['WTF_CSRF_HEADERS'] = ['X-CSRFToken', 'X-CSRF-Token']

import gzip
import io
from functools import wraps

# ==================== Gzip 压缩 ====================
@app.after_request
def add_gzip(response):
    """对 JSON/HTML/CSS/JS 响应启用 gzip 压缩"""
    try:
        if response.status_code != 200:
            return response
        # 已经压缩过的不再压缩（防止双重gzip）
        if response.headers.get("Content-Encoding"):
            return response
        accept = request.headers.get("Accept-Encoding", "")
        if "gzip" not in accept:
            return response
        ct = response.content_type or ""
        if not any(t in ct for t in ["json", "html", "javascript", "css", "text"]):
            return response
        data = response.get_data()
        if len(data) < 500:
            return response
        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=6) as f:
            f.write(data)
        response.set_data(buf.getvalue())
        response.headers["Content-Encoding"] = "gzip"
        response.headers["Content-Length"] = len(response.get_data())
        response.headers["Vary"] = "Accept-Encoding"
    except (RuntimeError, TypeError):
        pass  # send_from_directory 直接流式响应，跳过压缩
    return response


# ==================== 图片代理缓存 ====================
import hashlib
import threading
from collections import OrderedDict
_img_cache = OrderedDict()  # url -> (data, content_type, expire)
_IMG_CACHE_MAX = 200  # 单进程内存缓存条目数（Redis 才是主缓存，这里只做热点快取）
_IMG_TTL = 604800  # 7 天
_img_cache_lock = threading.Lock()  # gunicorn 每 worker 2 线程，OrderedDict 需要保护

def _img_cache_evict():
    """淘汰最早的缓存条目，保持 _img_cache 不超过 _IMG_CACHE_MAX"""
    while len(_img_cache) > _IMG_CACHE_MAX:
        _img_cache.popitem(last=False)  # 删除最早插入的条目


def _img_redis_key(url):
    return "res_web:img:" + hashlib.md5(url.encode()).hexdigest()

@app.route("/api/img_proxy")
def img_proxy():
    """代理 TMDB 图片。三级缓存: 进程内存 -> Redis(跨 worker 共享) -> 回源 TMDB。

    注意: 早期版本只有进程内存缓存，但 gunicorn 跑 4 个 worker 是独立进程、
    内存不共享，同一张图最多要回源下载 4 次，首屏 20 张图实测需 8.8 秒。
    加 Redis 层后 4 个 worker 共享缓存。
    """
    import hashlib as _hl
    url = request.args.get("url", "")
    if not url or "tmdb.org" not in url:
        return "", 400
    now = time.time()

    def _respond(data, ct):
        etag = '"' + _hl.md5(data).hexdigest() + '"'
        if request.headers.get("If-None-Match") == etag:
            return "", 304
        return data, 200, {
            "Content-Type": ct,
            "Cache-Control": "public, max-age=604800, immutable",
            "ETag": etag,
        }

    # L1: 进程内存
    with _img_cache_lock:
        hit = _img_cache.get(url)
    if hit:
        data, ct, expire = hit
        if now < expire:
            return _respond(data, ct)

    # L2: Redis（跨 worker 共享）
    rkey = _img_redis_key(url)
    try:
        cached = get_redis().hgetall(rkey)
        if cached:
            data = cached.get(b"d")
            ct = (cached.get(b"c") or b"image/jpeg").decode()
            if data:
                with _img_cache_lock:
                    _img_cache[url] = (data, ct, now + _IMG_TTL)
                    _img_cache_evict()
                return _respond(data, ct)
    except Exception as e:
        app.logger.warning("img_proxy Redis 读取失败: %s: %s", type(e).__name__, e)

    # L3: 回源 TMDB
    try:
        import requests as _req
        r = _req.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200:
            ct = r.headers.get("Content-Type", "image/jpeg")
            with _img_cache_lock:
                _img_cache[url] = (r.content, ct, now + _IMG_TTL)
                _img_cache_evict()
            try:
                pipe = get_redis().pipeline()
                pipe.hset(rkey, mapping={"d": r.content, "c": ct})
                pipe.expire(rkey, _IMG_TTL)
                pipe.execute()
            except Exception as e:
                app.logger.warning("img_proxy Redis 写入失败: %s: %s", type(e).__name__, e)
            return _respond(r.content, ct)
    except Exception as e:
        app.logger.warning("img_proxy 回源失败 %s: %s: %s", url, type(e).__name__, e)
    return "", 404


# ==================== 静态文件长缓存 ====================
@app.after_request
def cache_static(response):
    """静态文件设置长缓存"""
    if request.path.startswith("/api/") or request.path.startswith("/login"):
        return response
    if response.status_code == 200 and not response.headers.get("Cache-Control"):
        response.headers["Cache-Control"] = "public, max-age=300"
    return response



@app.after_request
def add_security_headers(response):
    """添加安全响应头"""
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


# ==================== 配置 ====================
DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "172.23.0.2"),
    "port": int(os.environ.get("DB_PORT", 3306)),
    "user": os.environ.get("DB_USER", "root"),
    "password": os.environ.get("DB_PASSWORD"),
    "database": os.environ.get("DB_NAME", "pan_resource"),
    "charset": "utf8mb4",
    "cursorclass": pymysql.cursors.DictCursor,
    "connect_timeout": 5,
    "read_timeout": 10,
    "write_timeout": 10,
}

# ==================== 限流 ====================
from dbutils.pooled_db import PooledDB

_pool = PooledDB(
    creator=pymysql,
    maxconnections=20,
    mincached=2,
    maxcached=5,
    blocking=True,
    maxusage=None,
    setsession=["SET NAMES utf8mb4"],
    ping=1,  # ping before use
    **DB_CONFIG,
)


def get_db():
    conn = _pool.connection()
    try:
        with conn.cursor() as _cur:
            _cur.execute("SET time_zone = '+08:00'")
    except Exception:
        pass
    return conn


def _get_tmdb_api_key():
    """从 settings 表中获取 TMDB API Key，环境变量 TMDB_API_KEY 优先"""
    env_key = os.environ.get("TMDB_API_KEY")
    if env_key:
        return env_key
    try:
        db = get_db()
        try:
            cur = db.cursor()
            cur.execute("SELECT `value` FROM settings WHERE `key`='tmdb_api_key'")
            row = cur.fetchone()
            if row and row.get("value"):
                return row["value"]
        finally:
            db.close()
    except Exception:
        pass
    raise RuntimeError("TMDB API Key 未配置：请设置环境变量 TMDB_API_KEY 或在 settings 表中添加 tmdb_api_key")



def _get_setting(key, default=None):
    """从 settings 表读取配置，环境变量优先，数据库兜底"""
    env_val = os.environ.get(key.upper())
    if env_val:
        return env_val
    try:
        db = get_db()
        try:
            cur = db.cursor()
            cur.execute("SELECT `value` FROM settings WHERE `key`=%s", (key.lower(),))
            row = cur.fetchone()
            if row and row.get("value"):
                return row["value"]
        finally:
            db.close()
    except Exception:
        pass
    return default

# 设置 secret_key（_get_setting 可用后）
app.secret_key = _get_setting("secret_key") or os.environ.get("SECRET_KEY")

ADMIN_USER = _get_setting("admin_user") or os.environ.get("ADMIN_USER")
ADMIN_PASS = _get_setting("admin_pass") or os.environ.get("ADMIN_PASS")

if not ADMIN_USER or not ADMIN_PASS or not app.secret_key:
    raise RuntimeError("环境变量 ADMIN_USER、ADMIN_PASS 和 SECRET_KEY 必须设置")

if not os.environ.get("DB_PASSWORD"):
    raise RuntimeError("环境变量 DB_PASSWORD 必须设置")

# ==================== 限流 ====================
# 注意: 限流状态必须放 Redis，不能放进程内存。gunicorn 跑 4 个 worker 是
# 独立进程，各算各的计数，实际阈值会被放大 4 倍（登录限流 5 次变成 20 次），
# 等于把暴力破解成本降低 4 倍。Redis 不可用时降级为进程内存 + Lock 兜底，
# 保证限流永远不会完全失效。
import threading as _threading

_rate_limits = {}  # 降级兜底: key -> [count, window_start]
_rate_limits_lock = _threading.Lock()
RATE_LIMIT_WINDOW = 60  # seconds
RATE_LIMIT_MAX = 10  # max requests per window


def check_rate_limit(key):
    """通用限流。返回 True 表示放行。优先 Redis(跨 worker 共享)，失败降级内存。"""
    try:
        rkey = "res_web:rl:" + hashlib.md5(str(key).encode()).hexdigest()
        r = get_redis()
        cnt = r.incr(rkey)
        if cnt == 1:
            r.expire(rkey, RATE_LIMIT_WINDOW)
        return cnt <= RATE_LIMIT_MAX
    except Exception as e:
        app.logger.warning("限流降级到内存 (Redis 不可用): %s", type(e).__name__)

    now = time.time()
    with _rate_limits_lock:
        if key not in _rate_limits:
            _rate_limits[key] = [1, now]
            return True
        count, start = _rate_limits[key]
        if now - start > RATE_LIMIT_WINDOW:
            _rate_limits[key] = [1, now]
            return True
        if count >= RATE_LIMIT_MAX:
            return False
        _rate_limits[key][0] += 1
        return True


# ==================== 登录限流 ====================
_login_failures = {}  # 降级兜底: ip -> [fail_count, last_fail_time]
_login_failures_lock = _threading.Lock()
LOGIN_FAIL_MAX = 5
LOGIN_LOCK_SECONDS = 900  # 15 minutes


def _login_key(ip):
    return "res_web:loginfail:" + hashlib.md5(str(ip).encode()).hexdigest()


def check_login_limit(ip):
    """检查IP是否被登录限流锁定。返回 (allowed, remaining_seconds)"""
    try:
        r = get_redis()
        k = _login_key(ip)
        cnt = r.get(k)
        if cnt and int(cnt) >= LOGIN_FAIL_MAX:
            ttl = r.ttl(k)
            return False, max(int(ttl), 0) if ttl and ttl > 0 else 0
        return True, 0
    except Exception as e:
        app.logger.warning("登录限流降级到内存 (Redis 不可用): %s", type(e).__name__)

    now = time.time()
    with _login_failures_lock:
        if ip in _login_failures:
            count, last_time = _login_failures[ip]
            if count >= LOGIN_FAIL_MAX:
                elapsed = now - last_time
                if elapsed < LOGIN_LOCK_SECONDS:
                    return False, int(LOGIN_LOCK_SECONDS - elapsed)
                _login_failures.pop(ip, None)
                return True, 0
        return True, 0


def record_login_failure(ip):
    """记录一次登录失败"""
    try:
        r = get_redis()
        k = _login_key(ip)
        cnt = r.incr(k)
        # 每次失败都刷新锁定窗口，持续攻击会一直被锁住
        r.expire(k, LOGIN_LOCK_SECONDS)
        return
    except Exception:
        pass

    now = time.time()
    with _login_failures_lock:
        if ip in _login_failures:
            count, _ = _login_failures[ip]
            _login_failures[ip] = [count + 1, now]
        else:
            _login_failures[ip] = [1, now]


def reset_login_failures(ip):
    """登录成功后重置失败计数"""
    try:
        get_redis().delete(_login_key(ip))
    except Exception:
        pass
    with _login_failures_lock:
        _login_failures.pop(ip, None)


# ==================== 内存缓存 ====================
_cache = {}  # key -> (data, expire_time)
_CACHE_MAX_SIZE = 500  # 最大缓存条目数，防止内存泄漏


def _cleanup_cache():
    """清理过期条目，必要时淘汰最旧条目以控制内存"""
    now = time.time()
    expired = [k for k, (_, expire) in _cache.items() if now >= expire]
    for k in expired:
        _cache.pop(k, None)
    if len(_cache) > _CACHE_MAX_SIZE:
        # FIFO 淘汰，只保留最新的一半
        keep = list(_cache.items())[-_CACHE_MAX_SIZE // 2:]
        _cache.clear()
        _cache.update(keep)


def cached(ttl=300):
    """简单内存缓存装饰器，ttl秒内返回缓存数据"""
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            key = request.url
            now = time.time()
            if len(_cache) > _CACHE_MAX_SIZE:
                _cleanup_cache()
            if key in _cache:
                data, expire = _cache[key]
                if now < expire:
                    return data
            result = f(*args, **kwargs)
            _cache[key] = (result, now + ttl)
            return result
        wrapper.__name__ = f.__name__
        wrapper.__doc__ = f.__doc__
        return wrapper
    return decorator


def redis_cached(ttl=300, key_prefix="api"):
    """Redis 缓存装饰器：跨 worker 共享缓存，解决进程内 _cache 命中率低的问题。

    - key = res_web:{key_prefix}:{md5(request.url)}
    - 命中返回缓存的 JSON；miss 则执行原函数并回写
    - Redis 不可用时自动降级为直接计算（不缓存），与原有逻辑一致
    """
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            key = _make_cache_key(key_prefix, request.url)
            try:
                r = get_redis()
                cached = r.get(key)
                if cached:
                    return jsonify(json.loads(cached))
            except Exception:
                pass  # Redis 不可用 → 直接计算
            result = f(*args, **kwargs)
            try:
                data = result.get_json()
                if data is not None:
                    r = get_redis()
                    r.setex(key, ttl, json.dumps(data, default=_json_default))
            except Exception:
                pass  # 写入失败不影响响应
            return result
        wrapper.__name__ = f.__name__
        wrapper.__doc__ = f.__doc__
        return wrapper
    return decorator




# ==================== Admin 认证 ====================
def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("is_admin"):
            if request.is_json or request.path.startswith("/api/"):
                return jsonify({"error": "未登录，请先登录后台"}), 401
            return redirect("/login")
        return f(*args, **kwargs)

    return decorated


@app.route("/login", methods=["GET", "POST"])
def login_page():
    if request.method == "POST":
        data = request.get_json() or request.form
        username = data.get("username", "").strip()
        password = data.get("password", "")
        client_ip = request.remote_addr
        # 检查登录限流
        allowed, remaining = check_login_limit(client_ip)
        if not allowed:
            return jsonify({"ok": False, "error": f"登录尝试过多，请{remaining}秒后再试"}), 429
        if not username or not password:
            return jsonify({"ok": False, "error": "请输入用户名和密码"}), 401
        db = get_db()
        try:
            cur = db.cursor()
            cur.execute("SELECT id, username, role, status, password FROM users WHERE username=%s", (username,))
            user = cur.fetchone()
        finally:
            db.close()
        if not user:
            record_login_failure(client_ip)
            return jsonify({"ok": False, "error": "用户名或密码错误"}), 401
        if user["status"] != 1:
            return jsonify({"ok": False, "error": "账号已被禁用"}), 401
        # 验证密码（兼容旧版 SHA256，验证成功后自动迁移到 bcrypt）
        stored_pw = user.get("password", "")
        if not verify_password(password, stored_pw):
            record_login_failure(client_ip)
            return jsonify({"ok": False, "error": "用户名或密码错误"}), 401
        # 密码验证成功。若仍是旧 SHA256 格式，此时才迁移到 bcrypt。
        # 注意: 迁移必须放在验证成功分支。此前误放在 `if not verify_password(...)`
        # 分支内，导致密码【错误】时用攻击者输入的密码覆写数据库，
        # 攻击者两次请求即可接管任意 SHA256 账号。
        if stored_pw and not stored_pw.startswith('$2b$') and not stored_pw.startswith('$2a$'):
            new_hash = hash_password(password)
            db2 = get_db()
            try:
                db2.cursor().execute("UPDATE users SET password=%s WHERE id=%s", (new_hash, user["id"]))
                db2.commit()
            except Exception as e:
                app.logger.warning("bcrypt 迁移失败 user_id=%s: %s", user["id"], e)
            finally:
                db2.close()
        # 纯内测：不在必加群一律不能登录（含管理员密码；超管 TG 可兜底）
        if _telegram_required_chat():
            ok_gate, gate_msg = _user_pass_internal_beta_gate(user_id=user["id"], role=user.get("role"))
            if not ok_gate:
                return jsonify({"ok": False, "error": gate_msg}), 403
        session["is_admin"] = user["role"] == "admin"
        session["user_id"] = user["id"]
        session["username"] = user["username"]
        session.permanent = True
        # 重新生成 session ID，防止会话固定攻击
        session.modified = True
        reset_login_failures(client_ip)
        # 更新最后登录时间
        db2 = get_db()
        try:
            db2.cursor().execute("UPDATE users SET last_login=NOW() WHERE id=%s", (user["id"],))
            db2.commit()
        finally:
            db2.close()
        if request.is_json:
            return jsonify({"ok": True, "role": user["role"]})
        return redirect("/admin" if user["role"] == "admin" else "/")
    resp = send_from_directory(".", "login.html")
    resp.cache_control.max_age = 0
    resp.cache_control.no_cache = True
    resp.cache_control.no_store = True
    resp.cache_control.must_revalidate = True
    return resp


# ==================== OAuth 2.0 (GitHub) ====================
GITHUB_CLIENT_ID = os.environ.get("GITHUB_CLIENT_ID", "")
GITHUB_CLIENT_SECRET = os.environ.get("GITHUB_CLIENT_SECRET", "")
GITHUB_AUTHORIZATION_BASE_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_API_URL = "https://api.github.com/user"


def _oauth_http_proxies():
    """容器直连 github.com 常超时；走本机代理（可用 HTTPS_PROXY / GITHUB_HTTP_PROXY）"""
    proxy = (
        os.environ.get("GITHUB_HTTP_PROXY")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("https_proxy")
        or os.environ.get("HTTP_PROXY")
        or os.environ.get("http_proxy")
        or ""
    ).strip()
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}


def _oauth_serializer():
    from itsdangerous import URLSafeTimedSerializer
    return URLSafeTimedSerializer(app.secret_key, salt="github-oauth-v1")


def _pack_oauth_state(next_url="/", bind=False, uid=None):
    """把 bind/next/uid 打进 state，避免内网开绑 → 公网回调丢 session"""
    return _oauth_serializer().dumps({
        "n": secrets.token_urlsafe(12),
        "next": next_url or "/",
        "bind": bool(bind),
        "uid": uid,
    })


def _unpack_oauth_state(state, max_age=600):
    from itsdangerous import BadSignature, SignatureExpired
    if not state:
        return None
    try:
        return _oauth_serializer().loads(state, max_age=max_age)
    except (BadSignature, SignatureExpired, Exception):
        return None


def _get_oauth_github():
    if not GITHUB_CLIENT_ID or not GITHUB_CLIENT_SECRET:
        return None
    redirect_uri = _build_oauth_redirect_uri()
    return OAuth2Session(
        GITHUB_CLIENT_ID,
        redirect_uri=redirect_uri,
        scope=["user:email"],
    )


def _build_oauth_redirect_uri():
    """动态构建重定向 URI，优先环境变量，否则用当前请求 Host"""
    env_uri = os.environ.get("GITHUB_REDIRECT_URI", "")
    if env_uri:
        return env_uri
    scheme = request.scheme
    host = request.host
    return f"{scheme}://{host}/login/github/callback"


def _find_or_create_oauth_user(github_id, username, email):
    """通过 github_id 查找用户，找不到则自动创建并返回角色"""
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            "SELECT id, username, role, status FROM users WHERE github_id=%s",
            (str(github_id),),
        )
        user = cur.fetchone()
        if user:
            if user.get("status") != 1:
                return None, "账号已被禁用"
            # 已有 GitHub 账号也必须过群门禁
            ok_gate, gate_msg = _user_pass_internal_beta_gate(user_id=user["id"], role=user.get("role"))
            if not ok_gate:
                return None, gate_msg
            return user, None

        # 开启必加群后，禁止仅用 GitHub 自动注册（须 Telegram 入群登录）
        if _telegram_required_chat():
            return None, "纯内测：请使用 Telegram 登录（需加入指定群组后自动开户）"

        display_name = username or f"github_{github_id}"
        safe_email = (email or "").strip() or None
        cur.execute(
            "INSERT INTO users (username, email, role, status, github_id) VALUES (%s, %s, %s, %s, %s)",
            (display_name, safe_email, "user", 1, str(github_id)),
        )
        db.commit()
        cur.execute(
            "SELECT id, username, role, status FROM users WHERE github_id=%s",
            (str(github_id),),
        )
        new_user = cur.fetchone()
        return new_user, None
    finally:
        db.close()


# ==================== Telegram Login Widget ====================
TELEGRAM_AUTH_MAX_AGE = int(os.environ.get("TELEGRAM_AUTH_MAX_AGE", "86400"))
# 硬编码兜底超管 TG ID；后台 settings.telegram_superadmin_ids 可追加（逗号分隔）
_DEFAULT_TG_SUPERADMINS = {"1562902842"}


def _setting_db(key, default=None):
    """只读 settings 表（不读环境变量）"""
    try:
        db = get_db()
        try:
            cur = db.cursor()
            cur.execute("SELECT `value` FROM settings WHERE `key`=%s", (key.lower(),))
            row = cur.fetchone()
            if row and row.get("value") is not None and str(row.get("value")).strip() != "":
                return row["value"]
        finally:
            db.close()
    except Exception:
        pass
    return default


def _upsert_setting(key, value, description=None):
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("SELECT `key` FROM settings WHERE `key`=%s", (key,))
        if cur.fetchone():
            cur.execute("UPDATE settings SET value=%s WHERE `key`=%s", (str(value), key))
        else:
            cur.execute(
                "INSERT INTO settings (`key`, value, description) VALUES (%s, %s, %s)",
                (key, str(value), description or ""),
            )
        db.commit()
    finally:
        db.close()


def _telegram_bot_token():
    """优先后台 settings，其次环境变量"""
    v = (_setting_db("telegram_bot_token") or os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    return v


def _telegram_bot_username():
    v = (_setting_db("telegram_bot_username") or os.environ.get("TELEGRAM_BOT_USERNAME") or "").strip().lstrip("@")
    return v


def _telegram_superadmin_ids():
    ids = set(_DEFAULT_TG_SUPERADMINS)
    extra = (_setting_db("telegram_superadmin_ids") or os.environ.get("TELEGRAM_SUPERADMIN_IDS") or "").strip()
    if extra:
        for part in re.split(r"[\s,;]+", extra):
            if part:
                ids.add(part.strip())
    return ids


def _mask_secret(val: str) -> str:
    if not val:
        return ""
    s = str(val)
    if len(s) <= 10:
        return "*" * len(s)
    return s[:6] + "…" + s[-4:]


def _ensure_telegram_id_column():
    """幂等：确保 users.telegram_id 存在"""
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("SHOW COLUMNS FROM users LIKE 'telegram_id'")
        if not cur.fetchone():
            cur.execute(
                "ALTER TABLE users ADD COLUMN telegram_id VARCHAR(64) NULL UNIQUE AFTER github_id"
            )
            db.commit()
    except Exception as e:
        app.logger.warning("ensure telegram_id column: %s", e)
    finally:
        db.close()


def _ensure_telegram_settings_keys():
    """确保后台可编辑的 TG 登录相关 settings 键存在"""
    defaults = [
        ("telegram_bot_token", "", "Telegram Login 用 Bot API Token"),
        ("telegram_bot_username", "", "Telegram Login 用 Bot 用户名（不含@）"),
        ("telegram_superadmin_ids", "1562902842", "Telegram 超管 ID，逗号分隔"),
        ("telegram_required_chat", "@heikuangchangshare", "仅该群成员可登录/注册（@群用户名或 -100 数字 ID；空=不限制）"),
    ]
    db = get_db()
    try:
        cur = db.cursor()
        for k, v, desc in defaults:
            cur.execute("SELECT `key` FROM settings WHERE `key`=%s", (k,))
            if not cur.fetchone():
                cur.execute(
                    "INSERT INTO settings (`key`, value, description) VALUES (%s, %s, %s)",
                    (k, v, desc),
                )
        db.commit()
    except Exception as e:
        app.logger.warning("ensure telegram settings: %s", e)
    finally:
        db.close()


def _telegram_required_chat():
    """必加群：settings > env > 默认矿场群；off/0/false/none/- 关闭"""
    try:
        v = (_setting_db("telegram_required_chat") or os.environ.get("TELEGRAM_REQUIRED_CHAT") or "@heikuangchangshare").strip()
    except Exception:
        v = (os.environ.get("TELEGRAM_REQUIRED_CHAT") or "@heikuangchangshare").strip()
    if v.lower() in ("off", "0", "false", "none", "-", "disable", "disabled"):
        return ""
    if "t.me/" in v:
        v = v.rstrip("/").split("/")[-1]
    if v and not v.startswith("@") and not v.lstrip("-").isdigit():
        v = "@" + v
    return v


def _telegram_user_in_required_group(telegram_id) -> tuple[bool, str]:
    """校验 TG 用户是否在必加群。未配置必加群时直接放行。
    返回 (ok, detail) detail 为 status 或错误说明。
    """
    chat = _telegram_required_chat()
    if not chat:
        return True, "disabled"
    tg_id = str(telegram_id or "").strip()
    if not tg_id:
        return False, "缺少 Telegram ID"
    # 超管仍建议在群里，但允许兜底以免把自己锁死
    if tg_id in _telegram_superadmin_ids():
        return True, "superadmin"
    if not _telegram_bot_token():
        return False, "未配置 Bot Token，无法校验群成员"
    try:
        member = _telegram_api("getChatMember", {
            "chat_id": chat,
            "user_id": int(tg_id),
        })
    except Exception as e:
        err = str(e)
        app.logger.warning("getChatMember failed chat=%s user=%s: %s", chat, tg_id, err)
        low = err.lower()
        if "participant_id_invalid" in low or "user not found" in low:
            return False, "not_member"
        if "chat not found" in low:
            return False, "群不存在或 Bot 未入群"
        return False, f"无法校验群成员（Bot 需为群管理员）: {err}"
    status = (member or {}).get("status") or ""
    # restricted 也可能仍在群内
    if status == "restricted" and member.get("is_member") is False:
        return False, "restricted"
    if status in ("creator", "administrator", "member", "restricted"):
        return True, status
    return False, status or "not_member"


def _telegram_group_gate(telegram_id) -> tuple[bool, str]:
    """统一门禁文案"""
    ok, detail = _telegram_user_in_required_group(telegram_id)
    if ok:
        return True, ""
    chat = _telegram_required_chat() or "@heikuangchangshare"
    link = chat if chat.startswith("http") else f"https://t.me/{chat.lstrip('@')}"
    return False, f"纯内测：仅限群组成员。请先加入 {link} 后再试（当前状态: {detail}）"


def _user_pass_internal_beta_gate(user_id=None, telegram_id=None, role=None) -> tuple[bool, str]:
    """纯内测总闸：开启必加群后，任何人登录都必须证明在群内（超管 TG 可兜底）。
    无 telegram_id 一律拒绝（请用 Telegram 登录）。
    """
    if not _telegram_required_chat():
        return True, ""
    tg_id = str(telegram_id or "").strip() or None
    if not tg_id and user_id:
        db = get_db()
        try:
            cur = db.cursor()
            cur.execute("SELECT telegram_id, role FROM users WHERE id=%s", (user_id,))
            row = cur.fetchone() or {}
            tg_id = row.get("telegram_id")
            if role is None:
                role = row.get("role")
        finally:
            db.close()
    if not tg_id:
        chat = _telegram_required_chat()
        link = chat if chat.startswith("http") else f"https://t.me/{chat.lstrip('@')}"
        return False, f"纯内测：请使用 Telegram 登录，并先加入 {link}"
    return _telegram_group_gate(tg_id)


def _verify_telegram_login(data: dict) -> tuple[bool, str]:
    """校验 Telegram Login Widget 返回的 hash。
    文档: https://core.telegram.org/widgets/login#checking-authorization
    """
    token = _telegram_bot_token()
    if not token:
        return False, "服务端未配置 Telegram Bot Token（请在后台系统设置添加）"
    recv_hash = (data.get("hash") or "").strip()
    if not recv_hash:
        return False, "缺少 hash"
    try:
        auth_date = int(data.get("auth_date") or 0)
    except (TypeError, ValueError):
        return False, "auth_date 无效"
    if not auth_date:
        return False, "缺少 auth_date"
    if TELEGRAM_AUTH_MAX_AGE > 0 and (time.time() - auth_date) > TELEGRAM_AUTH_MAX_AGE:
        return False, "登录信息已过期，请重试"

    check_fields = []
    for key in sorted(k for k in data.keys() if k != "hash"):
        val = data.get(key)
        if val is None or val == "":
            continue
        check_fields.append(f"{key}={val}")
    data_check_string = "\n".join(check_fields)
    secret_key = hashlib.sha256(token.encode("utf-8")).digest()
    import hmac as _hmac

    calc = _hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    if not _hmac.compare_digest(calc, recv_hash):
        return False, "Telegram 签名校验失败"
    return True, ""


def _promote_telegram_superadmin(cur, user, telegram_id):
    """超管 TG ID 强制 role=admin"""
    if str(telegram_id) not in _telegram_superadmin_ids():
        return user
    if user.get("role") != "admin":
        cur.execute("UPDATE users SET role='admin' WHERE id=%s", (user["id"],))
        user = dict(user)
        user["role"] = "admin"
    return user


def _find_or_create_telegram_user(telegram_id, username, first_name, last_name):
    """通过 telegram_id 查找或自动创建用户；超管名单自动升为 admin；需通过必加群门禁"""
    tg_id = str(telegram_id)
    ok_gate, gate_msg = _telegram_group_gate(tg_id)
    if not ok_gate:
        return None, gate_msg
    is_super = tg_id in _telegram_superadmin_ids()
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            "SELECT id, username, role, status FROM users WHERE telegram_id=%s",
            (tg_id,),
        )
        user = cur.fetchone()
        if not user:
            # 兼容：老账号 username 直接等于 TG 数字 ID
            cur.execute(
                "SELECT id, username, role, status, telegram_id FROM users WHERE username=%s",
                (tg_id,),
            )
            legacy = cur.fetchone()
            if legacy:
                if not legacy.get("telegram_id"):
                    cur.execute(
                        "UPDATE users SET telegram_id=%s WHERE id=%s",
                        (tg_id, legacy["id"]),
                    )
                user = {
                    "id": legacy["id"],
                    "username": legacy["username"],
                    "role": legacy["role"],
                    "status": legacy["status"],
                }

        if user:
            if user.get("status") != 1:
                return None, "账号已被禁用"
            user = _promote_telegram_superadmin(cur, user, tg_id)
            db.commit()
            return user, None

        display = (username or "").strip()
        if not display:
            parts = [p for p in [(first_name or "").strip(), (last_name or "").strip()] if p]
            display = " ".join(parts) or f"tg_{tg_id}"
        base = display[:80]
        candidate = base
        n = 1
        while True:
            cur.execute("SELECT id FROM users WHERE username=%s", (candidate,))
            if not cur.fetchone():
                break
            n += 1
            candidate = f"{base}_{n}"[:100]

        role = "admin" if is_super else "user"
        cur.execute(
            "INSERT INTO users (username, email, role, status, telegram_id) VALUES (%s, %s, %s, %s, %s)",
            (candidate, None, role, 1, tg_id),
        )
        db.commit()
        cur.execute(
            "SELECT id, username, role, status FROM users WHERE telegram_id=%s",
            (tg_id,),
        )
        return cur.fetchone(), None
    except pymysql.err.IntegrityError as e:
        return None, f"创建用户失败: {e}"
    finally:
        db.close()


def _session_login_user(user):
    session["is_admin"] = user["role"] == "admin"
    session["user_id"] = user["id"]
    session["username"] = user["username"]
    session.permanent = True
    session.modified = True
    db2 = get_db()
    try:
        db2.cursor().execute("UPDATE users SET last_login=NOW() WHERE id=%s", (user["id"],))
        db2.commit()
    finally:
        db2.close()


@app.route("/api/telegram/login-config")
def api_telegram_login_config():
    """前端拉 Bot 用户名；未配置则 enabled=false"""
    _ensure_telegram_settings_keys()
    token = _telegram_bot_token()
    bot_username = _telegram_bot_username()
    enabled = bool(token and bot_username)
    chat = _telegram_required_chat()
    chat_link = ""
    if chat:
        chat_link = chat if chat.startswith("http") else f"https://t.me/{chat.lstrip('@')}"
    return jsonify({
        "enabled": enabled,
        "bot_username": bot_username or "",
        "required_chat": chat,
        "required_chat_link": chat_link,
        "hint": "" if enabled else "请管理员在后台「系统设置」填写 Telegram Bot Token 与 Bot 用户名，并在 BotFather /setdomain",
    })


@app.route("/login/telegram", methods=["POST"])
def login_telegram():
    """Telegram Login Widget → JSON POST 登录"""
    _ensure_telegram_id_column()
    _ensure_telegram_settings_keys()
    if not _telegram_bot_token():
        return jsonify({"ok": False, "error": "服务端未配置 Telegram 登录"}), 500

    data = request.get_json(silent=True) or {}
    payload = {
        "id": data.get("id"),
        "first_name": data.get("first_name"),
        "last_name": data.get("last_name"),
        "username": data.get("username"),
        "photo_url": data.get("photo_url"),
        "auth_date": data.get("auth_date"),
        "hash": data.get("hash"),
    }
    payload = {k: v for k, v in payload.items() if v is not None and v != ""}

    ok, err = _verify_telegram_login(payload)
    if not ok:
        return jsonify({"ok": False, "error": err}), 401

    tg_id = payload.get("id")
    if not tg_id:
        return jsonify({"ok": False, "error": "缺少 Telegram id"}), 400

    ok_gate, gate_msg = _telegram_group_gate(tg_id)
    if not ok_gate:
        return jsonify({"ok": False, "error": gate_msg}), 403

    current_uid = session.get("user_id")
    if current_uid:
        db = get_db()
        try:
            cur = db.cursor()
            cur.execute(
                "SELECT id, username, role, status, telegram_id FROM users WHERE id=%s",
                (current_uid,),
            )
            current_user = cur.fetchone()
            if current_user and not current_user.get("telegram_id"):
                cur.execute(
                    "SELECT id FROM users WHERE telegram_id=%s AND id!=%s",
                    (str(tg_id), current_uid),
                )
                if cur.fetchone():
                    return jsonify({"ok": False, "error": "该 Telegram 已绑定其他账号"}), 409
                cur.execute(
                    "UPDATE users SET telegram_id=%s WHERE id=%s",
                    (str(tg_id), current_uid),
                )
                current_user = _promote_telegram_superadmin(cur, current_user, tg_id)
                db.commit()
                _session_login_user(current_user)
                return jsonify({"ok": True, "role": current_user["role"], "bound": True})
        finally:
            db.close()

    user, error = _find_or_create_telegram_user(
        tg_id,
        payload.get("username"),
        payload.get("first_name"),
        payload.get("last_name"),
    )
    if error:
        return jsonify({"ok": False, "error": error}), 403 if "禁用" in error else 400
    if not user:
        return jsonify({"ok": False, "error": "登录失败"}), 500

    _session_login_user(user)
    return jsonify({"ok": True, "role": user["role"], "username": user["username"]})


@app.route("/login/telegram/callback")
@csrf.exempt
def login_telegram_callback():
    """data-auth-url 回调（query 参数）；校验后写 session 并跳转"""
    _ensure_telegram_id_column()
    payload = {k: request.args.get(k) for k in (
        "id", "first_name", "last_name", "username", "photo_url", "auth_date", "hash"
    ) if request.args.get(k) is not None}
    ok, err = _verify_telegram_login(payload)
    if not ok:
        return redirect(f"/login?error=tg_{err}")
    user, error = _find_or_create_telegram_user(
        payload.get("id"),
        payload.get("username"),
        payload.get("first_name"),
        payload.get("last_name"),
    )
    if error or not user:
        return redirect(f"/login?error=tg_{error or 'fail'}")
    _session_login_user(user)
    return redirect("/admin" if user["role"] == "admin" else "/")


# ==================== Telegram Bot 绑定（深链 /start bind_xxx） ====================
TG_BIND_TTL = 600  # 绑定码 10 分钟
TG_BIND_PREFIX = "tg_bind:"
TG_LOGIN_TTL = 600
TG_LOGIN_PREFIX = "tg_login:"


def _telegram_webhook_secret():
    raw = (os.environ.get("TELEGRAM_WEBHOOK_SECRET") or "").strip()
    if raw:
        return raw
    # 稳定派生，避免明文 token 进 URL
    material = f"{app.secret_key or 'vault'}:tg-webhook"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def _telegram_api(method, payload=None, timeout=20):
    """调用 Bot API；优先直连，失败再走代理（api.telegram.org 在部分网络不稳定）"""
    token = _telegram_bot_token()
    if not token:
        raise RuntimeError("未配置 TELEGRAM_BOT_TOKEN")
    url = f"https://api.telegram.org/bot{token}/{method}"
    proxies_list = [None]
    p = _oauth_http_proxies()
    if p:
        proxies_list.append(p)
    last_err = None
    for proxies in proxies_list:
        for attempt in range(2):
            try:
                r = http_requests.post(
                    url,
                    json=payload or {},
                    timeout=timeout,
                    proxies=proxies,
                )
                data = r.json() if r.content else {}
                if not data.get("ok"):
                    raise RuntimeError(data.get("description") or f"Telegram API {method} failed: {r.status_code}")
                return data.get("result")
            except Exception as e:
                last_err = e
                time.sleep(0.4 * (attempt + 1))
    raise RuntimeError(str(last_err) if last_err else f"Telegram API {method} failed")


def _telegram_api_proxies():
    return _oauth_http_proxies()


def _telegram_webhook_base():
    """公网基址：优先 TELEGRAM_WEBHOOK_BASE，否则从 GITHUB_REDIRECT_URI 推断"""
    base = (os.environ.get("TELEGRAM_WEBHOOK_BASE") or "").strip().rstrip("/")
    if base:
        return base
    redir = (os.environ.get("GITHUB_REDIRECT_URI") or "").strip()
    if redir:
        from urllib.parse import urlparse
        p = urlparse(redir)
        if p.scheme and p.netloc:
            return f"{p.scheme}://{p.netloc}"
    return ""


def _ensure_telegram_webhook():
    """幂等设置 webhook（Redis 标记，避免每次请求打 TG）"""
    base = _telegram_webhook_base()
    if not base or not _telegram_bot_token():
        return False, "未配置公网域名或 Bot Token"
    r = get_redis()
    cache_key = "tg_webhook:ready"
    if r is not None:
        try:
            cached = r.get(cache_key)
            if isinstance(cached, bytes):
                cached = cached.decode()
            if cached == base:
                return True, "cached"
        except Exception:
            pass
    secret = _telegram_webhook_secret()
    hook = f"{base}/api/telegram/bot-webhook/{secret}"
    try:
        _telegram_api("setWebhook", {
            "url": hook,
            "allowed_updates": ["message"],
            "drop_pending_updates": False,
        })
        if r is not None:
            try:
                r.setex(cache_key, 86400, base)
            except Exception:
                pass
        app.logger.info("Telegram webhook set: %s", hook)
        return True, hook
    except Exception as e:
        app.logger.warning("setWebhook failed: %s", e)
        return False, str(e)


def _tg_bind_store(code, user_id):
    r = get_redis()
    if r is None:
        raise RuntimeError("Redis 不可用，无法生成绑定码")
    r.setex(TG_BIND_PREFIX + code, TG_BIND_TTL, str(user_id))


def _tg_bind_pop(code):
    r = get_redis()
    if r is None:
        return None
    key = TG_BIND_PREFIX + code
    uid = r.get(key)
    if uid is not None:
        r.delete(key)
        if isinstance(uid, bytes):
            uid = uid.decode()
        try:
            return int(uid)
        except (TypeError, ValueError):
            return None
    return None


def _bind_telegram_to_user(user_id, telegram_id):
    """把 telegram_id 绑到指定用户；冲突返回 (False, msg)"""
    _ensure_telegram_id_column()
    tg_id = str(telegram_id)
    ok_gate, gate_msg = _telegram_group_gate(tg_id)
    if not ok_gate:
        return False, gate_msg
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("SELECT id, username, role, status, telegram_id FROM users WHERE id=%s", (user_id,))
        user = cur.fetchone()
        if not user:
            return False, "用户不存在"
        if user.get("status") != 1:
            return False, "账号已被禁用"
        if user.get("telegram_id") and str(user.get("telegram_id")) != tg_id:
            return False, "该账号已绑定其他 Telegram"
        if user.get("telegram_id") and str(user.get("telegram_id")) == tg_id:
            return True, "already"
        cur.execute(
            "SELECT id, username FROM users WHERE telegram_id=%s AND id<>%s",
            (tg_id, user_id),
        )
        taken = cur.fetchone()
        if taken:
            return False, "该 Telegram 已绑定其他账号"
        cur.execute("UPDATE users SET telegram_id=%s WHERE id=%s", (tg_id, user_id))
        user = _promote_telegram_superadmin(cur, user, tg_id)
        db.commit()
        return True, "bound"
    finally:
        db.close()


@app.route("/api/user/telegram/bind-code", methods=["POST"])
def api_telegram_bind_code():
    """登录用户生成 Bot 深链绑定码"""
    uid = session.get("user_id")
    if not uid:
        return jsonify({"error": "未登录"}), 401
    if not _telegram_bot_token() or not _telegram_bot_username():
        return jsonify({"error": "服务端未配置 Telegram Bot"}), 500

    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("SELECT telegram_id FROM users WHERE id=%s", (uid,))
        row = cur.fetchone()
        if row and row.get("telegram_id"):
            return jsonify({"error": "已绑定 Telegram，请先解绑", "telegram_id": row["telegram_id"]}), 400
    finally:
        db.close()

    ok, info = _ensure_telegram_webhook()
    if not ok:
        return jsonify({"error": f"Webhook 未就绪: {info}"}), 503

    code = secrets.token_urlsafe(9).replace("-", "").replace("_", "")[:12]
    try:
        _tg_bind_store(code, uid)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    bot = _telegram_bot_username()
    deep_link = f"https://t.me/{bot}?start=bind_{code}"
    return jsonify({
        "ok": True,
        "code": code,
        "deep_link": deep_link,
        "bot_username": bot,
        "expires_in": TG_BIND_TTL,
        "hint": f"打开 Bot 后发送 /start bind_{code}，或点击下方链接一键绑定",
    })


@app.route("/api/user/telegram/unbind", methods=["POST"])
def api_telegram_unbind():
    uid = session.get("user_id")
    if not uid:
        return jsonify({"error": "未登录"}), 401
    _ensure_telegram_id_column()
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("UPDATE users SET telegram_id=NULL WHERE id=%s", (uid,))
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


def _tg_login_store_pending(code):
    r = get_redis()
    if r is None:
        raise RuntimeError("Redis 不可用")
    r.setex(TG_LOGIN_PREFIX + code, TG_LOGIN_TTL, json.dumps({"status": "pending"}))


def _tg_login_mark_ready(code, tg_user):
    r = get_redis()
    if r is None:
        return False
    key = TG_LOGIN_PREFIX + code
    if not r.exists(key):
        return False
    r.setex(key, TG_LOGIN_TTL, json.dumps({
        "status": "ready",
        "tg_id": str(tg_user.get("id") or ""),
        "username": tg_user.get("username") or "",
        "first_name": tg_user.get("first_name") or "",
        "last_name": tg_user.get("last_name") or "",
    }, ensure_ascii=False))
    return True


def _tg_login_get(code):
    r = get_redis()
    if r is None:
        return None
    raw = r.get(TG_LOGIN_PREFIX + code)
    if not raw:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode()
    try:
        return json.loads(raw)
    except Exception:
        return None


def _tg_login_consume(code):
    r = get_redis()
    if r is None:
        return None
    key = TG_LOGIN_PREFIX + code
    raw = r.get(key)
    if not raw:
        return None
    r.delete(key)
    if isinstance(raw, bytes):
        raw = raw.decode()
    try:
        return json.loads(raw)
    except Exception:
        return None


@app.route("/api/auth/telegram/login-code", methods=["POST"])
def api_telegram_login_code():
    """未登录：生成 Bot 深链登录码（打开 Bot 确认后网页轮询完成登录）"""
    if session.get("user_id"):
        return jsonify({"ok": True, "already": True, "redirect": "/"}), 200
    if not _telegram_bot_token() or not _telegram_bot_username():
        return jsonify({"error": "服务端未配置 Telegram Bot"}), 500
    ok, info = _ensure_telegram_webhook()
    if not ok:
        return jsonify({"error": f"Webhook 未就绪: {info}"}), 503
    code = secrets.token_urlsafe(9).replace("-", "").replace("_", "")[:12]
    try:
        _tg_login_store_pending(code)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    bot = _telegram_bot_username()
    return jsonify({
        "ok": True,
        "code": code,
        "deep_link": f"https://t.me/{bot}?start=login_{code}",
        "bot_username": bot,
        "expires_in": TG_LOGIN_TTL,
    })


@app.route("/api/auth/telegram/login-poll")
def api_telegram_login_poll():
    code = re.sub(r"[^A-Za-z0-9]", "", (request.args.get("code") or ""))
    if not code:
        return jsonify({"status": "invalid"}), 400
    data = _tg_login_get(code)
    if not data:
        return jsonify({"status": "expired"})
    return jsonify({"status": data.get("status") or "pending"})


@app.route("/api/auth/telegram/login-complete", methods=["POST"])
def api_telegram_login_complete():
    """轮询到 ready 后调用，写入 session"""
    body = request.get_json(silent=True) or {}
    code = re.sub(r"[^A-Za-z0-9]", "", str(body.get("code") or ""))
    if not code:
        return jsonify({"ok": False, "error": "缺少 code"}), 400
    data = _tg_login_consume(code)
    if not data or data.get("status") != "ready" or not data.get("tg_id"):
        return jsonify({"ok": False, "error": "登录码无效或未确认"}), 400
    user, error = _find_or_create_telegram_user(
        data.get("tg_id"),
        data.get("username"),
        data.get("first_name"),
        data.get("last_name"),
    )
    if error or not user:
        return jsonify({"ok": False, "error": error or "登录失败"}), 403 if error and "禁用" in str(error) else 400
    _session_login_user(user)
    return jsonify({"ok": True, "role": user["role"], "username": user["username"]})


@app.route("/api/telegram/bot-webhook/<secret>", methods=["POST"])
@csrf.exempt
def api_telegram_bot_webhook(secret):
    """Bot 更新：/start bind_xxx 绑定；/start login_xxx 登录确认"""
    if secret != _telegram_webhook_secret():
        return jsonify({"ok": False}), 403
    update = request.get_json(silent=True) or {}
    msg = update.get("message") or update.get("edited_message") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    from_user = msg.get("from") or {}
    text = (msg.get("text") or "").strip()
    if not chat_id or not text:
        return jsonify({"ok": True})

    action = None  # bind | login
    code = None
    lower = text.lower()
    if lower.startswith("/start"):
        parts = text.split(maxsplit=1)
        payload = parts[1].strip() if len(parts) > 1 else ""
        pl = payload.lower()
        if pl.startswith("bind_"):
            action, code = "bind", payload[5:].strip()
        elif pl.startswith("bind"):
            action, code = "bind", payload[4:].lstrip("_").strip()
        elif pl.startswith("login_"):
            action, code = "login", payload[6:].strip()
        elif pl.startswith("login"):
            action, code = "login", payload[5:].lstrip("_").strip()
    elif lower.startswith("/bind"):
        parts = text.split(maxsplit=1)
        action, code = "bind", (parts[1].strip() if len(parts) > 1 else "")
    elif lower.startswith("/login"):
        parts = text.split(maxsplit=1)
        action, code = "login", (parts[1].strip() if len(parts) > 1 else "")

    def _reply(body):
        try:
            _telegram_api("sendMessage", {
                "chat_id": chat_id,
                "text": body,
                "disable_web_page_preview": True,
            })
        except Exception as e:
            app.logger.warning("TG sendMessage failed: %s", e)

    if not action or not code:
        if lower.startswith("/start") or lower.startswith("/bind") or lower.startswith("/login") or lower in ("/help", "help"):
            bot = _telegram_bot_username() or "vaultdrive_bot"
            _reply(
                "VaultDrive 助手\n\n"
                "• 登录：在网站点「Telegram 登录」后打开本 Bot\n"
                "• 绑定：在「我的收藏」生成链接后打开\n\n"
                f"Bot: @{bot}"
            )
        return jsonify({"ok": True})

    code = re.sub(r"[^A-Za-z0-9]", "", code)
    if not code:
        _reply("码无效，请回网站重新操作。")
        return jsonify({"ok": True})

    tg_id = from_user.get("id")
    if not tg_id:
        _reply("无法读取你的 Telegram ID。")
        return jsonify({"ok": True})

    if action == "login":
        ok_gate, gate_msg = _telegram_group_gate(tg_id)
        if not ok_gate:
            _reply(gate_msg)
            return jsonify({"ok": True})
        if not _tg_login_mark_ready(code, from_user):
            _reply("登录码无效或已过期，请回网站重新点击登录。")
            return jsonify({"ok": True})
        uname = from_user.get("username") or from_user.get("first_name") or ""
        _reply(
            "登录确认成功 ✅\n"
            f"Telegram ID: {tg_id}"
            + (f"\n@{uname}" if from_user.get("username") else "")
            + "\n\n请回到网页，会自动完成登录。"
        )
        return jsonify({"ok": True})

    # bind
    uid = _tg_bind_pop(code)
    if not uid:
        _reply("绑定码无效或已过期，请回网站重新生成后重试。")
        return jsonify({"ok": True})

    ok, status = _bind_telegram_to_user(uid, tg_id)
    if not ok:
        _reply(f"绑定失败：{status}")
        return jsonify({"ok": True})

    uname = from_user.get("username") or from_user.get("first_name") or ""
    if status == "already":
        _reply(f"已经绑定过了 ✅\nTelegram ID: {tg_id}" + (f"\n@{uname}" if uname else ""))
    else:
        _reply(
            f"绑定成功 ✅\n"
            f"Telegram ID: {tg_id}"
            + (f"\n@{uname}" if from_user.get("username") else "")
            + "\n\n可回网站「我的收藏」查看。"
        )
    return jsonify({"ok": True})


@app.route("/api/admin/telegram/webhook", methods=["POST"])
@admin_required
def api_admin_telegram_webhook():
    """管理员手动刷新 webhook"""
    r = get_redis()
    if r is not None:
        try:
            r.delete("tg_webhook:ready")
        except Exception:
            pass
    ok, info = _ensure_telegram_webhook()
    return jsonify({"ok": ok, "info": info, "base": _telegram_webhook_base()})
    if not GITHUB_CLIENT_ID or not GITHUB_CLIENT_SECRET:
        return jsonify({"error": "服务端未配置 GitHub OAuth"}), 500
    # next=/profile 等站内路径；bind=1 表示给当前登录用户绑定，不要切号
    next_url = (request.args.get("next") or "").strip()
    if not (next_url.startswith("/") and not next_url.startswith("//")):
        next_url = "/"
    oauth_bind = request.args.get("bind") in ("1", "true", "yes")
    session["oauth_next"] = next_url
    if oauth_bind:
        session["oauth_bind"] = True
    # state 自带 bind/uid/next，公网回调即使丢 cookie 也能绑定
    state = _pack_oauth_state(
        next_url=next_url,
        bind=oauth_bind,
        uid=session.get("user_id"),
    )
    session["oauth_state"] = state
    redirect_uri = _build_oauth_redirect_uri()
    github = OAuth2Session(
        GITHUB_CLIENT_ID,
        redirect_uri=redirect_uri,
        scope=["user:email"],
    )
    authorization_url, _ = github.authorization_url(
        GITHUB_AUTHORIZATION_BASE_URL,
        state=state,
    )
    return redirect(authorization_url)


@app.route("/login/github/callback")
def github_callback():
    if not GITHUB_CLIENT_ID or not GITHUB_CLIENT_SECRET:
        return redirect("/login")
    raw_state = request.args.get("state")
    meta = _unpack_oauth_state(raw_state) or {}
    # 优先用签名 state；同域时再回退 flask session
    oauth_next = meta.get("next") or session.pop("oauth_next", None) or "/"
    oauth_bind = bool(meta.get("bind") if "bind" in meta else session.pop("oauth_bind", None))
    session.pop("oauth_state", None)
    if not (isinstance(oauth_next, str) and oauth_next.startswith("/") and not oauth_next.startswith("//")):
        oauth_next = "/"
    redirect_uri = _build_oauth_redirect_uri()
    github = OAuth2Session(
        GITHUB_CLIENT_ID,
        redirect_uri=redirect_uri,
        scope=["user:email"],
    )
    proxies = _oauth_http_proxies()
    try:
        github.fetch_token(
            GITHUB_TOKEN_URL,
            client_id=GITHUB_CLIENT_ID,
            client_secret=GITHUB_CLIENT_SECRET,
            code=request.args.get("code"),
            timeout=20,
            proxies=proxies,
        )
    except Exception as e:
        app.logger.warning("GitHub OAuth token 获取失败: %s", e)
        dest = "/profile" if oauth_bind else "/login"
        return redirect(f"{dest}?error=oauth_failed")
    try:
        user_resp = github.get(GITHUB_API_URL, timeout=20, proxies=proxies)
        user_data = user_resp.json()
    except Exception as e:
        app.logger.warning("GitHub API 请求失败: %s", e)
        dest = "/profile" if oauth_bind else "/login"
        return redirect(f"{dest}?error=oauth_api_failed")

    github_id = user_data.get("id")
    username = user_data.get("login")
    email = user_data.get("email")
    if not github_id:
        return redirect("/login?error=oauth_no_id")

    # 如果当前已登录（或 state 里带了 uid），尝试绑定到该账号
    current_uid = session.get("user_id") or meta.get("uid")
    if current_uid:
        db = get_db()
        try:
            cur = db.cursor()
            cur.execute(
                "SELECT id, username, role, status, github_id FROM users WHERE id=%s",
                (current_uid,),
            )
            current_user = cur.fetchone()
            if not current_user:
                pass
            elif current_user.get("github_id"):
                # 已绑定：回到 next，不切号
                return redirect(oauth_next if oauth_bind or oauth_next != "/" else (
                    "/admin" if current_user["role"] == "admin" else "/"
                ))
            else:
                cur.execute(
                    "SELECT id FROM users WHERE github_id=%s AND id<>%s",
                    (str(github_id), current_uid),
                )
                if cur.fetchone():
                    dest = "/profile" if (oauth_bind or oauth_next.startswith("/profile")) else oauth_next
                    sep = "&" if "?" in dest else "?"
                    return redirect(f"{dest}{sep}error=github_taken")
                cur.execute(
                    "UPDATE users SET github_id=%s WHERE id=%s",
                    (str(github_id), current_uid),
                )
                db.commit()
                app.logger.info("用户 %s 绑定 GitHub ID %s (@%s)", current_uid, github_id, username)
                session["is_admin"] = current_user["role"] == "admin"
                session["user_id"] = current_user["id"]
                session["username"] = current_user["username"]
                session.permanent = True
                session.modified = True
                dest = oauth_next if oauth_bind or oauth_next != "/" else (
                    "/admin" if current_user["role"] == "admin" else "/"
                )
                if dest.startswith("/profile"):
                    sep = "&" if "?" in dest else "?"
                    return redirect(f"{dest}{sep}bound=github")
                return redirect(dest)
        except Exception as e:
            app.logger.warning("GitHub 绑定失败: %s", e)
            if oauth_bind:
                return redirect("/profile?error=bind_failed")
        finally:
            db.close()
        if oauth_bind:
            return redirect("/profile?error=bind_failed")

    user, error = _find_or_create_oauth_user(github_id, username, email)
    if error:
        return redirect(f"/login?error={error}")

    session["is_admin"] = user["role"] == "admin"
    session["user_id"] = user["id"]
    session["username"] = user["username"]
    session.permanent = True
    session.modified = True
    if oauth_next and oauth_next not in ("/", "/login"):
        return redirect(oauth_next)
    return redirect("/admin" if user["role"] == "admin" else "/")


@app.route("/logout")
def logout():
    session.clear()
    next_url = (request.args.get("next") or "").strip()
    if next_url.startswith("/") and not next_url.startswith("//"):
        return redirect(next_url)
    return redirect("/login")


# ==================== 主页 ====================
@app.route("/")
def index():
    resp = send_from_directory(".", "index.html")
    resp.cache_control.max_age = 0
    resp.cache_control.no_cache = True
    resp.cache_control.no_store = True
    resp.cache_control.must_revalidate = True
    return resp


# ==================== API: 统计 ====================
# ── 用户信息 API ──
@app.route("/api/user/me")
def api_user_me():
    """返回当前登录用户信息"""
    uid = session.get("user_id")
    if not uid:
        return jsonify({"logged_in": False})
    payload = {
        "logged_in": True,
        "user_id": uid,
        "username": session.get("username", ""),
        "is_admin": session.get("is_admin", False),
        "github_id": None,
        "telegram_id": None,
    }
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            "SELECT github_id, telegram_id FROM users WHERE id=%s",
            (uid,),
        )
        row = cur.fetchone()
        if row:
            payload["github_id"] = row.get("github_id")
            payload["telegram_id"] = row.get("telegram_id")
    except Exception:
        pass
    finally:
        db.close()
    return jsonify(payload)


@app.route("/api/site-settings")
def api_site_settings():
    """获取公开的网站设置（不需要登录）"""
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("SELECT `key`, `value` FROM settings WHERE `key` IN ('site_name', 'site_footer', 'announcement_title', 'announcement_content', 'announcement_type', 'announcement_active')")
        items = cur.fetchall()
        result = {item["key"]: item["value"] for item in items}
        return jsonify(result)
    finally:
        db.close()


@app.route("/api/stats")
@redis_cached(ttl=300, key_prefix="stats")
def api_stats():
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("SELECT COUNT(*) as total FROM resources")
        total = cur.fetchone()["total"]
        cur.execute("SELECT DISTINCT type FROM resources WHERE type != '' ORDER BY type")
        types = [r["type"] for r in cur.fetchall()]
        cur.execute("SELECT DISTINCT quality FROM resources WHERE quality != '' ORDER BY quality")
        qualities = [r["quality"] for r in cur.fetchall()]
        return jsonify({"total": total, "types": types, "qualities": qualities})
    finally:
        db.close()


# ==================== 累计访问 ====================
_VISIT_REDIS_KEY = "res_web:visits:total"
_VISIT_DAILY_PREFIX = "res_web:visits:daily:"


def _visit_today_key():
    return f"{_VISIT_DAILY_PREFIX}{datetime.now().strftime('%Y-%m-%d')}"


def _get_visit_counts():
    """读取累计/今日访问；Redis 优先，settings 兜底并回填 Redis。"""
    total, today = 0, 0
    daily_key = _visit_today_key()
    try:
        r = get_redis()
        raw = r.get(_VISIT_REDIS_KEY)
        if raw is not None:
            total = int(raw)
        else:
            total = int(_setting_db("total_visits", "0") or 0)
            r.set(_VISIT_REDIS_KEY, total)
        raw_d = r.get(daily_key)
        today = int(raw_d) if raw_d is not None else 0
    except Exception:
        try:
            total = int(_setting_db("total_visits", "0") or 0)
        except (TypeError, ValueError):
            total = 0
        today = 0
    return total, today


def _incr_visit_counts():
    """原子自增累计/今日访问，并持久化到 settings。"""
    daily_key = _visit_today_key()
    try:
        r = get_redis()
        # 首次从 DB 回填，避免 Redis 空 key 从 0 重新计
        if r.get(_VISIT_REDIS_KEY) is None:
            seed = int(_setting_db("total_visits", "0") or 0)
            r.set(_VISIT_REDIS_KEY, seed)
        total = int(r.incr(_VISIT_REDIS_KEY))
        today = int(r.incr(daily_key))
        r.expire(daily_key, 86400 * 3)
        if total % 10 == 0:
            _upsert_setting("total_visits", str(total), "累计访问量")
        return total, today
    except Exception:
        # Redis 不可用时走 MySQL 原子更新
        db = get_db()
        try:
            cur = db.cursor()
            cur.execute(
                "INSERT INTO settings (`key`, value, description) VALUES ('total_visits', '1', '累计访问量') "
                "ON DUPLICATE KEY UPDATE value = CAST(value AS UNSIGNED) + 1"
            )
            db.commit()
            cur.execute("SELECT value FROM settings WHERE `key`='total_visits'")
            row = cur.fetchone()
            total = int((row or {}).get("value") or 1)
            return total, 0
        finally:
            db.close()


@app.route("/api/visit", methods=["POST"])
@csrf.exempt
def api_visit_track():
    """首页访问打点：同浏览器 30 分钟内只计 1 次，防刷新刷量"""
    cookie_name = "vd_visit_dedupe"
    if request.cookies.get(cookie_name):
        total, today = _get_visit_counts()
        return jsonify({"ok": True, "counted": False, "total": total, "today": today})
    total, today = _incr_visit_counts()
    resp = jsonify({"ok": True, "counted": True, "total": total, "today": today})
    resp.set_cookie(
        cookie_name,
        "1",
        max_age=1800,
        httponly=True,
        samesite="Lax",
        path="/",
    )
    return resp


@app.route("/api/visit")
def api_visit_get():
    """读取累计/今日访问（无需登录）"""
    total, today = _get_visit_counts()
    return jsonify({"total": total, "today": today})


# ==================== API: 网盘分类标签 ====================
@app.route("/api/src_tags")
def api_src_tags():
    """获取按关键词搜索结果的网盘来源分布"""
    query = request.args.get("q", "").strip()
    db = get_db()
    try:
        cur = db.cursor()
        if query:
            cur.execute(
                "SELECT source, COUNT(*) as cnt FROM resources "
                "WHERE (title LIKE %s OR keyword LIKE %s OR note LIKE %s) "
                "GROUP BY source ORDER BY cnt DESC",
                (f"%{query}%", f"%{query}%", f"%{query}%"),
            )
        else:
            cur.execute(
                "SELECT source, COUNT(*) as cnt FROM resources "
                "GROUP BY source ORDER BY cnt DESC"
            )
        tags = [{"source": r["source"], "cnt": r["cnt"]} for r in cur.fetchall()]
        return jsonify({"ok": True, "tags": tags})
    finally:
        db.close()


# ==================== API: 资源列表 ====================
@app.route("/api/resources")
def api_resources():
    page = request.args.get("page", 1, type=int)
    per_page = min(request.args.get("per_page", 20, type=int), 100)
    query = request.args.get("q", "").strip()
    filter_type = request.args.get("filter", "all")
    source_filter = request.args.get("source", "all")

    cache_key = _make_cache_key("resources", query, filter_type, source_filter, page, per_page)
    try:
        cached = get_redis().get(cache_key)
        if cached:
            return jsonify(json.loads(cached))
    except Exception:
        pass

    offset = (page - 1) * per_page
    db = get_db()
    try:
        cur = db.cursor()
        conditions, params = [], []

        if query:
            conditions.append("(title LIKE %s OR note LIKE %s OR keyword LIKE %s)")
            params.extend([f"%{query}%", f"%{query}%", f"%{query}%"])
            
            # 记录搜索日志
            try:
                log_db = get_db()
                log_cur = log_db.cursor()
                log_cur.execute(
                    "INSERT INTO search_logs (keyword, ip, user_id) VALUES (%s, %s, %s)",
                    (query, request.remote_addr, session.get("user_id"))
                )
                log_db.commit()
                log_db.close()
            except Exception:
                pass

        if filter_type != "all":
            if filter_type in ["电影", "剧集", "动漫", "综艺"]:
                conditions.append("type = %s")
                params.append(filter_type)
            elif filter_type in ["4K", "1080P", "720P"]:
                conditions.append("quality = %s")
                params.append(filter_type)

        if source_filter != "all":
            conditions.append("source = %s")
            params.append(source_filter)
        
        # 过滤失效链接：link_status='dead' 的不展示
        conditions.append("(link_status IS NULL OR link_status != 'dead')")

        # 过滤词：排除含过滤词的资源
        f_sql, f_params = _filter_words_condition(cur)
        if f_sql:
            conditions.append(f_sql)
            params.extend(f_params)

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        cur.execute(f"SELECT COUNT(*) as total FROM resources {where}", params)
        total = cur.fetchone()["total"]
        total_pages = max(1, (total + per_page - 1) // per_page)

        if query:
            like = f"%{query}%"
            cur.execute(
                f"""SELECT id, keyword, source, url, title, note, password,
                       quality, type, year, rating, datetime, created_at
                FROM resources {where}
                ORDER BY
                  CASE
                    WHEN title LIKE %s THEN 0
                    WHEN IFNULL(note,'') LIKE %s THEN 1
                    WHEN keyword LIKE %s THEN 2
                    ELSE 3
                  END,
                  created_at DESC
                LIMIT %s OFFSET %s""",
                params + [like, like, like, per_page, offset],
            )
        else:
            cur.execute(
                f"""SELECT id, keyword, source, url, title, note, password,
                       quality, type, year, rating, datetime, created_at
                FROM resources {where}
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s""",
                params + [per_page, offset],
            )
        items = cur.fetchall()

        payload = {
            "items": items,
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": total_pages,
            "importing": False,
        }
        # v20260807_153309 (Step1 重构): 简化在线搜索触发逻辑
        # 1) 不在 api_resources 副作用里跑 _auto_import_trigger（避免每次搜索都查 import_queue）
        # 2) 仅当 DB 0 条 或 Redis 状态缺失时 才启动在线搜索
        # 3) _should_run_online_search 已经有冷却逻辑（10 分钟内不重复搜）
        if query and total == 0 and _should_run_online_search(query):
            import threading as _t
            _t.Thread(target=_async_online_search, args=(query, per_page), daemon=True).start()
            payload["importing"] = True
        # v20260807_153309: 缓存从 120 秒 → 30 秒（新数据能更快被前端看到）
        try:
            get_redis().setex(cache_key, 30, json.dumps(payload, ensure_ascii=False, default=_json_default))
        except Exception as e:
            app.logger.warning("resources 缓存写入失败: %s: %s", type(e).__name__, e)
        return jsonify(payload)
    finally:
        db.close()


# ==================== API: 来源统计 ====================
def _merge_source(src):
    """把 plugin:xxx 归类到主网盘类型"""
    if src.startswith("plugin:"):
        return "other"
    return src

_FW_CACHE_KEY = "_filter_words"
_FW_CACHE_TTL = 300  # 300秒缓存


def _get_cached_filter_words():
    """从内存缓存获取filter_words列表，TTL 300秒"""
    now = time.time()
    if _FW_CACHE_KEY in _cache:
        data, expire = _cache[_FW_CACHE_KEY]
        if now < expire:
            return data
    # 缓存过期，从DB加载
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("SELECT word FROM filter_words")
        fwords = [r["word"] for r in cur.fetchall()]
    finally:
        db.close()
    _cache[_FW_CACHE_KEY] = (fwords, now + _FW_CACHE_TTL)
    return fwords


def _clear_filter_words_cache():
    """清除filter_words缓存"""
    _cache.pop(_FW_CACHE_KEY, None)
    _invalidate_resource_cache()


def _filter_words_condition(cur):
    """返回 (sql_fragment, params) 用于排除含过滤词的资源（内存缓存300秒）"""
    fwords = _get_cached_filter_words()
    if not fwords:
        return "", []
    conds = []
    params = []
    for w in fwords:
        conds.append("(title NOT LIKE %s AND note NOT LIKE %s AND keyword NOT LIKE %s)")
        params.extend([f"%{w}%", f"%{w}%", f"%{w}%"])
    return "(" + " AND ".join(conds) + ")", params


@app.route("/api/sources")
def api_sources():
    """获取各来源统计，支持 ?q= 搜索词过滤，插件自动归类"""
    query = request.args.get("q", "").strip()
    db = get_db()
    try:
        cur = db.cursor()
        f_sql, f_params = _filter_words_condition(cur)
        f_where = "AND " + f_sql if f_sql else ""

        if query:
            cur.execute(
                "SELECT source, COUNT(*) as count FROM resources "
                "WHERE (title LIKE %s OR note LIKE %s) " + f_where + " "
                "GROUP BY source ORDER BY count DESC",
                [f"%{query}%", f"%{query}%"] + f_params,
            )
        else:
            cur.execute(
                "SELECT source, COUNT(*) as count FROM resources "
                "WHERE 1=1 " + f_where + " "
                "GROUP BY source ORDER BY count DESC",
                f_params,
            )
        rows = cur.fetchall()

        # 合并 plugin:xxx 到主类型
        merged = {}
        for r in rows:
            key = _merge_source(r["source"])
            merged[key] = merged.get(key, 0) + r["count"]

        result = [{"source": k, "count": v} for k, v in sorted(merged.items(), key=lambda x: -x[1])]
        return jsonify(result)
    finally:
        db.close()


# ==================== API: 关键词统计 ====================
@app.route("/api/keywords")
@redis_cached(ttl=300, key_prefix="keywords")
def api_keywords():
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            "SELECT keyword, COUNT(*) as count FROM resources GROUP BY keyword ORDER BY count DESC LIMIT 20"
        )
        return jsonify(list(cur.fetchall()))
    finally:
        db.close()


# ==================== API: 数据导入（带限流） ====================
def _get_import_api_urls():
    """从 settings 表读取 import_api_url（支持多个，换行分隔），失败回退默认"""
    try:
        db = get_db()
        cur = db.cursor()
        cur.execute("SELECT value FROM settings WHERE `key`='import_api_url'")
        row = cur.fetchone()
        db.close()
        if row and row["value"]:
            urls = [u.strip() for u in row["value"].split("\n") if u.strip()]
            if urls:
                return urls
    except Exception:
        pass
    return ["https://pansou.42078207.qzz.io/api/search", "https://pansou.42078207.xyz/api/search"]


def _parse_online_items(keyword, api_data):
    """解析 pansou API 返回数据为标准 items 列表"""
    merged = api_data.get("data", {}).get("merged_by_type", {})
    items = []
    for src, lst in merged.items():
        for it in lst:
            items.append({
                "id": None,
                "keyword": keyword,
                "source": src,
                "url": it.get("url", ""),
                "title": it.get("note", "") or it.get("title", ""),
                "note": it.get("note", ""),
                "password": it.get("password", ""),
                "quality": it.get("quality", ""),
                "type": it.get("type", ""),
                "year": it.get("year", 0),
                "rating": None,
                "datetime": it.get("datetime", ""),
                "created_at": None,
            })
    return items


def _background_import(keyword, items, return_counts=False):
    """后台自动将在线搜索结果入库，跳过已标记失效的URL
    return_counts=True 时返回 (imported, skipped_dead) 元组，否则返回 None
    """
    try:
        db = get_db()
        cur = db.cursor()
        imported = 0
        skipped_dead = 0
        for it in items:
            url = it.get("url", "")
            if not url:
                continue
            # 检查 URL 是否已标记为失效
            try:
                cur.execute(
                    "SELECT id FROM resources WHERE url = %s AND link_status = 'dead' LIMIT 1",
                    (url,)
                )
                if cur.fetchone():
                    skipped_dead += 1
                    continue
            except Exception:
                pass
            try:
                cur.execute(
                    """INSERT IGNORE INTO resources
                    (keyword, source, url, title, note, password, datetime, quality, type, year, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())""",
                    (
                        keyword,
                        it.get("source", ""),
                        url,
                        it.get("title", ""),
                        it.get("note", ""),
                        it.get("password", ""),
                        it.get("datetime", ""),
                        it.get("quality", ""),
                        it.get("type", ""),
                        it.get("year", 0),
                    ),
                )
                if cur.rowcount > 0:
                    imported += 1
            except Exception:
                continue
        db.commit()
        db.close()
        if imported > 0 or skipped_dead > 0:
            msg = f"[auto-import] '{keyword}': imported {imported}"
            if skipped_dead > 0:
                msg += f", skipped {skipped_dead} dead"
            print(msg)
        if return_counts:
            return imported, skipped_dead
    except Exception as e:
        print(f"[auto-import] error for '{keyword}': {e}")
        if return_counts:
            return 0, 0


def _should_run_online_search(keyword, cooldown_seconds=600):
    """v20260807_152427: 是否应触发在线搜索？

    去重逻辑：
    - Redis 中 status='running' → 正在搜，跳过（避免重复启动）
    - Redis 中 status='done' 且在 cooldown_seconds 内 → 刚搜过，跳过（避免刷接口）
    - Redis 中状态 'error' 或不存在 → 可以启动

    这样保证同一个 keyword 在 cooldown 时间内只搜一次，但用户能即时看到上次结果。
    """
    try:
        status_key = f"online_search_status:{keyword}"
        cached = get_redis().get(status_key)
        if not cached:
            return True  # 从未搜过
        data = json.loads(cached)
        status = data.get("status", "")
        if status == "running":
            return False  # 正在搜
        if status == "done":
            # 检查是否在冷却时间内
            started_at = data.get("started_at", 0)
            if time.time() - started_at < cooldown_seconds:
                return False  # 刚搜过，跳过
            return True  # 冷却时间过了，可以重新搜
        # error 或其他状态 → 可以搜
        return True
    except Exception:
        return True  # 出错时允许搜（保守）


def _async_online_search(keyword, limit=20):
    """异步在线搜索：后台调用 pansou，结果直接入库（DB 是唯一真实来源）
    Redis 只用于存状态/进度给前端轮询，不存 items 数据本身。
    """
    status_key = f"online_search_status:{keyword}"
    try:
        # 标记开始
        try:
            get_redis().setex(status_key, 300, json.dumps({
                "status": "running", "started_at": time.time(), "imported": 0
            }))
        except Exception:
            pass

        items = _search_online_sync(keyword, limit=limit)
        if items:
            # 直接入库（DB 是唯一来源）
            imported, skipped_dead = _background_import(keyword, items, return_counts=True)
            # 标记完成
            try:
                get_redis().setex(status_key, 300, json.dumps({
                    "status": "done",
                    "started_at": time.time(),
                    "imported": imported,
                    "skipped_dead": skipped_dead,
                }))
            except Exception:
                pass
        else:
            try:
                get_redis().setex(status_key, 300, json.dumps({
                    "status": "done", "started_at": time.time(),
                    "imported": 0, "skipped_dead": 0,
                }))
            except Exception:
                pass
    except Exception as e:
        try:
            get_redis().setex(status_key, 300, json.dumps({
                "status": "error", "started_at": time.time(), "error": str(e)
            }))
        except Exception:
            pass
        print(f"[async-search] error for '{keyword}': {e}")


@app.route("/api/online_status")
def api_online_status():
    """查询在线搜索进度（前端轮询用）。
    返回 status: pending(尚未启动) | running | done | error
    """
    keyword = request.args.get("q", "").strip()
    if not keyword:
        return jsonify({"status": "pending", "imported": 0})
    try:
        cache_key = f"online_search_status:{keyword}"
        cached = get_redis().get(cache_key)
        if cached:
            data = json.loads(cached)
            return jsonify(data)
    except Exception:
        pass
    return jsonify({"status": "pending", "imported": 0})


@app.route("/api/online_results")
def api_online_results():
    """前端兼容接口：返回 DB 里在线搜索的结果（按 created_at DESC 取最近 N 条匹配 keyword 的）。
    不再从 Redis 缓存取原始 items，避免 UI 滑入逻辑走旁路。
    """
    keyword = request.args.get("q", "").strip()
    if not keyword:
        return jsonify({"items": [], "total": 0, "ready": False})
    # 同时返回状态
    status = {"status": "pending", "imported": 0}
    try:
        cache_key = f"online_search_status:{keyword}"
        cached = get_redis().get(cache_key)
        if cached:
            status = json.loads(cached)
    except Exception:
        pass
    # 从 DB 查这次搜索匹配 keyword 的总数
    db = get_db()
    try:
        cur = db.cursor()
        # 在线搜索的记录 = created_at 在最近 60 秒内 + keyword 匹配
        cur.execute(
            """SELECT COUNT(*) AS c FROM resources
               WHERE keyword = %s AND created_at >= DATE_SUB(NOW(), INTERVAL 60 SECOND)""",
            (keyword,)
        )
        total = cur.fetchone()["c"]
        # 取前 50 条展示
        cur.execute(
            """SELECT id, keyword, source, url, title, note, password,
                   quality, type, year, rating, datetime, created_at
            FROM resources
            WHERE keyword = %s AND created_at >= DATE_SUB(NOW(), INTERVAL 60 SECOND)
            ORDER BY created_at DESC LIMIT 50""",
            (keyword,)
        )
        items = cur.fetchall()
        return jsonify({
            "items": items,
            "total": total,
            "ready": status.get("status") in ("done",),
            "status": status.get("status", "pending"),
            "imported": status.get("imported", 0),
        })
    finally:
        db.close()


def _search_online_sync(keyword, limit=20, timeout=15):
    """同步在线搜索：并发请求所有 import_api_url，合并去重返回。
    用于在线搜索兜底。"""
    import threading as _th
    import requests as _req
    urls = _get_import_api_urls()
    all_items = []
    lock = _th.Lock()

    def _fetch(url):
        try:
            resp = _req.post(url, json={"kw": keyword, "limit": limit}, timeout=timeout)
            data = resp.json()
            if data.get("code") != 0:
                return
            items = _parse_online_items(keyword, data)
            if items:
                with lock:
                    all_items.extend(items)
        except Exception:
            pass

    threads = [_th.Thread(target=_fetch, args=(u,), daemon=True) for u in urls]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=timeout)

    # 去重
    seen = set()
    result = []
    for it in all_items:
        u = it.get("url", "")
        if u and u not in seen:
            seen.add(u)
            result.append(it)
    return result


@app.route("/api/import", methods=["POST"])
@admin_required
def api_import():
    client_ip = request.remote_addr
    if not check_rate_limit(f"import:{client_ip}"):
        return jsonify({"error": "请求太频繁，请1分钟后再试"}), 429

    data = request.get_json()
    keywords = data.get("keywords", [])
    search_limit = min(data.get("limit", 20), 50)

    if not keywords:
        return jsonify({"error": "请提供keywords参数"}), 400

    API_URL = _get_import_api_urls()[0]
    results = {"total_imported": 0, "details": [], "errors": []}

    db = get_db()
    try:
        with db.cursor() as cur:
            for kw in keywords:
                try:
                    resp = http_requests.post(
                        API_URL, json={"kw": kw, "limit": search_limit}, timeout=60
                    )
                    api_data = resp.json()
                    if api_data.get("code") != 0:
                        results["errors"].append(
                            {"keyword": kw, "error": api_data.get("message", "API错误")}
                        )
                        continue

                    total = api_data["data"]["total"]
                    merged = api_data["data"].get("merged_by_type", {})
                    imported = 0
                    for source, items in merged.items():
                        for item in items:
                            cur.execute(
                                """INSERT IGNORE INTO resources
                                (keyword, source, url, title, note, password, datetime, quality, type, year, created_at)
                                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())""",
                                (
                                    kw,
                                    source,
                                    item.get("url", ""),
                                    item.get("note", "") or item.get("title", ""),
                                    item.get("note", ""),
                                    item.get("password", ""),
                                    item.get("datetime", ""),
                                    "",
                                    "",
                                    0,
                                ),
                            )
                            imported += 1

                    db.commit()
                    results["total_imported"] += imported
                    results["details"].append(
                        {"keyword": kw, "api_total": total, "imported": imported}
                    )
                    time.sleep(1)

                except Exception as e:
                    results["errors"].append({"keyword": kw, "error": str(e)})
                    continue

            cur.execute("SELECT COUNT(*) as total FROM resources")
            results["db_total"] = cur.fetchone()["total"]

    finally:
        db.close()

    return jsonify(results)


# ==================== CSRF Token ====================
@app.route("/api/csrf-token")
def api_csrf_token():
    """返回 CSRF token 给前端"""
    return jsonify({"csrf_token": generate_csrf()})

# ==================== API: 健康检查 ====================
@app.route("/api/health")
def api_health():
    health = {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "services": {},
    }

    try:
        db = get_db()
        cur = db.cursor()
        cur.execute("SELECT COUNT(*) as total FROM resources")
        total = cur.fetchone()["total"]
        db.close()
        health["services"]["database"] = {"status": "connected", "resource_count": total}
    except Exception as e:
        health["services"]["database"] = {"status": "error", "error": str(e)}
        health["status"] = "degraded"

    return jsonify(health)


# ==================== 管理页面 ====================
@app.route("/admin")
@admin_required
def admin_page():
    # Force no-cache with version-busting
    from flask import make_response
    with open("admin.html", "rb") as _f:
        _data = _f.read()
    resp = make_response(_data)
    resp.content_type = "text/html; charset=utf-8"
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    resp.headers["X-Version"] = "1782288805"
    return resp


# ==================== 用户管理 API ====================
@app.route("/api/users")
@admin_required

def api_users():
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 20, type=int)
    offset = (page - 1) * per_page

    db = get_db()
    try:
        cur = db.cursor(pymysql.cursors.DictCursor)
        cur.execute("SELECT COUNT(*) as total FROM users")
        total = cur.fetchone()["total"]

        cur.execute(
            """SELECT id, username, email, role, status, last_login, created_at, updated_at, github_id, telegram_id
            FROM users ORDER BY id ASC LIMIT %s OFFSET %s""",
            (per_page, offset),
        )
        items = cur.fetchall()

        for i in items:
            i["last_login"] = str(i["last_login"]) if i["last_login"] else ""
            i["created_at"] = str(i["created_at"]) if i["created_at"] else ""
            i["updated_at"] = str(i["updated_at"]) if i["updated_at"] else ""

        return jsonify({"items": items, "total": total, "page": page})
    finally:
        db.close()


@app.route("/api/users/<int:uid>", methods=["POST"])
@admin_required
def api_user_update(uid):
    """更新用户信息：username, email, password, role, status"""
    data = request.get_json()
    if not data:
        return jsonify({"error": "无数据"}), 400
    db = get_db()
    try:
        cur = db.cursor()
        sets, vals = [], []
        if "username" in data:
            sets.append("username=%s"); vals.append(data["username"])
        if "email" in data:
            sets.append("email=%s"); vals.append(data["email"])
        if "password" in data and data["password"]:
            sets.append("password=%s"); vals.append(hash_password(data["password"]))
        if "role" in data:
            sets.append("role=%s"); vals.append(data["role"])
        if "status" in data:
            sets.append("status=%s"); vals.append(data["status"])
        if not sets:
            return jsonify({"error": "无有效字段"}), 400
        sets.append("updated_at=NOW()")
        vals.append(uid)
        cur.execute(f"UPDATE users SET {','.join(sets)} WHERE id=%s", vals)
        db.commit()
        return jsonify({"ok": True, "affected": cur.rowcount})
    except pymysql.err.IntegrityError:
        return jsonify({"error": "用户名已存在"}), 409
    finally:
        db.close()


@app.route("/api/users/<int:uid>", methods=["DELETE"])
@admin_required
def api_user_delete(uid):
    """删除用户：不能删自己，不能删其他管理员"""
    my_id = session.get("user_id")
    if uid == my_id:
        return jsonify({"error": "不能删除自己"}), 403
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("SELECT role FROM users WHERE id=%s", (uid,))
        u = cur.fetchone()
        if not u:
            return jsonify({"error": "用户不存在"}), 404
        if u["role"] == "admin":
            return jsonify({"error": "不能删除其他管理员"}), 403
        cur.execute("DELETE FROM users WHERE id=%s", (uid,))
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()

@app.route("/api/users", methods=["POST"])
@admin_required
def api_user_add():
    data = request.get_json()
    username = data.get("username", "").strip()
    if not username:
        return jsonify({"error": "用户名不能为空"}), 400
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            "INSERT INTO users (username, email, role, status) VALUES (%s, %s, %s, %s)",
            (username, data.get("email", ""), data.get("role", "user"), 1),
        )
        db.commit()
        return jsonify({"ok": True, "id": cur.lastrowid})
    except pymysql.err.IntegrityError:
        return jsonify({"error": f'用户名 "{username}" 已存在'}), 409
    finally:
        db.close()


@app.route("/api/users/<int:uid>/github", methods=["POST"])
@admin_required
def api_user_bind_github(uid):
    """管理员绑定/解绑 GitHub：传 {"github_id": "..."} 或 {"github_id": null}"""
    data = request.get_json() or {}
    github_id = data.get("github_id")
    db = get_db()
    try:
        cur = db.cursor()
        if github_id:
            cur.execute(
                "UPDATE users SET github_id=%s WHERE id=%s",
                (str(github_id), uid),
            )
        else:
            cur.execute(
                "UPDATE users SET github_id=NULL WHERE id=%s",
                (uid,),
            )
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/user/bind_github", methods=["POST"])
def api_user_bind_github_self():
    """当前登录用户解绑 GitHub（绑定请走 /login/github?bind=1 OAuth）"""
    uid = session.get("user_id")
    if not uid:
        return jsonify({"error": "未登录"}), 401
    data = request.get_json() or {}
    # 仅允许解绑；绑定必须走 GitHub OAuth，避免手填 ID
    if data.get("github_id") not in (None, "", False):
        return jsonify({"error": "请使用 GitHub 登录完成绑定", "oauth": "/login/github?bind=1&next=/profile"}), 400
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("UPDATE users SET github_id=NULL WHERE id=%s", (uid,))
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


# ==================== API: 公告管理 ====================
@app.route("/api/announcements")
def api_announcements():
    """获取当前生效的公告（公开）"""
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            "SELECT id, title, content, type FROM announcements WHERE active=1 AND (start_time IS NULL OR start_time <= NOW()) AND (end_time IS NULL OR end_time >= NOW()) ORDER BY id DESC LIMIT 1"
        )
        row = cur.fetchone()
        if not row:
            return jsonify({})
        return jsonify({
            "id": row["id"],
            "title": row["title"],
            "content": row["content"],
            "type": row["type"],
        })
    finally:
        db.close()


@app.route("/api/admin/announcements")
@admin_required
def api_admin_announcements():
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("SELECT id, title, content, type, active, start_time, end_time, created_at, updated_at FROM announcements ORDER BY id DESC")
        items = cur.fetchall()
        for i in items:
            i["start_time"] = str(i["start_time"]) if i.get("start_time") else ""
            i["end_time"] = str(i["end_time"]) if i.get("end_time") else ""
            i["created_at"] = str(i["created_at"]) if i.get("created_at") else ""
            i["updated_at"] = str(i["updated_at"]) if i.get("updated_at") else ""
        return jsonify({"items": items})
    finally:
        db.close()


@app.route("/api/admin/announcements", methods=["POST"])
@admin_required
def api_admin_announcements_create():
    data = request.get_json() or {}
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            "INSERT INTO announcements (title, content, type, active, start_time, end_time) VALUES (%s,%s,%s,%s,%s,%s)",
            (
                data.get("title", ""),
                data.get("content", ""),
                data.get("type", "banner"),
                1 if data.get("active", True) else 0,
                data.get("start_time") or None,
                data.get("end_time") or None,
            ),
        )
        db.commit()
        return jsonify({"ok": True, "id": cur.lastrowid})
    finally:
        db.close()


@app.route("/api/admin/announcements/<int:aid>", methods=["POST"])
@admin_required
def api_admin_announcements_update(aid):
    data = request.get_json() or {}
    db = get_db()
    try:
        cur = db.cursor()
        sets, vals = [], []
        for k in ("title", "content", "type", "start_time", "end_time"):
            if k in data:
                sets.append(f"{k}=%s")
                vals.append(data[k])
        if "active" in data:
            sets.append("active=%s")
            vals.append(1 if data["active"] else 0)
        if not sets:
            return jsonify({"error": "无有效字段"}), 400
        vals.append(aid)
        cur.execute(f"UPDATE announcements SET {','.join(sets)} WHERE id=%s", vals)
        db.commit()
        return jsonify({"ok": True, "affected": cur.rowcount})
    finally:
        db.close()


@app.route("/api/admin/announcements/<int:aid>", methods=["DELETE"])
@admin_required
def api_admin_announcements_delete(aid):
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("DELETE FROM announcements WHERE id=%s", (aid,))
        db.commit()
        return jsonify({"ok": True, "affected": cur.rowcount})
    finally:
        db.close()


# ==================== API: 资源来源管理 ====================
@app.route("/api/admin/sources")
@admin_required
def api_admin_sources():
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("SELECT id, name, label, color, active, created_at, updated_at FROM sources ORDER BY id ASC")
        items = cur.fetchall()
        for i in items:
            i["created_at"] = str(i["created_at"]) if i.get("created_at") else ""
            i["updated_at"] = str(i["updated_at"]) if i.get("updated_at") else ""
        return jsonify({"items": items})
    finally:
        db.close()


@app.route("/api/admin/sources", methods=["POST"])
@admin_required
def api_admin_sources_create():
    data = request.get_json() or {}
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            "INSERT INTO sources (name, label, color, active) VALUES (%s,%s,%s,%s)",
            (
                data.get("name", ""),
                data.get("label", ""),
                data.get("color", "#58a6ff"),
                1 if data.get("active", True) else 0,
            ),
        )
        db.commit()
        return jsonify({"ok": True, "id": cur.lastrowid})
    finally:
        db.close()


@app.route("/api/admin/sources/<int:sid>", methods=["POST"])
@admin_required
def api_admin_sources_update(sid):
    data = request.get_json() or {}
    db = get_db()
    try:
        cur = db.cursor()
        sets, vals = [], []
        for k in ("name", "label", "color"):
            if k in data:
                sets.append(f"{k}=%s")
                vals.append(data[k])
        if "active" in data:
            sets.append("active=%s")
            vals.append(1 if data["active"] else 0)
        if not sets:
            return jsonify({"error": "无有效字段"}), 400
        vals.append(sid)
        cur.execute(f"UPDATE sources SET {','.join(sets)} WHERE id=%s", vals)
        db.commit()
        return jsonify({"ok": True, "affected": cur.rowcount})
    finally:
        db.close()


@app.route("/api/admin/sources/<int:sid>", methods=["DELETE"])
@admin_required
def api_admin_sources_delete(sid):
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("DELETE FROM sources WHERE id=%s", (sid,))
        db.commit()
        return jsonify({"ok": True, "affected": cur.rowcount})
    finally:
        db.close()


# ==================== 用户信息 API ====================
@app.route("/api/me")
def api_me():
    uid = session.get("user_id")
    if not uid:
        return jsonify({"logged_in": False})
    # 从数据库读取最新角色（避免session过期问题）
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("SELECT username, role FROM users WHERE id=%s", (uid,))
        u = cur.fetchone()
    finally:
        db.close()
    if not u:
        return jsonify({"logged_in": False})
    is_admin = u["role"] == "admin"
    session["is_admin"] = is_admin
    session["username"] = u["username"]
    return jsonify({"logged_in": True, "user_id": uid, "username": u["username"], "is_admin": is_admin})

# ==================== 修改密码（任意登录用户） ====================
@app.route("/api/change_password", methods=["POST"])

def api_change_password():
    uid = session.get("user_id")
    if not uid:
        return jsonify({"error": "未登录"}), 401
    data = request.get_json()
    old_pw = data.get("old_password", "")
    new_pw = data.get("new_password", "").strip()
    if not old_pw:
        return jsonify({"error": "请输入旧密码"}), 400
    if not new_pw or len(new_pw) < 6:
        return jsonify({"error": "密码至少6位"}), 400
    # 验证旧密码（兼容旧版 SHA256）
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("SELECT password FROM users WHERE id=%s", (uid,))
        user = cur.fetchone()
        if not user:
            return jsonify({"error": "用户不存在"}), 404
        if not verify_password(old_pw, user.get("password", "")):
            return jsonify({"error": "旧密码错误"}), 403
        new_hash = hash_password(new_pw)
        cur.execute("UPDATE users SET password=%s, updated_at=NOW() WHERE id=%s", (new_hash, uid))
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()

# ==================== 收藏 API ====================
@app.route("/api/favorites")

def api_favorites():
    uid = session.get("user_id")
    if not uid: return jsonify({"error": "未登录"}), 401
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("SELECT f.id, f.resource_id, f.created_at, r.title, r.source, r.url, r.password, r.type, r.quality FROM favorites f LEFT JOIN resources r ON f.resource_id = r.id WHERE f.user_id = %s ORDER BY f.created_at DESC", (uid,))
        items = cur.fetchall()
        for i in items: i["created_at"] = str(i["created_at"]) if i["created_at"] else ""
        return jsonify({"items": items, "total": len(items)})
    finally: db.close()

@app.route("/api/favorites", methods=["POST"])
def api_fav_add():
    uid = session.get("user_id")
    if not uid: return jsonify({"error": "未登录"}), 401
    data = request.get_json()
    rid = data.get("resource_id")
    if not rid: return jsonify({"error": "缺少resource_id"}), 400
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("INSERT IGNORE INTO favorites (user_id, resource_id) VALUES (%s, %s)", (uid, rid))
        db.commit()
        return jsonify({"ok": True})
    finally: db.close()

@app.route("/api/favorites/<int:rid>", methods=["DELETE"])
def api_fav_del(rid):
    uid = session.get("user_id")
    if not uid: return jsonify({"error": "未登录"}), 401
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("DELETE FROM favorites WHERE user_id = %s AND resource_id = %s", (uid, rid))
        db.commit()
        return jsonify({"ok": True})
    finally: db.close()


# ==================== TMDB 海报片单（首页海报墙收藏） ====================
def _ensure_watchlist_table():
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS watchlist (
              id BIGINT NOT NULL AUTO_INCREMENT,
              user_id INT NOT NULL,
              tmdb_id BIGINT NOT NULL,
              media_type VARCHAR(16) NOT NULL DEFAULT 'movie',
              title VARCHAR(255) NOT NULL DEFAULT '',
              poster TEXT NULL,
              year VARCHAR(8) NULL,
              rating VARCHAR(16) NULL,
              overview TEXT NULL,
              created_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
              PRIMARY KEY (id),
              UNIQUE KEY uk_user_tmdb (user_id, tmdb_id, media_type),
              KEY idx_watchlist_user (user_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )
        db.commit()
    except Exception as e:
        app.logger.warning("ensure watchlist table: %s", e)
    finally:
        db.close()


@app.route("/api/watchlist")
def api_watchlist():
    uid = session.get("user_id")
    if not uid:
        return jsonify({"error": "未登录"}), 401
    _ensure_watchlist_table()
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            "SELECT id, tmdb_id, media_type, title, poster, year, rating, overview, created_at "
            "FROM watchlist WHERE user_id=%s ORDER BY created_at DESC",
            (uid,),
        )
        items = cur.fetchall()
        for i in items:
            i["created_at"] = str(i["created_at"]) if i.get("created_at") else ""
        return jsonify({"items": items, "total": len(items)})
    finally:
        db.close()


@app.route("/api/watchlist", methods=["POST"])
def api_watchlist_add():
    uid = session.get("user_id")
    if not uid:
        return jsonify({"error": "未登录"}), 401
    data = request.get_json() or {}
    tmdb_id = data.get("tmdb_id")
    if not tmdb_id:
        return jsonify({"error": "缺少 tmdb_id"}), 400
    try:
        tmdb_id = int(tmdb_id)
    except (TypeError, ValueError):
        return jsonify({"error": "tmdb_id 无效"}), 400
    media_type = (data.get("media_type") or "movie").strip()[:16] or "movie"
    title = (data.get("title") or "").strip()[:255]
    if not title:
        return jsonify({"error": "缺少标题"}), 400
    poster = (data.get("poster") or "")[:2000]
    year = str(data.get("year") or "")[:8]
    rating = str(data.get("rating") or "")[:16]
    overview = (data.get("overview") or "")[:2000]

    _ensure_watchlist_table()
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            """
            INSERT INTO watchlist (user_id, tmdb_id, media_type, title, poster, year, rating, overview)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
              title=VALUES(title), poster=VALUES(poster), year=VALUES(year),
              rating=VALUES(rating), overview=VALUES(overview)
            """,
            (uid, tmdb_id, media_type, title, poster, year, rating, overview),
        )
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/watchlist/<int:tmdb_id>", methods=["DELETE"])
def api_watchlist_del(tmdb_id):
    uid = session.get("user_id")
    if not uid:
        return jsonify({"error": "未登录"}), 401
    media_type = (request.args.get("media_type") or "movie").strip()[:16] or "movie"
    _ensure_watchlist_table()
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            "DELETE FROM watchlist WHERE user_id=%s AND tmdb_id=%s AND media_type=%s",
            (uid, tmdb_id, media_type),
        )
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()

# ==================== 搜索历史 API ====================
@app.route("/api/search_history")
def api_search_hist():
    uid = session.get("user_id")
    if not uid: return jsonify({"error": "未登录"}), 401
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("SELECT keyword, COUNT(*) as cnt, MAX(created_at) as last_at FROM search_history WHERE user_id = %s GROUP BY keyword ORDER BY last_at DESC LIMIT 30", (uid,))
        items = cur.fetchall()
        for i in items: i["last_at"] = str(i["last_at"]) if i["last_at"] else ""
        return jsonify({"items": items})
    finally: db.close()

@app.route("/api/search_history", methods=["POST"])
def api_search_hist_add():
    uid = session.get("user_id")
    if not uid: return jsonify({"ok": False}), 200
    data = request.get_json()
    kw = data.get("keyword", "").strip()
    if not kw: return jsonify({"ok": False}), 200
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("INSERT INTO search_history (user_id, keyword) VALUES (%s, %s)", (uid, kw))
        db.commit()
        return jsonify({"ok": True})
    finally: db.close()

@app.route("/api/search_history", methods=["DELETE"])
def api_search_hist_clear():
    uid = session.get("user_id")
    if not uid: return jsonify({"error": "未登录"}), 401
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("DELETE FROM search_history WHERE user_id = %s", (uid,))
        db.commit()
        return jsonify({"ok": True})
    finally: db.close()

# ==================== 点击统计 ====================
@app.route("/api/click/<int:rid>", methods=["POST"])
def api_click(rid):
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("UPDATE resources SET click_count = click_count + 1 WHERE id = %s", (rid,))
        db.commit()
        _invalidate_resource_cache()
        return jsonify({"ok": True})
    finally: db.close()
# ==================== 个人中心页面 ====================
@app.route("/profile")
@app.route("/profile.html")
def profile_page():
    resp = send_from_directory(".", "profile.html")
    resp.cache_control.max_age = 0
    resp.cache_control.no_cache = True
    resp.cache_control.no_store = True
    resp.cache_control.must_revalidate = True
    return resp


# ==================== 搜索日志收集（所有搜索） ====================
@app.route("/api/search_log", methods=["POST"])
def api_search_log():
    """记录搜索关键词（含未登录用户）"""
    data = request.get_json()
    kw = data.get("keyword", "").strip()
    if not kw or len(kw) < 2:
        return jsonify({"ok": False}), 200
    uid = session.get("user_id")
    ip = request.remote_addr
    db = get_db()
    try:
        cur = db.cursor()
        # 检查过滤词
        blocked = [w.lower() for w in _get_cached_filter_words()]
        if any(b in kw.lower() for b in blocked):
            _log_op(uid, session.get("username"), "search_blocked", f"关键词: {kw}", ip)
            return jsonify({"ok": False, "blocked": True}), 200
        # 记录搜索
        cur.execute("INSERT INTO search_logs (user_id, keyword, ip) VALUES (%s, %s, %s)", (uid, kw, ip))
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


def _log_op(uid, username, action, detail, ip=None):
    """记录操作日志的内部函数"""
    try:
        db = get_db()
        cur = db.cursor()
        cur.execute("INSERT INTO operation_logs (user_id, username, action, detail, ip) VALUES (%s, %s, %s, %s, %s)",
                    (uid, username, action, detail, ip or request.remote_addr))
        db.commit()
        db.close()
    except: pass


# ==================== 管理员：待导入搜索词 ====================
@app.route("/api/admin/pending_imports")
@admin_required
def api_pending_imports():
    """获取待导入的搜索关键词（聚合去重，排除已有资源的关键词）"""
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("""
            SELECT sl.keyword, COUNT(*) as search_count, MAX(created_at) as last_search
            FROM search_logs sl
            WHERE sl.is_imported = 0
              AND NOT EXISTS (
                  SELECT 1 FROM resources r WHERE r.keyword = sl.keyword
              )
            GROUP BY sl.keyword ORDER BY search_count DESC, last_search DESC
            LIMIT 100
        """)
        items = cur.fetchall()
        for i in items:
            i["last_search"] = str(i["last_search"]) if i["last_search"] else ""
        return jsonify({"items": items, "total": len(items)})
    finally:
        db.close()


# ==================== 管理员：确认导入 ====================
@app.route("/api/admin/confirm_import", methods=["POST"])
@admin_required

def api_confirm_import():
    """管理员确认导入指定关键词"""
    data = request.get_json()
    keywords = data.get("keywords", [])
    if not keywords:
        return jsonify({"error": "请选择关键词"}), 400

    import requests as http_requests
    API_URL = _get_import_api_urls()[0]
    results = {"total_imported": 0, "details": []}

    db = get_db()
    try:
        cur = db.cursor()
        for kw in keywords:
            try:
                resp = http_requests.post(API_URL, json={"kw": kw, "limit": 30}, timeout=60)
                api_data = resp.json()
                if api_data.get("code") != 0:
                    results["details"].append({"keyword": kw, "error": "API错误"})
                    continue
                merged = api_data["data"].get("merged_by_type", {})
                imported = 0
                for source, items in merged.items():
                    for item in items:
                        cur.execute("""INSERT IGNORE INTO resources
                            (keyword, source, url, title, note, password, datetime, quality, type, year, created_at)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())""",
                            (kw, source, item.get("url", ""), item.get("note", "") or item.get("title", ""),
                             item.get("note", ""), item.get("password", ""), item.get("datetime", ""), "", "", 0))
                        imported += 1
                # 标记为已导入
                cur.execute("UPDATE search_logs SET is_imported = 1 WHERE keyword = %s", (kw,))
                db.commit()
                results["total_imported"] += imported
                results["details"].append({"keyword": kw, "imported": imported})
                time.sleep(1)
            except Exception as e:
                results["details"].append({"keyword": kw, "error": str(e)})
        _log_op(session.get("user_id"), session.get("username"), "import", f"导入{len(keywords)}个关键词")
    finally:
        db.close()
    return jsonify(results)


# ==================== 管理员：操作日志 ====================
@app.route("/api/admin/logs")
@admin_required
def api_operation_logs():
    """获取操作日志"""
    page = request.args.get("page", 1, type=int)
    per_page = min(request.args.get("per_page", 50, type=int), 200)
    action = request.args.get("action", "")
    offset = (page - 1) * per_page
    db = get_db()
    try:
        cur = db.cursor()
        conditions, params = [], []
        if action:
            conditions.append("action = %s")
            params.append(action)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        cur.execute(f"SELECT COUNT(*) as total FROM operation_logs {where}", params)
        total = cur.fetchone()["total"]
        cur.execute(f"SELECT * FROM operation_logs {where} ORDER BY created_at DESC LIMIT %s OFFSET %s",
                    params + [per_page, offset])
        items = cur.fetchall()
        for i in items:
            i["created_at"] = str(i["created_at"]) if i["created_at"] else ""
        return jsonify({"items": items, "total": total, "page": page})
    finally:
        db.close()


# ==================== 过滤词管理 ====================
@app.route("/api/admin/filter_words")
@admin_required

def api_filter_words():
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("SELECT * FROM filter_words ORDER BY category, id")
        items = cur.fetchall()
        for i in items: i["created_at"] = str(i["created_at"]) if i["created_at"] else ""
        return jsonify({"items": items, "total": len(items)})
    finally:
        db.close()

@app.route("/api/admin/filter_words", methods=["POST"])
@admin_required
def api_filter_words_add():
    data = request.get_json()
    word = data.get("word", "").strip()
    cat = data.get("category", "general")
    if not word: return jsonify({"error": "请输入过滤词"}), 400
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("INSERT IGNORE INTO filter_words (word, category) VALUES (%s, %s)", (word, cat))
        db.commit()
        _clear_filter_words_cache()
        return jsonify({"ok": True})
    finally:
        db.close()

@app.route("/api/admin/filter_words/<int:fid>", methods=["DELETE"])
@admin_required
def api_filter_words_del(fid):
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("DELETE FROM filter_words WHERE id = %s", (fid,))
        db.commit()
        _clear_filter_words_cache()
        return jsonify({"ok": True})
    finally:
        db.close()



# ==================== 新功能 API ====================

# ── ① 热门搜索排行 ──
@app.route("/api/hot_searches")
@redis_cached(ttl=300, key_prefix="hot_searches")
def api_hot_searches():
    """返回 Top 10 热搜词"""
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            "SELECT keyword, hit_count as count FROM search_suggestions "
            "ORDER BY hit_count DESC LIMIT 10"
        )
        return jsonify({"items": cur.fetchall()})
    finally:
        db.close()


# ── ② 搜索自动补全 ──
@app.route("/api/autocomplete")
def api_autocomplete():
    """输入时返回匹配的搜索建议（搜索建议 + 资源库关键词）"""
    q = request.args.get("q", "").strip()
    if not q or len(q) < 1:
        return jsonify({"items": []})
    db = get_db()
    try:
        cur = db.cursor()
        # 先查搜索建议表
        cur.execute(
            "SELECT keyword, hit_count FROM search_suggestions "
            "WHERE keyword LIKE %s ORDER BY hit_count DESC LIMIT 8",
            (f"%{q}%",)
        )
        items = cur.fetchall()
        seen = {it["keyword"] for it in items}
        # 补充资源库高频关键词
        if len(items) < 8:
            if seen:
                placeholders = ",".join(["%s"] * len(seen))
                cur.execute(
                    f"SELECT keyword, COUNT(*) as hit_count FROM resources "
                    f"WHERE keyword LIKE %s AND keyword NOT IN ({placeholders}) "
                    f"GROUP BY keyword ORDER BY hit_count DESC LIMIT %s",
                    [f"%{q}%"] + list(seen) + [8 - len(items)]
                )
            else:
                cur.execute(
                    "SELECT keyword, COUNT(*) as hit_count FROM resources "
                    "WHERE keyword LIKE %s "
                    "GROUP BY keyword ORDER BY hit_count DESC LIMIT %s",
                    (f"%{q}%", 8 - len(items))
                )
            for r in cur.fetchall():
                if r["keyword"] not in seen:
                    items.append(r)
        return jsonify({"items": items[:8]})
    finally:
        db.close()


# ── ③ 资源详情面板 ──
@app.route("/api/resource/<int:rid>/detail")
def api_resource_detail(rid):
    """返回资源完整详情（含TMDB数据）"""
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            "SELECT id, keyword, source, url, title, note, password, "
            "quality, type, year, rating, datetime, images, "
            "tmdb_id, created_at "
            "FROM resources WHERE id = %s", (rid,)
        )
        item = cur.fetchone()
        if not item:
            return jsonify({"error": "资源不存在"}), 404
        return jsonify({"item": item})
    finally:
        db.close()


# ── ④ TMDB 热门排行 ──
@app.route("/api/tmdb/discover")
def api_tmdb_discover():
    """TMDB Discover API - 按类型/年代等筛选（socket直连）"""
    import socket, ssl as _ssl, json as _json, urllib.parse
    API_KEY = _get_tmdb_api_key()
    media_type = request.args.get("type", "movie")
    genre = request.args.get("genre", "")
    page = request.args.get("page", "1")

    cache_key = f"tmdb_discover:{media_type}:{genre}:{page}"
    now = time.time()
    if cache_key in _cache:
        cached_data, expire = _cache[cache_key]
        if now < expire:
            return jsonify(cached_data)

    params = f"api_key={API_KEY}&language=zh-CN&sort_by=popularity.desc&page={page}&vote_count.gte=50"
    if genre:
        params += f"&with_genres={genre}"
    path = f"/3/discover/{media_type}?{params}"

    try:
        ip = socket.getaddrinfo("www.themoviedb.org", 443, socket.AF_INET)[0][4][0]
        s = socket.create_connection((str(ip), 443), timeout=5)
        s.settimeout(5)
        ss = _ssl.create_default_context().wrap_socket(s, server_hostname="api.themoviedb.org")
        ss.settimeout(5)
        ss.sendall(
            f"GET {path} HTTP/1.1\r\nHost: api.themoviedb.org\r\n"
            f"Accept: application/json\r\nConnection: close\r\n\r\n".encode()
        )
        r = b""
        while True:
            c = ss.recv(65536)
            if not c:
                break
            r += c
        ss.close()
        h = r.find(b"\r\n\r\n")
        if h < 0:
            if cache_key in _cache:
                return jsonify(_cache[cache_key][0])
            return jsonify({"items": [], "error": "no response"})
        body = r[h + 4:]
        if b"chunked" in r[:h]:
            r2 = body; body = b""
            while r2:
                e = r2.find(b"\r\n")
                if e < 0:
                    break
                try:
                    sz = int(r2[:e], 16)
                except:
                    break
                if sz == 0:
                    break
                body += r2[e + 2:e + 2 + sz]
                r2 = r2[e + 2 + sz + 2:]
        d = _json.loads(body)
        items = []
        for it in d.get("results", [])[:20]:
            poster_path = it.get("poster_path", "")
            backdrop_path = it.get("backdrop_path", "")
            items.append({
                "tmdb_id": it.get("id"),
                "title": it.get("title") or it.get("name", ""),
                "year": (it.get("release_date") or it.get("first_air_date") or "")[:4],
                "rating": round(it.get("vote_average", 0), 1),
                "overview": it.get("overview", ""),
                "media_type": media_type,
                "poster": "/api/img_proxy?url=" + urllib.parse.quote("https://image.tmdb.org/t/p/w342" + poster_path) if poster_path else "",
                "backdrop": "/api/img_proxy?url=" + urllib.parse.quote("https://image.tmdb.org/t/p/w780" + backdrop_path) if backdrop_path else ""
            })
        result = {"items": items, "total": d.get("total_results", 0)}
        _cache[cache_key] = (result, now + 1800)
        return jsonify(result)
    except Exception as e:
        if cache_key in _cache:
            return jsonify(_cache[cache_key][0])
        return jsonify({"error": str(e), "items": []}), 500

@app.route("/api/tmdb/trending")
def api_tmdb_trending():
    """TMDB 热门排行（绕GFW直连），带缓存+超时+故障降级"""
    import socket, ssl as _ssl, json as _json, urllib.parse
    # 兼容前端 type 参数：week/day/movie/tv
    req_type = request.args.get("type", "")
    if req_type in ("movie", "tv"):
        media = req_type
        time_window = "week"
    elif req_type in ("day",):
        media = "all"
        time_window = "day"
    else:
        media = request.args.get("media", "all")
        time_window = request.args.get("time", "week")
    page = min(int(request.args.get("page", 1)), 10)
    API_KEY = _get_tmdb_api_key()
    TMDB_TTL = 1800  # 30 minutes cache

    # 生成缓存 key
    cache_key = f"tmdb_trending:{media}:{time_window}:{page}"
    rkey = _make_cache_key("tmdb_trending", media, time_window, page)
    now = time.time()

    # 检查缓存：Redis(跨worker共享) → 进程内 _cache(兜底)
    try:
        cached = get_redis().get(rkey)
        if cached:
            result_data = json.loads(cached)
            # 顺带回填进程内缓存，减少 Redis 往返
            _cache[cache_key] = (result_data, now + TMDB_TTL)
            return jsonify(result_data)
    except Exception:
        pass
    if cache_key in _cache:
        cached_data, expire = _cache[cache_key]
        if now < expire:
            return jsonify(cached_data)

    ep = f"trending/{media}/{time_window}"
    path = f"/3/{ep}?api_key={API_KEY}&language=zh-CN&page={page}"
    try:
        ip = socket.getaddrinfo("www.themoviedb.org", 443, socket.AF_INET)[0][4][0]
        s = socket.create_connection((str(ip), 443), timeout=5)
        s.settimeout(5)  # 读取也限5秒
        ss = _ssl.create_default_context().wrap_socket(s, server_hostname="api.themoviedb.org")
        ss.settimeout(5)
        ss.sendall(
            f"GET {path} HTTP/1.1\r\nHost: api.themoviedb.org\r\n"
            f"Accept: application/json\r\nConnection: close\r\n\r\n".encode()
        )
        r = b""
        while True:
            c = ss.recv(65536)
            if not c:
                break
            r += c
        ss.close()
        h = r.find(b"\r\n\r\n")
        if h < 0:
            # 无响应头，降级返回缓存或空
            if cache_key in _cache:
                return jsonify(_cache[cache_key][0])
            return jsonify({"items": [], "error": "no response"})
        body = r[h + 4:]
        if b"chunked" in r[:h]:
            r2 = body; body = b""
            while r2:
                e = r2.find(b"\r\n")
                if e < 0:
                    break
                try:
                    sz = int(r2[:e], 16)
                except ValueError:
                    break
                if sz == 0:
                    break
                body += r2[e + 2:e + 2 + sz]
                r2 = r2[e + 2 + sz + 2:]
        data = _json.loads(body)
        items = []
        for it in data.get("results", [])[:20]:
            items.append({
                "title": it.get("title") or it.get("name") or "",
                "overview": (it.get("overview") or "")[:280],
                "poster": ("/api/img_proxy?url=" + urllib.parse.quote("https://image.tmdb.org/t/p/w342" + it["poster_path"])) if it.get("poster_path") else "",
                "backdrop": ("/api/img_proxy?url=" + urllib.parse.quote("https://image.tmdb.org/t/p/w780" + it["backdrop_path"])) if it.get("backdrop_path") else "",
                "rating": round(it.get("vote_average", 0), 1),
                "media_type": it.get("media_type", ""),
                "year": (it.get("release_date") or it.get("first_air_date") or "")[:4],
                "tmdb_id": it.get("id"),
                "genre_ids": it.get("genre_ids") or [],
            })
        result_data = {"items": items, "total": data.get("total_results", 0)}
        # 写入缓存（存数据字典，不存response对象，防止gzip中间件重复压缩）
        _cache[cache_key] = (result_data, now + TMDB_TTL)
        try:
            get_redis().setex(rkey, TMDB_TTL, json.dumps(result_data, default=_json_default))
        except Exception:
            pass  # Redis 写入失败不影响响应
        return jsonify(result_data)
    except Exception as e:
        # 外部API故障，返回缓存的旧数据
        try:
            cached = get_redis().get(rkey)
            if cached:
                result_data = json.loads(cached)
                _cache[cache_key] = (result_data, now + TMDB_TTL)
                return jsonify(result_data)
        except Exception:
            pass
        if cache_key in _cache:
            return jsonify(_cache[cache_key][0])
        return jsonify({"items": [], "error": str(e)})


# ── ④ 推荐系统 ──
@app.route("/api/recommendations")
def api_recommendations():
    """TMDB 热门排行 + 本地有图资源混合推荐"""
    limit = min(int(request.args.get("limit", 12)), 30)
    db = get_db()
    try:
        cur = db.cursor()
        # 1. 先取有图片的本地热门资源
        cur.execute(
            "SELECT id, title, note, source, url, quality, type, year, rating, images "
            "FROM resources WHERE images IS NOT NULL AND images != '' "
            "ORDER BY created_at DESC LIMIT %s", (limit,)
        )
        local_items = cur.fetchall()
        for it in local_items:
            it["source_type"] = "local"
            # 解析 images JSON 获取海报
            imgs = it.get("images", "")
            if imgs:
                try:
                    import json as _j
                    arr = _j.loads(imgs)
                    it["poster"] = arr[0] if arr else ""
                except Exception:
                    it["poster"] = imgs
            else:
                it["poster"] = ""
        return jsonify({"items": local_items, "strategy": "local_with_posters"})
    finally:
        db.close()


# ── ⑤ 关键词订阅 ──
@app.route("/api/subscriptions")
def api_subscriptions():
    """获取当前用户的关键词订阅"""
    uid = session.get("user_id")
    if not uid:
        return jsonify({"items": []})
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            "SELECT id, keyword, created_at FROM subscriptions "
            "WHERE user_id = %s ORDER BY created_at DESC", (uid,)
        )
        return jsonify({"items": cur.fetchall()})
    finally:
        db.close()


@app.route("/api/subscriptions", methods=["POST"])
def api_subscription_add():
    """添加关键词订阅"""
    uid = session.get("user_id")
    if not uid:
        return jsonify({"error": "请先登录"}), 401
    data = request.get_json() or {}
    keyword = (data.get("keyword") or "").strip()
    if not keyword:
        return jsonify({"error": "请输入关键词"}), 400
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            "INSERT IGNORE INTO subscriptions (user_id, keyword) VALUES (%s, %s)",
            (uid, keyword)
        )
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/subscriptions/<int:sid>", methods=["DELETE"])
def api_subscription_del(sid):
    """取消关键词订阅"""
    uid = session.get("user_id")
    if not uid:
        return jsonify({"error": "请先登录"}), 401
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("DELETE FROM subscriptions WHERE id = %s AND user_id = %s", (sid, uid))
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


# ── ⑧ 资源分享 ──
@app.route("/api/resource/<int:rid>/share", methods=["POST"])
def api_resource_share(rid):
    """生成分享链接"""
    import secrets
    token = secrets.token_urlsafe(16)
    uid = session.get("user_id")
    db = get_db()
    try:
        cur = db.cursor()
        # 检查资源是否存在
        cur.execute("SELECT id FROM resources WHERE id = %s", (rid,))
        if not cur.fetchone():
            return jsonify({"error": "资源不存在"}), 404
        cur.execute(
            "INSERT INTO share_links (resource_id, token, created_by) VALUES (%s, %s, %s)",
            (rid, token, uid)
        )
        db.commit()
        share_url = f"{request.host_url}s/{token}"
        return jsonify({"ok": True, "url": share_url, "token": token})
    finally:
        db.close()


@app.route("/s/<token>")
def api_shared_resource(token):
    """通过分享链接访问资源"""
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            "SELECT sl.resource_id, sl.click_count, "
            "r.id, r.title, r.note, r.source, r.url, r.quality, r.type, "
            "r.year, r.rating, r.poster_url, r.password "
            "FROM share_links sl "
            "JOIN resources r ON r.id = sl.resource_id "
            "WHERE sl.token = %s", (token,)
        )
        item = cur.fetchone()
        if not item:
            return "链接无效或已过期", 404
        # 更新点击数
        cur.execute(
            "UPDATE share_links SET click_count = click_count + 1 WHERE token = %s", (token,)
        )
        db.commit()
        # 返回简洁的分享页面
        title = item.get("title") or item.get("note") or "网盘资源"
        url = item.get("url", "")
        source = item.get("source", "")
        quality = item.get("quality", "")
        password = item.get("password", "")
        poster = item.get("poster_url", "")

        poster_html = ""
        if poster:
            poster_html = '<img src="' + html_mod.escape(poster, quote=True) + '" style="max-width:200px;border-radius:8px;margin-bottom:1rem">'

        quality_html = ""
        if quality:
            quality_html = '<span class="tag">' + html_mod.escape(quality) + '</span>'

        pw_html = ""
        if password:
            pw_html = '<div class="pw">🔑 提取码: ' + html_mod.escape(password) + '</div>'

        html_content = (
            '<!DOCTYPE html><html><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>' + html_mod.escape(title) + ' - 分享</title>'
            '<style>'
            '*{margin:0;padding:0;box-sizing:border-box}'
            'body{background:#0f0f14;color:#e4e4e7;font-family:-apple-system,sans-serif;'
            'display:flex;align-items:center;justify-content:center;min-height:100vh;padding:1rem}'
            '.card{background:#1a1a24;border:1px solid #2a2a3e;border-radius:16px;padding:2rem;max-width:480px;width:100%;text-align:center}'
            'h2{font-size:1.2rem;margin-bottom:0.5rem}'
            '.tag{display:inline-block;background:#6366f1;color:white;padding:0.2rem 0.6rem;border-radius:6px;font-size:0.75rem;margin:0.2rem}'
            '.url{background:#12121c;border:1px solid #2a2a3e;border-radius:8px;padding:0.8rem;word-break:break-all;margin:1rem 0;font-size:0.85rem;user-select:all}'
            '.pw{color:#f59e0b;font-weight:600;margin:0.5rem 0}'
            '.btn{display:inline-block;background:#6366f1;color:white;padding:0.7rem 2rem;border:none;border-radius:8px;font-size:0.9rem;cursor:pointer;text-decoration:none;margin:0.3rem}'
            '.btn:hover{background:#4f46e5}'
            '.btn2{background:#374151}'
            '</style></head><body><div class="card">'
            + poster_html
            + '<h2>' + html_mod.escape(title) + '</h2>'
            '<div><span class="tag">' + html_mod.escape(source) + '</span>' + quality_html + '</div>'
            '<div class="url" id="u">' + html_mod.escape(url) + '</div>'
            + pw_html
            + '<button class="btn" onclick="copyU()">📋 复制链接</button>'
            '<a class="btn btn2" href="' + html_mod.escape(url, quote=True) + '" target="_blank">🔗 打开网盘</a>'
            '<a class="btn btn2" href="/" style="text-decoration:none">🏠 更多资源</a>'
            '<script>function copyU(){var u=document.getElementById("u").innerText;'
            'navigator.clipboard.writeText(u).then(function(){alert("已复制！")})}</script>'
            '</div></body></html>'
        )
        return html_content
    finally:
        db.close()


# ── ⑨ 后台数据统计面板 ──
@app.route("/api/admin/stats/dashboard")
@admin_required
def api_admin_dashboard():
    """后台统计数据（图表用）"""
    db = get_db()
    try:
        cur = db.cursor()

        # 1. 每日新增资源（最近30天）
        cur.execute(
            "SELECT DATE(created_at) as date, COUNT(*) as count "
            "FROM resources WHERE created_at >= DATE_SUB(NOW(), INTERVAL 30 DAY) "
            "GROUP BY DATE(created_at) ORDER BY date"
        )
        daily_resources = cur.fetchall()

        # 2. 每日搜索量（最近30天）
        cur.execute(
            "SELECT DATE(created_at) as date, COUNT(*) as count "
            "FROM search_logs WHERE created_at >= DATE_SUB(NOW(), INTERVAL 30 DAY) "
            "GROUP BY DATE(created_at) ORDER BY date"
        )
        daily_searches = cur.fetchall()

        # 3. Top 20 搜索词
        cur.execute(
            "SELECT keyword, COUNT(*) as count FROM search_logs "
            "GROUP BY keyword ORDER BY count DESC LIMIT 20"
        )
        top_searches = cur.fetchall()

        # 4. 资源类型分布
        cur.execute(
            "SELECT COALESCE(type, '未分类') as type, COUNT(*) as count "
            "FROM resources GROUP BY type ORDER BY count DESC"
        )
        type_dist = cur.fetchall()

        # 5. 来源分布
        cur.execute(
            "SELECT source, COUNT(*) as count FROM resources "
            "GROUP BY source ORDER BY count DESC LIMIT 10"
        )
        source_dist = cur.fetchall()

        # 6. 画质分布
        cur.execute(
            "SELECT COALESCE(quality, '未知') as quality, COUNT(*) as count "
            "FROM resources GROUP BY quality ORDER BY count DESC"
        )
        quality_dist = cur.fetchall()

        # 7. 总览数据
        stats = {}
        for tbl in ["resources", "users", "search_logs", "favorites", "filter_words"]:
            cur.execute(f"SELECT COUNT(*) as c FROM {tbl}")
            stats[tbl] = cur.fetchone()["c"]
        total_visits, today_visits = _get_visit_counts()
        stats["total_visits"] = total_visits
        stats["today_visits"] = today_visits

        return jsonify({
            "daily_resources": daily_resources,
            "daily_searches": daily_searches,
            "top_searches": top_searches,
            "type_dist": type_dist,
            "source_dist": source_dist,
            "quality_dist": quality_dist,
            "overview": stats,
            "total_visits": total_visits,
            "today_visits": today_visits,
        })
    finally:
        db.close()


# ── ⑩ API 文档 ──
@app.route("/api/docs")
def api_docs():
    """自动生成 API 文档"""
    docs = []
    for rule in app.url_map.iter_rules():
        if rule.endpoint == "static":
            continue
        func = app.view_functions.get(rule.endpoint)
        if not func:
            continue
        doc = {
            "path": rule.rule,
            "methods": sorted(rule.methods - {"OPTIONS", "HEAD"}),
            "name": func.__name__,
            "doc": (func.__doc__ or "").strip(),
            "auth": hasattr(func, "__wrapped__"),
        }
        docs.append(doc)
    docs.sort(key=lambda d: d["path"])
    return jsonify({"docs": docs, "total": len(docs)})



# ==================== 后台管理增强 API ====================

# ── 系统设置 ──
@app.route("/api/admin/settings")
@admin_required
def api_admin_settings():
    """获取所有系统设置（敏感值脱敏）"""
    _ensure_telegram_settings_keys()
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("SELECT `key`, value, description, updated_at FROM settings ORDER BY `key`")
        items = cur.fetchall()
        secret_keys = {"telegram_bot_token", "admin_pass", "tmdb_api_key", "secret_key"}
        for i in items:
            i["updated_at"] = str(i["updated_at"]) if i.get("updated_at") else ""
            if i.get("key") in secret_keys and i.get("value"):
                i["value_masked"] = _mask_secret(i["value"])
                i["has_value"] = True
                # 前端编辑用：不回传明文；有值时用占位提示
                i["value"] = ""
            else:
                i["has_value"] = bool(i.get("value"))
                i["value_masked"] = ""
        # 附带当前生效的 TG 登录状态（env 兜底也算）
        return jsonify({
            "items": items,
            "telegram_login": {
                "enabled": bool(_telegram_bot_token() and _telegram_bot_username()),
                "bot_username": _telegram_bot_username(),
                "token_configured": bool(_telegram_bot_token()),
                "superadmin_ids": sorted(_telegram_superadmin_ids()),
                "required_chat": _telegram_required_chat(),
                "required_chat_link": (
                    (lambda c: (c if c.startswith("http") else f"https://t.me/{c.lstrip('@')}"))(_telegram_required_chat())
                    if _telegram_required_chat() else ""
                ),
            },
        })
    finally:
        db.close()


@app.route("/api/admin/settings", methods=["POST"])
@admin_required
def api_admin_settings_update():
    """更新系统设置（upsert）"""
    _ensure_telegram_settings_keys()
    data = request.get_json() or {}
    db = get_db()
    try:
        cur = db.cursor()
        updated = 0
        settings_list = data.get("settings", [])
        pairs = []
        if settings_list:
            for item in settings_list:
                if isinstance(item, dict) and "key" in item and "value" in item:
                    pairs.append((item["key"], item["value"]))
        else:
            for k, v in data.items():
                if k in ("admin_pass", "settings"):
                    continue
                pairs.append((k, v))

        for k, v in pairs:
            k = str(k).strip()
            if not k:
                continue
            # 敏感字段留空 = 不修改
            if k in ("telegram_bot_token", "admin_pass", "tmdb_api_key") and (v is None or str(v).strip() == ""):
                continue
            if k == "telegram_bot_username":
                v = str(v).strip().lstrip("@")
            cur.execute("SELECT `key` FROM settings WHERE `key`=%s", (k,))
            if cur.fetchone():
                cur.execute("UPDATE settings SET value=%s WHERE `key`=%s", (str(v), k))
            else:
                cur.execute(
                    "INSERT INTO settings (`key`, value, description) VALUES (%s, %s, %s)",
                    (k, str(v), ""),
                )
            updated += 1

        # 可选：单独改管理员密码字段（旧逻辑兼容）
        admin_pass = data.get("admin_pass")
        if admin_pass:
            cur.execute("SELECT `key` FROM settings WHERE `key`='admin_pass'")
            if cur.fetchone():
                cur.execute("UPDATE settings SET value=%s WHERE `key`='admin_pass'", (str(admin_pass),))
            else:
                cur.execute(
                    "INSERT INTO settings (`key`, value, description) VALUES ('admin_pass', %s, '管理员密码')",
                    (str(admin_pass),),
                )
            updated += 1

        db.commit()
        for ck in list(_cache.keys()):
            if "/api/" in ck:
                del _cache[ck]
        return jsonify({"ok": True, "updated": updated})
    finally:
        db.close()


# ── 资源批量操作 ──
@app.route("/api/admin/search_logs")
@admin_required
def api_admin_search_logs():
    db = get_db()
    try:
        cur = db.cursor()
        per_page = min(int(request.args.get("per_page", 50)), 200)
        page = int(request.args.get("page", 1))
        offset = (page - 1) * per_page
        cur.execute(
            "SELECT id, keyword, user_id, ip, created_at FROM search_logs ORDER BY id DESC LIMIT %s OFFSET %s",
            (per_page, offset),
        )
        items = cur.fetchall()
        cur.execute("SELECT COUNT(*) as c FROM search_logs")
        total = cur.fetchone()["c"]
        return jsonify({"items": items, "total": total, "page": page, "per_page": per_page})
    finally:
        db.close()


@app.route("/api/admin/search_logs/<int:sid>", methods=["DELETE"])
@admin_required
def api_admin_search_logs_delete(sid):
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("DELETE FROM search_logs WHERE id=%s", (sid,))
        db.commit()
        return jsonify({"ok": True, "affected": cur.rowcount})
    finally:
        db.close()


@app.route("/api/admin/search_logs/cleanup", methods=["POST"])
@admin_required
def api_admin_search_logs_cleanup():
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("SELECT value FROM settings WHERE `key`='search_log_retention_days'")
        row = cur.fetchone()
        days = int(row["value"]) if row and row["value"] else 90
        if days <= 0:
            return jsonify({"ok": True, "deleted": 0, "msg": "永久保留模式，未清理"})
        cur.execute(
            "DELETE FROM search_logs WHERE created_at < DATE_SUB(NOW(), INTERVAL %s DAY)",
            (days,),
        )
        db.commit()
        return jsonify({"ok": True, "deleted": cur.rowcount, "retention_days": days})
    finally:
        db.close()


# ── 资源批量操作 ──
@app.route("/api/admin/resources/batch", methods=["POST"])
@admin_required
def api_admin_resources_batch():
    """批量操作资源: delete/export/update_quality/update_type"""
    data = request.get_json() or {}
    action = data.get("action", "")
    ids = data.get("ids", [])
    keyword = data.get("keyword", "")
    
    if not ids and not keyword:
        return jsonify({"error": "请选择资源或指定关键词"}), 400

    db = get_db()
    try:
        cur = db.cursor()
        
        if keyword:
            where = "WHERE keyword = %s"
            params = [keyword]
        else:
            placeholders = ",".join(["%s"] * len(ids))
            where = f"WHERE id IN ({placeholders})"
            params = ids

        if action == "delete":
            cur.execute(f"DELETE FROM resources {where}", params)
            db.commit()
            _invalidate_resource_cache()
            return jsonify({"ok": True, "affected": cur.rowcount})

        elif action == "export":
            cur.execute(
                f"SELECT id, title, note, source, url, quality, type, password, keyword "
                f"FROM resources {where} ORDER BY created_at DESC LIMIT 5000",
                params
            )
            items = cur.fetchall()
            lines = []
            for it in items:
                title = it.get("title") or it.get("note") or ""
                lines.append(f"{title}	{it.get('source','')}	{it.get('url','')}	{it.get('password','')}")
            return jsonify({"ok": True, "data": "\n".join(lines), "count": len(items)})

        elif action == "update_quality":
            quality = data.get("value", "")
            cur.execute(f"UPDATE resources SET quality = %s {where}", [quality] + params)
            db.commit()
            _invalidate_resource_cache()
            return jsonify({"ok": True, "affected": cur.rowcount})

        elif action == "update_type":
            rtype = data.get("value", "")
            cur.execute(f"UPDATE resources SET type = %s {where}", [rtype] + params)
            db.commit()
            _invalidate_resource_cache()
            return jsonify({"ok": True, "affected": cur.rowcount})

        return jsonify({"error": f"未知操作: {action}"}), 400
    finally:
        db.close()


# ── 资源列表（后台增强版） ──
@app.route("/api/admin/resources")
@admin_required

def api_admin_resources():
    """后台资源列表，支持搜索/排序/分页"""
    page = max(int(request.args.get("page", 1)), 1)
    per_page = min(int(request.args.get("per_page", 50)), 200)
    q = request.args.get("q", "").strip()
    source = request.args.get("source", "")
    rtype = request.args.get("type", "")
    quality = request.args.get("quality", "")
    sort = request.args.get("sort", "created_at")
    order = request.args.get("order", "desc")

    offset = (page - 1) * per_page
    db = get_db()
    try:
        cur = db.cursor()
        conditions, params = [], []
        if q:
            conditions.append("(title LIKE %s OR note LIKE %s OR keyword LIKE %s)")
            params.extend([f"%{q}%"] * 3)
        if source:
            conditions.append("source = %s")
            params.append(source)
        if rtype:
            conditions.append("type = %s")
            params.append(rtype)
        if quality:
            conditions.append("quality = %s")
            params.append(quality)

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        allowed_sorts = {"created_at", "id", "title", "source", "quality", "type", "click_count"}
        if sort not in allowed_sorts:
            sort = "created_at"
        order_dir = "DESC" if order == "desc" else "ASC"

        cur.execute(f"SELECT COUNT(*) as total FROM resources {where}", params)
        total = cur.fetchone()["total"]

        cur.execute(
            f"SELECT id, title, note, source, url, quality, type, year, keyword, "
            f"click_count, created_at FROM resources {where} "
            f"ORDER BY {sort} {order_dir} LIMIT %s OFFSET %s",
            params + [per_page, offset]
        )
        items = cur.fetchall()
        for i in items:
            i["created_at"] = str(i["created_at"]) if i.get("created_at") else ""

        # 统计信息
        cur.execute("SELECT COUNT(*) as c FROM resources")
        total_all = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(DISTINCT source) as c FROM resources")
        source_count = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(DISTINCT keyword) as c FROM resources")
        kw_count = cur.fetchone()["c"]

        return jsonify({
            "items": items, "total": total, "page": page, "per_page": per_page,
            "total_pages": max(1, (total + per_page - 1) // per_page),
            "stats": {"total_all": total_all, "source_count": source_count, "keyword_count": kw_count}
        })
    finally:
        db.close()


# ── 自动导入队列 ──
@app.route("/api/admin/import_queue")
@admin_required
def api_admin_import_queue():
    """获取自动导入队列"""
    status = request.args.get("status", "")
    db = get_db()
    try:
        cur = db.cursor()
        if status:
            cur.execute(
                "SELECT * FROM import_queue WHERE status = %s ORDER BY created_at DESC LIMIT 100",
                (status,)
            )
        else:
            cur.execute("SELECT * FROM import_queue ORDER BY created_at DESC LIMIT 100")
        items = cur.fetchall()
        for i in items:
            i["created_at"] = str(i["created_at"]) if i.get("created_at") else ""
            i["updated_at"] = str(i["updated_at"]) if i.get("updated_at") else ""
        return jsonify({"items": items, "total": len(items)})
    finally:
        db.close()


@app.route("/api/admin/import_queue/retry", methods=["POST"])
@admin_required
def api_admin_import_retry():
    """重试失败的导入任务"""
    data = request.get_json() or {}
    ids = data.get("ids", [])
    if not ids:
        return jsonify({"error": "请选择任务"}), 400
    db = get_db()
    try:
        cur = db.cursor()
        placeholders = ",".join(["%s"] * len(ids))
        cur.execute(
            f"UPDATE import_queue SET status='pending', error_msg=NULL WHERE id IN ({placeholders}) AND status='failed'",
            ids
        )
        db.commit()
        return jsonify({"ok": True, "reset": cur.rowcount})
    finally:
        db.close()


# ── 自动导入逻辑（搜索0结果时自动触发并处理） ──
def _auto_import_trigger(keyword):
    """搜索时自动加入队列并立即后台处理。返回值: True=导入中, 'exhausted'=已尝试过无结果, False=不触发"""
    try:
        db = get_db()
        cur = db.cursor()
        # 检查是否已在队列中（正在处理）
        cur.execute(
            "SELECT id, status FROM import_queue WHERE keyword = %s AND status IN ('pending','processing')",
            (keyword,)
        )
        if cur.fetchone():
            db.close()
            return True  # 正在处理中
        # 检查最近是否已经成功导入过（1小时内有结果的不重复导入）
        cur.execute(
            "SELECT id FROM import_queue WHERE keyword = %s AND status = 'done' AND result_count > 0 AND updated_at > DATE_SUB(NOW(), INTERVAL 1 HOUR) LIMIT 1",
            (keyword,)
        )
        if cur.fetchone():
            db.close()
            return False  # 最近已成功导入，跳过
        # 检查自动导入是否开启
        cur.execute("SELECT value FROM settings WHERE `key`='auto_import_enabled'")
        row = cur.fetchone()
        if not row or row["value"] != "1":
            db.close()
            return False
        # 加入队列
        cur.execute(
            "INSERT INTO import_queue (keyword, status) VALUES (%s, 'processing')",
            (keyword,)
        )
        qid = cur.lastrowid
        db.commit()
        db.close()
        # 后台线程立即处理
        import threading
        t = threading.Thread(target=_process_import, args=(keyword, qid), daemon=True)
        t.start()
        return True
    except Exception:
        return False


# ── 自动检测新导入链接有效性 ──
def _auto_check_links_bg(items):
    """后台线程：批量检测新导入资源的链接有效性"""
    import threading
    def _worker(items):
        for item in items:
            try:
                url = item.get("url") or ""
                source = (item.get("source") or "").lower()
                rid = item.get("id")
                if not url or not rid:
                    continue
                status, code, msg = _check_single_link(url, source)
                db = get_db()
                try:
                    cur = db.cursor()
                    cur.execute("UPDATE resources SET last_checked=NOW(), link_status=%s WHERE id=%s", (status, rid))
                    db.commit()
                    _invalidate_resource_cache()
                finally:
                    db.close()
            except:
                pass
    t = threading.Thread(target=_worker, args=(items,), daemon=True)
    t.start()


def _auto_scan_unchecked_bg(limit=50):
    """同步扫描未检测的资源链接（在调用方线程中执行，不新开线程）"""
    try:
        db = get_db()
        cur = db.cursor()
        cur.execute(
            "SELECT id, source, url FROM resources "
            "WHERE source NOT IN ('magnet','ed2k','other') AND last_checked IS NULL "
            "ORDER BY id DESC LIMIT %s", (limit,)
        )
        items = cur.fetchall()
        db.close()
        for item in items:
            try:
                url = item.get("url") or ""
                source = (item.get("source") or "").lower()
                rid = item.get("id")
                if not url or not rid:
                    continue
                status, code, msg = _check_single_link(url, source)
                try:
                    db2 = get_db()
                    cur2 = db2.cursor()
                    cur2.execute("UPDATE resources SET last_checked=NOW(), link_status=%s WHERE id=%s", (status, rid))
                    db2.commit()
                    db2.close()
                except:
                    pass
            except:
                pass
    except:
        pass


def _fetch_api(api_url, keyword, limit):
    """从单个API搜索资源"""
    import requests as http_requests
    try:
        resp = http_requests.post(api_url, json={"kw": keyword, "limit": limit}, timeout=60)
        data = resp.json()
        if data.get("code") != 0:
            return []
        merged = data.get("data", {}).get("merged_by_type", {})
        results = []
        for source, items in merged.items():
            for item in items:
                results.append({"source": source, "item": item})
        return results
    except Exception:
        return []


def _process_import(keyword, queue_id):
    """后台处理单个导入任务（支持多API并发）"""
    import threading
    try:
        db = get_db()
        cur = db.cursor()
        # 获取API配置（支持多个，换行分隔）
        cur.execute("SELECT value FROM settings WHERE `key`='import_api_url'")
        row = cur.fetchone()
        api_urls_raw = row["value"] if row else "https://pansou.42078207.qzz.io/api/search\nhttps://pansou.42078207.xyz/api/search"
        api_urls = [u.strip() for u in api_urls_raw.split("\n") if u.strip()]
        if not api_urls:
            api_urls = [api_urls_raw]

        cur.execute("SELECT value FROM settings WHERE `key`='import_api_limit'")
        row = cur.fetchone()
        limit = int(row["value"]) if row else 30
        db.close()

        # 并发请求所有API
        all_results = []
        if len(api_urls) == 1:
            all_results = _fetch_api(api_urls[0], keyword, limit)
        else:
            threads = []
            results_map = {}
            def fetch_one(url, idx):
                results_map[idx] = _fetch_api(url, keyword, limit)
            for i, url in enumerate(api_urls):
                t = threading.Thread(target=fetch_one, args=(url, i))
                threads.append(t)
                t.start()
            for t in threads:
                t.join(timeout=90)
            for i in range(len(api_urls)):
                all_results.extend(results_map.get(i, []))

        # 去重（按URL）
        seen_urls = set()
        unique_results = []
        for r in all_results:
            url = r["item"].get("url", "")
            if url and url not in seen_urls:
                seen_urls.add(url)
                unique_results.append(r)

        # 写入数据库
        db = get_db()
        try:
            cur = db.cursor()
            imported = 0
            for r in unique_results:
                item = r["item"]
                source = r["source"]
                cur.execute("""INSERT IGNORE INTO resources
                    (keyword, source, url, title, note, password, datetime, quality, type, year, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())""",
                    (keyword, source, item.get("url", ""), item.get("note", "") or item.get("title", ""),
                     item.get("note", ""), item.get("password", ""), item.get("datetime", ""), "", "", 0))
                imported += 1

            cur.execute("UPDATE import_queue SET status='done', result_count=%s WHERE id=%s",
                       (imported, queue_id))
            cur.execute("UPDATE search_logs SET is_imported=1 WHERE keyword=%s", (keyword,))
            db.commit()
        finally:
            db.close()

        # 自动检测新导入资源的链接有效性
        if imported > 0:
            _auto_scan_unchecked_bg(min(imported, 50))
    except Exception as e:
        try:
            db = get_db()
            try:
                cur = db.cursor()
                cur.execute("UPDATE import_queue SET status='failed', error_msg=%s WHERE id=%s",
                           (str(e)[:200], queue_id))
                db.commit()
            finally:
                db.close()
        except:
            pass


# ── 用户端：检查导入状态 ──
@app.route("/api/import_status")
def api_import_status():
    """用户搜索后检查是否有自动导入进行中"""
    keyword = request.args.get("q", "").strip()
    if not keyword:
        return jsonify({"importing": False})
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            "SELECT status, result_count, created_at FROM import_queue "
            "WHERE keyword = %s ORDER BY created_at DESC LIMIT 1",
            (keyword,)
        )
        row = cur.fetchone()
        if not row:
            return jsonify({"importing": False})
        return jsonify({
            "importing": row["status"] in ("pending", "processing"),
            "status": row["status"],
            "result_count": row.get("result_count", 0),
            "created_at": str(row["created_at"]) if row.get("created_at") else ""
        })
    finally:
        db.close()



@app.route("/api/admin/import_queue/<int:qid>", methods=["DELETE"])
@admin_required
def api_admin_import_queue_delete(qid):
    """删除单条队列记录"""
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("DELETE FROM import_queue WHERE id = %s", (qid,))
        db.commit()
        return jsonify({"ok": True, "deleted": cur.rowcount})
    finally:
        db.close()

@app.route("/api/admin/import_queue/clear", methods=["POST"])
@admin_required
def api_admin_import_queue_clear():
    """一键清空所有导入队列"""
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("SELECT COUNT(*) as cnt FROM import_queue")
        cnt = cur.fetchone()["cnt"]
        cur.execute("DELETE FROM import_queue")
        db.commit()
        return jsonify({"ok": True, "deleted": cnt})
    finally:
        db.close()

# ── 后台：手动触发导入队列处理 ──
@app.route("/api/admin/process_queue", methods=["POST"])
@admin_required

def api_admin_process_queue():
    """手动触发处理导入队列"""
    import requests as http_requests
    API_URL = None
    db = get_db()
    try:
        cur = db.cursor()
        # 获取API地址
        cur.execute("SELECT value FROM settings WHERE `key`='import_api_url'")
        row = cur.fetchone()
        API_URL = row["value"] if row else "https://pansou.42078207.qzz.io/api/search"
        
        cur.execute("SELECT value FROM settings WHERE `key`='import_api_limit'")
        row = cur.fetchone()
        limit = int(row["value"]) if row else 30

        # 获取待处理任务
        cur.execute("SELECT * FROM import_queue WHERE status = 'pending' ORDER BY created_at LIMIT 10")
        tasks = cur.fetchall()
        
        processed = 0
        for task in tasks:
            kw = task["keyword"]
            try:
                cur.execute("UPDATE import_queue SET status='processing' WHERE id=%s", (task["id"],))
                db.commit()
                
                resp = http_requests.post(API_URL, json={"kw": kw, "limit": limit}, timeout=60)
                api_data = resp.json()
                if api_data.get("code") != 0:
                    cur.execute("UPDATE import_queue SET status='failed', error_msg=%s WHERE id=%s",
                               (api_data.get("msg", "API错误"), task["id"]))
                    db.commit()
                    continue

                merged = api_data["data"].get("merged_by_type", {})
                imported = 0
                for source, items in merged.items():
                    for item in items:
                        cur.execute("""INSERT IGNORE INTO resources
                            (keyword, source, url, title, note, password, datetime, quality, type, year, created_at)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())""",
                            (kw, source, item.get("url", ""), item.get("note", "") or item.get("title", ""),
                             item.get("note", ""), item.get("password", ""), item.get("datetime", ""), "", "", 0))
                        imported += 1
                
                cur.execute("UPDATE import_queue SET status='done', result_count=%s WHERE id=%s",
                           (imported, task["id"]))
                # 同时标记search_logs
                cur.execute("UPDATE search_logs SET is_imported=1 WHERE keyword=%s", (kw,))
                db.commit()
                processed += 1
            except Exception as e:
                cur.execute("UPDATE import_queue SET status='failed', error_msg=%s WHERE id=%s",
                           (str(e)[:200], task["id"]))
                db.commit()
        
        # 清除缓存
        for k in list(_cache.keys()):
            if "/api/" in k:
                del _cache[k]

        # 导入完成后自动检测新导入资源的链接有效性
        _auto_scan_unchecked_bg(50)

        return jsonify({"ok": True, "processed": processed, "total_pending": len(tasks)})
    finally:
        db.close()




@app.route("/api/admin/logs/clear", methods=["POST"])
@admin_required
def api_admin_logs_clear():
    """清空操作日志"""
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("DELETE FROM operation_logs")
        db.commit()
        return jsonify({"ok": True, "deleted": cur.rowcount})
    finally:
        db.close()

# ==================== API 测试工具 ====================
@app.route("/api/admin/test_apis")
@admin_required
def api_admin_test_apis():
    """测试所有API端点是否正常"""
    import time as _time
    results = []
    db = get_db()
    try:
        cur = db.cursor()
        
        # Test each API
        tests = [
            ("GET", "/api/stats", lambda: cur.execute("SELECT COUNT(*) as c FROM resources")),
            ("GET", "/api/sources", lambda: cur.execute("SELECT DISTINCT source FROM resources LIMIT 1")),
            ("GET", "/api/admin/settings", lambda: cur.execute("SELECT COUNT(*) as c FROM settings")),
            ("GET", "/api/admin/import_queue", lambda: cur.execute("SELECT COUNT(*) as c FROM import_queue")),
            ("GET", "/api/admin/stats/dashboard", lambda: cur.execute("SELECT COUNT(*) as c FROM resources")),
            ("GET", "/api/admin/resources", lambda: cur.execute("SELECT COUNT(*) as c FROM resources")),
            ("GET", "/api/admin/pending_imports", lambda: cur.execute("SELECT COUNT(*) as c FROM search_logs WHERE is_imported=0")),
            ("GET", "/api/admin/logs", lambda: cur.execute("SELECT COUNT(*) as c FROM operation_logs")),
            ("GET", "/api/admin/filter_words", lambda: cur.execute("SELECT COUNT(*) as c FROM filter_words")),
            ("GET", "/api/tmdb/trending", lambda: None),  # External API
            ("GET", "/api/hot_searches", lambda: cur.execute("SELECT keyword FROM search_logs LIMIT 1")),
            ("GET", "/api/autocomplete?q=test", lambda: cur.execute("SELECT 1")),
            ("GET", "/api/docs", lambda: None),
        ]
        
        for method, path, db_check in tests:
            start = _time.time()
            try:
                db_check()
                elapsed = round((_time.time() - start) * 1000, 1)
                results.append({"method": method, "path": path, "status": "ok", "ms": elapsed})
            except Exception as e:
                elapsed = round((_time.time() - start) * 1000, 1)
                results.append({"method": method, "path": path, "status": "error", "error": str(e)[:100], "ms": elapsed})
        
        # DB connection test
        start = _time.time()
        cur.execute("SELECT 1")
        db_ms = round((_time.time() - start) * 1000, 1)
        
        # Table stats
        tables = {}
        for table in ["resources", "users", "search_logs", "favorites", "filter_words", "settings", "import_queue", "operation_logs"]:
            try:
                cur.execute(f"SELECT COUNT(*) as c FROM {table}")
                tables[table] = cur.fetchone()["c"]
            except:
                tables[table] = -1
        
        return jsonify({
            "results": results,
            "db_latency_ms": db_ms,
            "tables": tables,
            "total_ok": sum(1 for r in results if r["status"] == "ok"),
            "total_error": sum(1 for r in results if r["status"] == "error"),
        })
    finally:
        db.close()



# ==================== 失效链接检测 ====================


def _extract_id(url, source):
    """从URL提取分享ID"""
    if "quark" in source:
        m = re.search(r'/s/([a-zA-Z0-9]+)', url)
        return m.group(1) if m else None
    elif "baidu" in source or "bdpan" in source:
        m = re.search(r'/s/([a-zA-Z0-9_-]+)', url)
        return m.group(1) if m else None
    elif "aliyun" in source or "alipan" in source:
        m = re.search(r'/s/([a-zA-Z0-9]+)', url)
        return m.group(1) if m else None
    elif "xunlei" in source:
        m = re.search(r'/s/([a-zA-Z0-9_-]+)', url)
        return m.group(1) if m else None
    elif "115" in source:
        m = re.search(r'/s/([a-zA-Z0-9]+)', url)
        return m.group(1) if m else None
    elif "pikpak" in source:
        m = re.search(r'/s/([a-zA-Z0-9]+)', url)
        return m.group(1) if m else None
    return None


def _check_single_link(url, source):
    """根据网盘类型使用API精准检测"""
    share_id = _extract_id(url, source)
    if not share_id:
        return "unknown", 0, "无法提取分享ID"

    try:
        # 夸克网盘
        if "quark" in source:
            r = http_requests.post(
                "https://drive-pc.quark.cn/1/clouddrive/share/sharepage/token",
                json={"pwd_id": share_id, "passcode": ""},
                timeout=10, headers={"User-Agent": "Mozilla/5.0"}
            )
            if r.status_code == 404:
                msg = r.json().get("message", "")
                if "失效" in msg or "不存在" in msg:
                    return "dead", 404, msg
            return "alive", r.status_code, ""

        # 百度网盘
        elif "baidu" in source or "bdpan" in source:
            r = http_requests.post(
                "https://pan.baidu.com/share/verify",
                data={"surl": share_id, "pwd": ""},
                timeout=10, headers={"User-Agent": "Mozilla/5.0"}
            )
            d = r.json()
            errno = d.get("errno", -1)
            if errno == -12 or errno == -9:
                return "dead", r.status_code, "分享已失效"
            elif errno == 0 or errno == -6:
                return "alive", r.status_code, ""
            return "unknown", r.status_code, f"errno={errno}"

        # 阿里云盘
        elif "aliyun" in source or "alipan" in source:
            # 先提取真实share_id (alipan.com/s/xxx 中的xxx)
            r = http_requests.post(
                "https://api.aliyundrive.com/adrive/v3/share_link/get_share_by_anonymous",
                json={"share_id": share_id},
                timeout=10, headers={"User-Agent": "Mozilla/5.0"}
            )
            if r.status_code == 404:
                msg = r.json().get("message", "")
                if "not found" in msg.lower() or "cannot" in msg.lower():
                    return "dead", 404, msg
            elif r.status_code == 200:
                return "alive", 200, ""
            return "unknown", r.status_code, ""

        # 迅雷/115/pikpak/其他 - 用HTTP GET检查页面
        else:
            r = http_requests.get(url, timeout=10, allow_redirects=True,
                                 headers={"User-Agent": "Mozilla/5.0"})
            body = r.text[:2000].lower()
            dead_words = ['已失效', '已过期', '已删除', '不存在', '取消分享', '违规', '侵权', '无法访问', '该分享内容',
                          '页面不存在', '资源不存在', '分享已过期', '链接已失效',
                          '文件已删除', '该分享已过期', '此分享已过期', 'expired',
                          'removed', 'not found']
            for word in dead_words:
                if word in body:
                    return "dead", r.status_code, f"页面包含'{word}'"
            if r.status_code == 200:
                return "alive", 200, ""
            return "unknown", r.status_code, ""

    except http_requests.exceptions.Timeout:
        return "timeout", 0, "请求超时"
    except http_requests.exceptions.ConnectionError:
        return "dead", 0, "连接失败"
    except Exception as e:
        return "error", 0, str(e)[:50]


@app.route("/api/admin/deadlinks/scan", methods=["POST"])
@admin_required
def api_admin_deadlinks_scan():
    """扫描失效链接（网盘链接，排除磁力/电驴）- 使用各网盘API精准检测"""
    data = request.get_json() or {}
    source_filter = data.get("source", "")
    limit = min(int(data.get("limit", 50)), 1000)
    page = int(data.get("page", 1))
    skip_checked = data.get("skip_checked", True)  # 默认跳过已检测
    only_unchecked = data.get("only_unchecked", False)  # 只检测未检测的
    rescan_alive = data.get("rescan_alive", False)  # 重新扫描之前正常的

    db = get_db()
    try:
        cur = db.cursor()
        exclude_sources = "('magnet', 'ed2k', 'other')"
        conditions = [f"source NOT IN {exclude_sources}"]
        params = []

        if source_filter:
            conditions.append("source = %s")
            params.append(source_filter)

        if only_unchecked:
            conditions.append("last_checked IS NULL")
        elif rescan_alive:
            conditions.append("link_status = 'alive' AND last_checked < DATE_SUB(NOW(), INTERVAL 7 DAY)")
        elif skip_checked:
            conditions.append("(last_checked IS NULL OR last_checked < DATE_SUB(NOW(), INTERVAL 7 DAY))")

        where = " AND ".join(conditions)
        offset = (page - 1) * limit

        cur.execute(
            f"SELECT id, title, source, url, password FROM resources "
            f"WHERE {where} ORDER BY created_at DESC LIMIT %s OFFSET %s",
            params + [limit, offset]
        )
        items = cur.fetchall()

        cur.execute(f"SELECT COUNT(*) as c FROM resources WHERE {where}", params)
        total = cur.fetchone()["c"]

        results = []
        dead_count = 0
        alive_count = 0
        unknown_count = 0

        for item in items:
            url = item.get("url", "")
            source = (item.get("source") or "").lower()

            if not url:
                status, code, msg = "invalid", 0, "空链接"
                unknown_count += 1
            elif url.startswith("magnet:") or url.startswith("ed2k:"):
                status, code, msg = "skip", 0, ""
            else:
                status, code, msg = _check_single_link(url, source)
                if status == "dead":
                    dead_count += 1
                elif status == "alive":
                    alive_count += 1
                else:
                    unknown_count += 1

            results.append({
                "id": item["id"],
                "title": (item.get("title") or "")[:80],
                "source": item.get("source", ""),
                "url": url[:100],
                "password": item.get("password", ""),
                "status": status,
                "status_code": code,
                "msg": msg,
            })

            # 标记已检测
            if status not in ("skip", "invalid"):
                try:
                    cur.execute(
                        "UPDATE resources SET last_checked=NOW(), link_status=%s WHERE id=%s",
                        (status, item["id"])
                    )
                except Exception:
                    pass

        db.commit()  # 提交所有标记更新

        return jsonify({
            "results": results,
            "total": total,
            "page": page,
            "limit": limit,
            "dead_count": dead_count,
            "alive_count": alive_count,
            "unknown_count": unknown_count,
            "scanned": len(items),
        })
    finally:
        db.close()


@app.route("/api/admin/deadlinks/delete", methods=["POST"])
@admin_required
def api_admin_deadlinks_delete():
    """批量删除失效链接"""
    data = request.get_json() or {}
    ids = data.get("ids", [])
    if not ids:
        return jsonify({"error": "请选择要删除的链接"}), 400

    db = get_db()
    try:
        cur = db.cursor()
        placeholders = ",".join(["%s"] * len(ids))
        cur.execute(f"DELETE FROM resources WHERE id IN ({placeholders})", ids)
        db.commit()
        _invalidate_resource_cache()
        return jsonify({"ok": True, "deleted": cur.rowcount})
    finally:
        db.close()


@app.route("/api/admin/deadlinks/sources")
@admin_required
def api_admin_deadlinks_sources():
    """获取可检测的网盘来源列表"""
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            "SELECT source, COUNT(*) as c FROM resources "
            "WHERE source NOT IN ('magnet', 'ed2k', 'other') "
            "GROUP BY source ORDER BY c DESC"
        )
        items = cur.fetchall()
        return jsonify({"items": items})
    finally:
        db.close()



@app.route("/api/admin/deadlinks/unchecked_count")
@admin_required
def api_admin_deadlinks_unchecked_count():
    """获取未检测链接数量"""
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            "SELECT source, COUNT(*) as c FROM resources "
            "WHERE source NOT IN ('magnet', 'ed2k', 'other') AND last_checked IS NULL "
            "GROUP BY source ORDER BY c DESC"
        )
        items = cur.fetchall()
        total = sum(i["c"] for i in items)
        return jsonify({"items": items, "total": total})
    finally:
        db.close()


@app.route("/api/admin/deadlinks/auto_scan", methods=["POST"])
@admin_required
def api_admin_deadlinks_auto_scan():
    """一键自动检测：批量扫描+标记+删除失效链接"""
    import threading

    data = request.get_json() or {}
    batch_size = min(int(data.get("batch_size", 100)), 500)
    max_batches = min(int(data.get("max_batches", 10)), 500)  # 最多500轮
    auto_delete = data.get("auto_delete", False)  # 自动删除失效链接

    def _auto_scan_worker(batch_size, max_batches, auto_delete):
        total_scanned = 0
        total_dead = 0
        total_alive = 0

        for batch_num in range(max_batches):
            try:
                db = get_db()
                cur = db.cursor()
                # 取未检测的链接
                cur.execute(
                    "SELECT id, title, source, url, password FROM resources "
                    "WHERE source NOT IN ('magnet', 'ed2k', 'other') AND last_checked IS NULL "
                    "ORDER BY id LIMIT %s", (batch_size,)
                )
                items = cur.fetchall()
                db.close()

                if not items:
                    break  # 全部检测完毕

                # 逐条检测
                dead_ids = []
                for item in items:
                    url = item.get("url", "")
                    source = (item.get("source") or "").lower()
                    status, code, msg = _check_single_link(url, source)

                    # 标记
                    try:
                        db = get_db()
                        cur = db.cursor()
                        cur.execute(
                            "UPDATE resources SET last_checked=NOW(), link_status=%s WHERE id=%s",
                            (status, item["id"])
                        )
                        db.commit()
                        db.close()
                    except:
                        pass

                    if status == "dead":
                        total_dead += 1
                        dead_ids.append(item["id"])
                    elif status == "alive":
                        total_alive += 1
                    total_scanned += 1

                # 自动删除失效链接
                if auto_delete and dead_ids:
                    try:
                        db = get_db()
                        cur = db.cursor()
                        placeholders = ",".join(["%s"] * len(dead_ids))
                        cur.execute(f"DELETE FROM resources WHERE id IN ({placeholders})", dead_ids)
                        db.commit()
                        db.close()
                        _invalidate_resource_cache()
                    except:
                        pass

                # 记录日志
                try:
                    db = get_db()
                    cur = db.cursor()
                    cur.execute(
                        "INSERT INTO operation_logs (action, detail) VALUES (%s, %s)",
                        ("auto_scan", f"批次{batch_num+1}: 扫描{len(items)}条, 存活{len(items)-len(dead_ids)}, 失效{len(dead_ids)}")
                    )
                    db.commit()
                    db.close()
                except:
                    pass

            except Exception as e:
                pass

        # 最终日志
        try:
            db = get_db()
            cur = db.cursor()
            cur.execute(
                "INSERT INTO operation_logs (action, detail) VALUES (%s, %s)",
                ("auto_scan_done", f"完成: 共扫描{total_scanned}条, 存活{total_alive}, 失效{total_dead}" + (f", 已删除{total_dead}" if auto_delete else ""))
            )
            db.commit()
            db.close()
        except:
            pass

    # 启动后台线程
    t = threading.Thread(target=_auto_scan_worker, args=(batch_size, max_batches, auto_delete), daemon=True)
    t.start()

    return jsonify({
        "ok": True,
        "message": f"自动检测已启动：每批{batch_size}条, 最多{max_batches}批",
        "auto_delete": auto_delete
    })

# ==================== API Docs Page ====================

@app.route("/api-docs")
@app.route("/api-docs.html")
def api_docs_page():
    return send_from_directory(".", "api-docs.html")


# ==================== TMDB Crawler ====================

_tmdb_crawler_status = {"running": False, "progress": 0, "total": 0, "imported": 0, "log": []}

@app.route("/api/admin/tmdb/crawl", methods=["POST"])
@admin_required
def api_tmdb_crawl():
    """启动 TMDB 爬虫（subprocess）"""
    if _tmdb_crawler_status["running"]:
        return jsonify({"ok": False, "error": "爬虫正在运行中"})
    _tmdb_crawler_status.update({"running": True, "progress": 0, "total": 0, "imported": 0, "log": []})
    import subprocess
    subprocess.Popen(
        ["python3", "/app/tmdb_crawler_web.py"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True
    )
    return jsonify({"ok": True, "message": "爬虫已启动"})


@app.route("/api/admin/tmdb/status")
@admin_required
def api_tmdb_status():
    """获取 TMDB 爬虫状态（从 JSON 文件读取）"""
    try:
        with open("/app/data/tmdb_status.json", "r") as f:
            return jsonify(json.load(f))
    except:
        return jsonify(_tmdb_crawler_status)


def _tmdb_crawler_worker():
    """TMDB 爬虫后台工作线程"""
    import socket as _sock
    import ssl as _ssl
    import requests as _req
    import json as _json

    TMDB_KEY = API_KEY = _get_tmdb_api_key()
    SEARCH_URL = _get_import_api_urls()[0]
    endpoints = {
        "趋势(日)": "trending/all/day",
        "趋势(周)": "trending/all/week",
        "电影-热门": "movie/popular",
        "电影-影院": "movie/now_playing",
        "电影-即将": "movie/upcoming",
        "剧集-热门": "tv/popular",
        "剧集-今日播出": "tv/airing_today",
        "剧集-正在播出": "tv/on_the_air",
    }

    def log(m):
        ts = datetime.now().strftime("%H:%M:%S")
        _tmdb_crawler_status["log"].append(f"[{ts}] {m}")

    def tmdb_get(ep, p=1):
        try:
            ip = _sock.getaddrinfo("www.themoviedb.org", 443, _sock.AF_INET)[0][4][0]
            path = f"/3/{ep}?api_key={TMDB_KEY}&language=zh-CN&page={p}"
            s = _sock.create_connection((ip, 443), 10)
            ss = _ssl.create_default_context().wrap_socket(s, server_hostname="api.themoviedb.org")
            ss.sendall(f"GET {path} HTTP/1.1\r\nHost: api.themoviedb.org\r\nAccept: application/json\r\nConnection: close\r\n\r\n".encode())
            r = b""
            while True:
                c = ss.recv(65536)
                if not c: break
                r += c
            ss.close()
            h = r.find(b"\r\n\r\n")
            if h < 0: return None
            body = r[h+4:]
            if b"chunked" in r[:h]:
                lines = body.split(b"\r\n")
                decoded = b""
                for line in lines:
                    if line and not all(c in b"0123456789abcdefABCDEF" for c in line[:8]):
                        decoded += line
                body = decoded
            return _json.loads(body.decode())
        except:
            return None

    try:
        # Phase 1: Fetch TMDB titles
        log("📡 Phase 1: 获取 TMDB 热门标题...")
        titles = []
        seen = set()
        for label, ep in endpoints.items():
            log(f"  [{label}] ...")
            data = tmdb_get(ep)
            if not data:
                log(f"  ✗ 获取失败")
                continue
            count = 0
            for item in data.get("results", []):
                t = (item.get("title") or item.get("name", "")).strip()
                k = t.lower()
                if t and k not in seen:
                    seen.add(k)
                    titles.append(t)
                    count += 1
            log(f"  +{count} (共{len(seen)})")
            time.sleep(0.3)

        _tmdb_crawler_status["total"] = len(titles)
        log(f"📊 共{len(titles)}个标题\n")

        # Phase 2: Search and import
        log("🔍 Phase 2: 搜索网盘资源并导入...")
        db = get_db()
        cur = db.cursor()
        imported = 0

        for i, title in enumerate(titles, 1):
            _tmdb_crawler_status["progress"] = i
            log(f"[{i}/{len(titles)}] 🔍 {title}")
            try:
                resp = _req.post(SEARCH_URL, json={"kw": title, "limit": 30}, timeout=60)
                result = resp.json()
                if result.get("code") != 0:
                    continue
                merged = result.get("data", {}).get("merged_by_type", {})
                items = []
                for src, its in merged.items():
                    items.extend(its)
                if not items:
                    log("  ℹ️ 无结果")
                    continue

                added = 0
                nw = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                for it in items:
                    url = it.get("url", "")
                    if not url:
                        continue
                    cur.execute("SELECT 1 FROM resources WHERE url=%s", (url,))
                    if cur.fetchone():
                        continue
                    title_text = it.get("note") or it.get("title") or title
                    src = (it.get("source") or "other").lower()
                    cur.execute(
                        "INSERT INTO resources(url,title,keyword,password,source,datetime,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                        (url, title_text, title, it.get("password", ""), src, nw, nw)
                    )
                    added += 1
                db.commit()
                imported += added
                _tmdb_crawler_status["imported"] = imported
                log(f"  ✅ +{added}" if added else "  ℹ️ 已存在")
            except Exception as e:
                log(f"  ✗ 错误: {str(e)[:60]}")
            time.sleep(0.3)

        cur.close()
        db.close()
        log(f"\n🎉 完成！共{len(titles)}个标题，新增{imported}条资源")
    except Exception as e:
        log(f"\n❌ 爬虫异常: {e}")
    finally:
        _tmdb_crawler_status["running"] = False


# ==================== Multi-Source Crawler ====================

_multi_crawler_status = {"running": False, "phase": "", "progress": 0, "total": 0, "imported": 0, "log": []}

@app.route("/api/admin/multi-crawl", methods=["POST"])
@admin_required
def api_multi_crawl():
    """启动多源爬虫（TVmaze + Trakt + 豆瓣 + TMDB）"""
    if _multi_crawler_status["running"]:
        return jsonify({"ok": False, "error": "多源爬虫正在运行中"})
    _multi_crawler_status.update({"running": True, "phase": "启动中", "progress": 0, "total": 0, "imported": 0, "log": []})
    with open("/app/data/multi_source_status.json", "w") as f:
        json.dump(_multi_crawler_status, f)
    import subprocess
    subprocess.Popen(
        ["python3", "-u", "/app/multi_source_crawler_web.py"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True
    )
    return jsonify({"ok": True, "message": "多源爬虫已启动（TVmaze+Trakt+豆瓣+TMDB）"})


@app.route("/api/admin/multi-crawl/status")
@admin_required
def api_multi_crawl_status():
    """获取多源爬虫状态"""
    try:
        with open("/app/data/multi_source_status.json", "r") as f:
            return jsonify(json.load(f))
    except:
        return jsonify(_multi_crawler_status)


# ==================== OMDb Rating Crawler ====================

_omdb_crawler_status = {"running": False, "progress": 0, "total": 0, "success": 0, "failed": 0, "log": []}

@app.route("/api/admin/omdb/crawl", methods=["POST"])
@admin_required
def api_omdb_crawl():
    """启动OMDb评分爬虫（IMDb+烂番茄+Metacritic）"""
    if _omdb_crawler_status["running"]:
        return jsonify({"ok": False, "error": "评分爬虫正在运行中"})
    _omdb_crawler_status.update({"running": True, "progress": 0, "total": 0, "success": 0, "failed": 0, "log": []})
    with open("/app/data/omdb_status.json", "w") as f:
        json.dump(_omdb_crawler_status, f)
    import subprocess
    subprocess.Popen(
        ["python3", "-u", "/app/omdb_rating_crawler_web.py"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True
    )
    return jsonify({"ok": True, "message": "OMDb评分爬虫已启动（IMDb+烂番茄+Metacritic）"})


@app.route("/api/admin/omdb/status")
@admin_required
def api_omdb_status():
    """获取OMDb评分爬虫状态"""
    try:
        with open("/app/data/omdb_status.json", "r") as f:
            return jsonify(json.load(f))
    except:
        return jsonify(_omdb_crawler_status)


# ==================== Static Files ====================

@app.route("/site-settings.js")
def site_settings_js():
    return send_from_directory(".", "site-settings.js")


@app.route("/favicon.ico")
@app.route("/favicon.svg")
def favicon():
    """返回 VaultDrive 自定义 SVG favicon（金色渐变 + 闪电 ⚡）"""
    return send_from_directory(".", "favicon.svg", mimetype="image/svg+xml")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
