# 多源影视爬虫 v1.0

## 📊 支持的数据源

| 数据源 | 类型 | 需要API Key | 说明 |
|--------|------|-------------|------|
| **TVmaze** | 美剧 | ❌ | 完全免费，无需认证 |
| **Trakt.tv** | 电影/剧集 | ✅ | 免费注册: https://tradt.tv/oauth/applications |
| **豆瓣** | 国内影视 | ❌ | 非官方API，有反爬限制 |
| **TMDB** | 全球影视 | ✅ | 已有Key |

## 🚀 使用方法

### 1. 本地测试版

```bash
# 快速测试（5个标题）
python3 test_crawler.py

# 完整版（所有数据源）
python3 multi_source_crawler.py
```

### 2. Docker Web版

```bash
# 在Docker容器中运行
docker exec -it resource_web python3 multi_source_crawler_web.py
```

### 3. 配置Trakt.tv（可选）

1. 注册 https://trakt.tv/oauth/applications
2. 获取 Client ID
3. 设置环境变量：
   ```bash
   export TRAKT_CLIENT_ID="你的Client ID"
   ```

## 📁 文件说明

| 文件 | 说明 |
|------|------|
| `test_crawler.py` | 快速测试版 |
| `multi_source_crawler.py` | 本地完整版 |
| `multi_source_crawler_web.py` | Docker Web版 |
| `tmdb_crawler.py` | TMDB专用爬虫 |
| `tmdb_crawler_web.py` | TMDB Docker版 |

## 📈 数据量预估

- **TVmaze**: 240+ 剧集/页
- **Trakt.tv**: 50-100 热门作品
- **豆瓣**: 50 热门电影 + 50 热门剧集
- **TMDB**: 400+ 趋势作品

**总计**: 约 800-1000 个作品标题

## ⚠️ 注意事项

1. **豆瓣反爬**: 请求间隔需 >1秒
2. **TMDB绕过GFW**: 使用IP直连方式
3. **数据库去重**: 自动跳过已存在的URL
4. **资源搜索**: 通过 ps.aiqinghaiwork.cn API

## 🔧 扩展其他数据源

### IMDb
- 无官方API
- 可爬取 https://www.imdb.com/chart/
- 需要处理JavaScript渲染

### JustWatch
- 无官方API
- 可爬取 https://www.justwatch.com/
- 需要处理反爬

### Netflix Top 10
- 可爬取 https://top10.netflix.com/
- 每周更新

### 猫眼/灯塔
- 可爬取 https://piaofang.maoyan.com/
- 实时票房数据
