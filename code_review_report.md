# /vol2/1000/docker/resource_web/ 代码质量审查报告

## 审查概要
- **项目路径**: /vol2/1000/docker/resource_web/
- **技术栈**: Flask + MySQL + Redis + Docker + 多爬虫系统
- **主要文件**: app.py (2986行), tmdb_crawler.py (419行), multi_source_crawler.py (717行), omdb_rating_crawler.py (213行), software_game_crawler.py (357行)
- **审查日期**: 2026-06-29

---

## 🔴 阻塞项 (Blockers - 必须修复)

### 1. SQL 语法错误：未闭合括号导致查询崩溃
**文件**: `app.py`  
**行号**: 2490  
**严重程度**: P0 - 功能完全失效

```python
conditions.append("link_status = 'alive' AND last_checked < DATE_SUB(NOW(), INTERVAL 7 DAY))")
```
该行多了一个右括号 `)`，生成SQL时会导致语法错误，`/api/admin/deadlinks/scan` 接口在 `rescan_alive=True` 时会直接崩溃。

**修复建议**: 删除多余的右括号。

---

### 2. 批量删除/更新时空列表导致 SQL 语法错误
**文件**: `app.py`  
**行号**: 1800-1801, 2479-2500  
**严重程度**: P0 - 运行时崩溃

```python
placeholders = ",".join(["%s"] * len(ids))
where = f"WHERE id IN ({placeholders})"
```
当 `ids=[]` 时，生成 `WHERE id IN ()`，MySQL 会报语法错误。前端如果传空数组会直接触发 500。

**修复建议**:
```python
if not ids:
    return jsonify({"error": "请选择有效ID"}), 400
placeholders = ",".join(["%s"] * len(ids))
```

---

### 3. 内存缓存/限流器非线程安全
**文件**: `app.py`  
**行号**: 246, 304, 320-340  
**严重程度**: P1 - 生产环境数据竞争

全局字典 `_rate_limits`、`_cache`、`_login_failures` 在 Gunicorn 多 worker + 内部 `threading.Thread` 环境下存在竞态条件。Python 字典操作不是原子性的，高并发下可能出现 `RuntimeError: dictionary changed size during iteration` 或数据丢失。

**修复建议**:
- 使用 `threading.Lock` 保护所有全局缓存读写
- 或迁移到线程安全的 `cachetools.LRUCache` / Redis 分布式缓存

---

### 4. TMDB 爬虫网络超时设计缺陷
**文件**: `tmdb_crawler.py`, `tmdb_crawler_web.py`, `app.py`  
**行号**: tmdb_crawler.py:108, tmdb_crawler_web.py:108, app.py:1387  
**严重程度**: P1 - 已知故障源

```python
s = socket.create_connection((ip, 443), 10)  # 连接超时10秒
ss.settimeout(15)  # 读取超时15秒
```
- 手动实现 HTTP/1.1 协议解析（chunked transfer decoding）脆弱，边界条件处理不完整
- 没有连接池，每个请求新建 TCP + TLS 握手，延迟高
- DNS 解析缓存 `_TMDB_IP` 无 TTL，IP 变更后永久失效

**修复建议**:
- 使用 `requests` 库 + `requests.adapters.HTTPAdapter` 连接池
- 或使用 `urllib3` + 正确的超时策略
- 为 DNS 缓存添加 TTL（如 1 小时）

---

### 5. 密码迁移逻辑存在死锁/事务问题
**文件**: `app.py`  
**行号**: 388-397  
**严重程度**: P1 - 数据一致性风险

```python
db2 = get_db()
try:
    db2.cursor().execute("UPDATE users SET password=%s WHERE id=%s", (new_hash, user["id"]))
    db2.commit()
except Exception:
    pass
finally:
    db2.close()
```
- 登录成功后才迁移密码，但 `except: pass` 会静默迁移失败
- 每次登录都尝试迁移，无防重入机制，可能对同一用户并发迁移
- 旧 SHA256 密码在验证成功即返回 True，但外层又判断 `if not verify_password(...)` 进入失败分支，逻辑可读性差（第386-399行）

---

### 6. 搜索逻辑不一致：遗漏 keyword 字段
**文件**: `app.py`  
**行号**: 536-537  
**严重程度**: P1 - 功能缺陷

```python
# api_resources 搜索
conditions.append("(title LIKE %s OR note LIKE %s)")
# 但 api_src_tags 搜索了 keyword
conditions.append("(title LIKE %s OR note LIKE %s OR keyword LIKE %s)")
```
用户搜索时，`keyword` 字段（通常是英文/别名关键词）不会被匹配到，导致部分资源搜不到。

**修复建议**: 统一搜索条件为 `(title LIKE %s OR note LIKE %s OR keyword LIKE %s)`。

---

### 7. 导入 API 硬编码外部服务地址
**文件**: `app.py`, `multi_source_crawler.py` 等  
**行号**: app.py:706, 1158; multi_source_crawler.py:21  
**严重程度**: P1 - 运维风险

```python
API_URL = "https://ps.aiqinghaiwork.cn/api/search"
```
- 硬编码第三方 API，若该服务宕机，所有导入功能瘫痪
- 仅单个 URL，无 fallback 机制
- `multi_source_crawler.py` 要求 `SEARCH_API` 环境变量，但 `tmdb_crawler.py` 也有默认值，配置分散

**修复建议**:
- 将 API URL 纳入 `settings` 表管理，支持多 URL fallback
- 添加熔断机制（如 连续失败 N 次后暂停 1 小时）

---

### 8. 爬虫数据库连接未使用连接池
**文件**: `tmdb_crawler.py`, `multi_source_crawler.py`, `omdb_rating_crawler.py`, `software_game_crawler.py`  
**行号**: 各文件的 `import_db` 函数  
**严重程度**: P1 - 性能与稳定性

所有爬虫脚本都直接 `pymysql.connect()` 创建新连接，无连接池。批量导入时频繁创建/销毁连接，可能导致：
- MySQL 连接数耗尽
- 导入性能低下

**修复建议**: 复用 `app.py` 中的 `PooledDB` 或使用 `DBUtils` 连接池。

---

## 🟡 建议项 (Warnings - 应尽快修复)

### 9. 全局状态在 Gunicorn 多 Worker 下不共享
**文件**: `app.py`  
**行号**: 2747, 2909, 2942  
**严重程度**: P2 - 功能降级

```python
_tmdb_crawler_status = {"running": False, ...}
```
Gunicorn 启动多个 worker 时，每个 worker 进程有独立的内存空间。管理员在 A worker 启动爬虫，B worker 的 `/api/admin/tmdb/status` 接口看不到运行状态。

**修复建议**:
- 使用 Redis 存储爬虫状态
- 或使用文件锁 + 共享文件系统（当前已用 JSON 文件，但进程间仍有 race）

---

### 10. 过滤词 SQL 构造 N+1 性能问题
**文件**: `app.py`  
**行号**: 626-636  
**严重程度**: P2 - 性能瓶颈

```python
for w in fwords:
    conds.append("(title NOT LIKE %s AND note NOT LIKE %s AND keyword NOT LIKE %s)")
    params.extend([f"%{w}%", f"%{w}%", f"%{w}%"])
```
若有 100 个过滤词，生成 300 个 `LIKE` 条件，SQL 执行计划可能退化为全表扫描。每次资源列表查询都动态拼接大 SQL。

**修复建议**:
- 使用全文索引（MySQL FULLTEXT）替代多个 `LIKE`
- 或使用专用过滤词表 + JOIN

---

### 11. 自动导入后台线程无超时控制
**文件**: `app.py`  
**行号**: 2096-2116  
**严重程度**: P2 - 资源泄漏

```python
t = threading.Thread(target=fetch_one, args=(url, i))
threads.append(t)
t.start()
for t in threads:
    t.join(timeout=90)
```
线程 join 超时后，线程仍在后台运行。若用户频繁搜索，会积累大量僵尸线程。

**修复建议**: 使用 `concurrent.futures.ThreadPoolExecutor` 并设置最大线程数。

---

### 12. 图片代理缓存无容量淘汰的原子性
**文件**: `app.py`  
**行号**: 125-148  
**严重程度**: P2 - 内存泄漏风险

```python
_img_cache[url] = (r.content, ct, now + 86400)
_img_cache_evict()
```
在高并发下，`_img_cache` 可能超过 `_IMG_CACHE_MAX` 很多才被清理。且 `OrderedDict` 不是线程安全的。

**修复建议**: 使用 `cachetools.LRUCache` 或在 Redis 中缓存图片。

---

### 13. Docker Compose 配置错误：重复环境变量
**文件**: `docker-compose.yml`  
**行号**: 25, 29-32  
**严重程度**: P2 - 配置混乱

```yaml
- DB_PASSWORD=***      - DB_NAME=pan_resource   # 第25行：格式错误，*** 是占位符
- DB_PORT=3306          # 第29行：重复定义
- DB_USER=root          # 第30行：重复定义
- DB_PASSWORD=resource_db_2026  # 第31行：覆盖了上面的***
- DB_NAME=pan_resource   # 第32行：重复定义
```
第25行存在语法错误（`***` 后缺少换行），且环境变量重复定义。

**修复建议**: 清理重复项，使用 `.env` 文件管理敏感信息。

---

### 14. CSRF 保护对 API 的兼容性可能过度宽松
**文件**: `app.py`  
**行号**: 82, 350  
**严重程度**: P2 - 安全风险

```python
app.config['WTF_CSRF_HEADERS'] = ['X-CSRFToken', 'X-CSRF-Token']
```
- 允许两种大小写变体，但前端可能只发送一种
- `admin_required` 装饰器对 API 请求返回 JSON，对浏览器请求重定向，逻辑正确但可进一步细化

**建议**: 统一使用 `X-CSRFToken`，移除 `X-CSRF-Token`。

---

### 15. 日志记录使用裸 `except: pass`
**文件**: `app.py`  
**行号**: 50-51, 1117, 2025-2026, 2057-2058, 2711-2712  
**严重程度**: P2 - 可观测性差

```python
except Exception:
    pass  # 静默忽略所有 Redis 错误
```
多处使用裸异常捕获，导致 Redis 故障、数据库写入失败等问题无法被及时发现。

**修复建议**: 至少使用 `logging` 模块记录异常堆栈。

---

## 💭 改进项 (Suggestions - 技术债)

### 16. 代码重复：爬虫逻辑高度重复
**文件**: `tmdb_crawler.py` / `tmdb_crawler_web.py` / `multi_source_crawler.py` / `multi_source_crawler_web.py`  
**行号**: 全部  
**严重程度**: P3 - 维护成本

- `tmdb_crawler.py` 和 `tmdb_crawler_web.py` 代码重复度 >90%
- `multi_source_crawler.py` 和 `multi_source_crawler_web.py` 代码重复度 >80%
- 每个爬虫都有自己的 `search_api`、`import_db`、`log` 函数

**修复建议**: 提取公共模块 `crawler_base.py`，包含：
- 统一的数据库连接池
- 统一的搜索 API 客户端（带重试、熔断）
- 统一的导入逻辑

---

### 17. 内存缓存策略简单，命中率低
**文件**: `app.py`  
**行号**: 304-340  
**严重程度**: P3 - 性能优化空间

- 内存缓存 `_cache` 基于 `request.url` 作为 key，但搜索 API 的 URL 可能包含变化的参数顺序
- FIFO 淘汰策略（第314-318行）不合理：清理过期后如果仍超限，只保留最新的一半，直接丢弃一半缓存

**修复建议**:
- 使用 `functools.lru_cache` 或 `cachetools.LRUCache`
- 统一 URL 参数顺序后再做 cache key
- 设置合理的 `maxsize` 和 `ttu`（time-to-use）

---

### 18. Redis 缓存键设计不一致
**文件**: `app.py`  
**行号**: 41-43, 522, 597  
**严重程度**: P3 - 维护困难

```python
def _make_cache_key(*args):
    raw = "|".join(str(a) for a in args)
    return "res_web:" + hashlib.md5(raw.encode()).hexdigest()
```
- 资源列表使用 `res_web:resources:...` 格式
- 过滤词使用 `_filter_words`（无前缀）
- 图片代理使用内存缓存，未用 Redis

**修复建议**: 统一缓存键命名规范，如 `rw:cache:resources:...`。

---

### 19. 连接池配置可能不足
**文件**: `app.py`  
**行号**: 202-212  
**严重程度**: P3 - 高并发瓶颈

```python
_pool = PooledDB(
    creator=pymysql,
    maxconnections=20,
    mincached=2,
    maxcached=5,
    blocking=True,
    ...
)
```
- `maxconnections=20` 对于 4 worker + 后台线程的场景可能不足
- `blocking=True` 意味着连接耗尽时会阻塞，导致请求堆积

**修复建议**: 根据压测结果调整 `maxconnections`，建议设置为 `workers * threads * 2 + 10`。

---

### 20. 自动导入触发时机过于激进
**文件**: `app.py`  
**行号**: 1959-2001  
**严重程度**: P3 - 资源浪费

```python
payload = {
    ...
    "importing": _auto_import_trigger(query) if total == 0 and query else False,
}
```
用户每次搜索无结果时都会触发后台导入，包括未登录用户。这可能导致：
- 大量无效的 API 调用（浪费第三方 API 配额）
- 数据库插入大量低质量资源

**修复建议**: 仅登录用户触发自动导入，或添加全局开关 + 频率限制。

---

### 21. 前端静态文件无版本化缓存
**文件**: `app.py`  
**行号**: 155-162  
**严重程度**: P3 - 用户体验

```python
if response.status_code == 200 and not response.headers.get("Cache-Control"):
    response.headers["Cache-Control"] = "public, max-age=300"
```
静态文件缓存 5 分钟，但前端 HTML 设置了 no-cache。建议对静态资源使用内容哈希文件名（如 `app.abc123.css`）以实现长期缓存。

---

### 22. 缺少输入验证和序列化
**文件**: `app.py`  
**行号**: 多处  
**严重程度**: P3 - 健壮性

- `request.args.get("page", 1, type=int)` 虽做了类型转换，但未校验范围（如负数、超大值）
- `request.get_json()` 未验证 schema，可能收到畸形数据

**修复建议**: 使用 `marshmallow` 或 `pydantic` 做请求验证。

---

### 23. 日志记录不规范
**文件**: 全局  
**严重程度**: P3 - 可观测性

- 使用 `print()` 而非 `logging` 模块
- 无结构化日志（JSON 格式）
- 无日志级别控制

**修复建议**: 使用 Python `logging` 模块，输出 JSON 格式日志到 stdout，由 Docker 日志驱动收集。

---

### 24. 测试覆盖率为零
**文件**: 全局  
**严重程度**: P3 - 质量保障

- `test_crawler.py` 存在但可能未维护
- 无单元测试、集成测试
- 关键路径（登录、导入、搜索）无测试

**修复建议**: 为 `app.py` 的 API 路由添加 pytest + Flask test client 测试。

---

## 重点问题总结

| 优先级 | 问题 | 影响 |
|--------|------|------|
| P0 | SQL 语法错误 (L2490) | 死链接扫描接口崩溃 |
| P0 | 空列表 IN 子句 (L1800) | 批量操作崩溃 |
| P1 | 全局缓存非线程安全 | 高并发数据竞争 |
| P1 | TMDB 网络超时不可用 | 爬虫功能瘫痪 |
| P1 | 搜索遗漏 keyword 字段 | 搜索结果不全 |
| P1 | 爬虫无连接池 | 性能瓶颈 |
| P2 | 过滤词 N+1 LIKE | 慢查询 |
| P2 | Docker 配置重复/错误 | 部署问题 |
| P2 | 裸 except pass | 故障难发现 |
| P3 | 代码重复率 >80% | 维护噩梦 |

---

## 修复优先级建议

1. **立即修复 (本周)**: 第1、2、6项
2. **短期修复 (2周内)**: 第3、4、7、8项
3. **中期重构 (1个月内)**: 第9、10、11、16项
4. **长期优化 (持续)**: 第17-24项

---

*报告生成时间: 2026-06-29*
