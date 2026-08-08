#!/usr/bin/env python3
"""
多源影视爬虫 v1.0
支持：TVmaze / Trakt.tv / 豆瓣 / TMDB
自动去重 + 搜索资源 + 导入数据库
"""
import os
import sys
import json
import time
import socket
import ssl
import re
from datetime import datetime
from collections import deque

import requests
import pymysql

# ── 配置（优先环境变量，无默认值强制设置） ──
SEARCH_API = os.environ.get("SEARCH_API")
if not SEARCH_API:
    raise RuntimeError("环境变量 SEARCH_API 未设置")
DB_HOST = os.environ.get("DB_HOST", "172.23.0.2")
DB_PORT = int(os.environ.get("DB_PORT", "3306"))
DB_USER = os.environ.get("DB_USER", "root")
DB_PASSWORD = os.environ.get("DB_PASSWORD")
if not DB_PASSWORD:
    raise RuntimeError("环境变量 DB_PASSWORD 未设置")
DB_NAME = os.environ.get("DB_NAME", "pan_resource")

# Trakt.tv API Key
TRAKT_CLIENT_ID = os.environ.get("TRAKT_CLIENT_ID", "")
if not TRAKT_CLIENT_ID:
    log("⚠️ Trakt 未配置 (设置 TRAKT_CLIENT_ID 环境变量)，将跳过 Trakt 源")

# TMDB API Key
TMDB_API_KEY = os.environ.get("TMDB_API_KEY")
if not TMDB_API_KEY:
    raise RuntimeError("环境变量 TMDB_API_KEY 未设置")


def log(m):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


# ══════════════════════════════════════════════════════════════
#  TVmaze API (完全免费，无需认证)
# ══════════════════════════════════════════════════════════════
class TVmazeCrawler:
    """TVmaze - 美剧数据最细，完全免费"""
    
    BASE_URL = "https://api.tvmaze.com"
    
    def fetch_shows(self, pages=5):
        """获取热门剧集列表"""
        shows = []
        for page in range(pages):
            try:
                resp = requests.get(f"{self.BASE_URL}/shows?page={page}", timeout=15)
                data = resp.json()
                for item in data:
                    shows.append({
                        "title": item.get("name", ""),
                        "title_cn": "",
                        "year": (item.get("premiered") or "")[:4],
                        "rating": item.get("rating", {}).get("average"),
                        "source": "TVmaze-热门",
                        "id": item.get("id"),
                        "type": "tv"
                    })
                log(f"  TVmaze 页{page+1}: +{len(data)} (共{len(shows)})")
                time.sleep(0.5)
            except Exception as e:
                log(f"  TVmaze 页{page+1} 失败: {e}")
                break
        return shows
    
    def fetch_schedule(self, country="US"):
        """获取本周播出表"""
        shows = []
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            resp = requests.get(f"{self.BASE_URL}/schedule?country={country}&date={today}", timeout=15)
            data = resp.json()
            seen = set()
            for item in data:
                show = item.get("show", {})
                name = show.get("name", "")
                if name and name not in seen:
                    seen.add(name)
                    shows.append({
                        "title": name,
                        "title_cn": "",
                        "year": (show.get("premiered") or "")[:4],
                        "rating": show.get("rating", {}).get("average"),
                        "source": f"TVmaze-今日播出({country})",
                        "id": show.get("id"),
                        "type": "tv"
                    })
            log(f"  TVmaze 今日播出: {len(shows)} 个")
        except Exception as e:
            log(f"  TVmaze 播出表失败: {e}")
        return shows
    
    def fetch_web(self):
        """获取网络剧/流媒体剧集"""
        shows = []
        try:
            resp = requests.get(f"{self.BASE_URL}/shows?page=0", timeout=15)
            data = resp.json()
            for item in data:
                network = item.get("network") or item.get("webChannel") or {}
                if network.get("name"):
                    shows.append({
                        "title": item.get("name", ""),
                        "title_cn": "",
                        "year": (item.get("premiered") or "")[:4],
                        "rating": item.get("rating", {}).get("average"),
                        "source": f"TVmaze-{network.get('name')}",
                        "id": item.get("id"),
                        "type": "tv"
                    })
        except Exception as e:
            log(f"  TVmaze 网络剧失败: {e}")
        return shows


# ══════════════════════════════════════════════════════════════
#  Trakt.tv API (需要免费注册获取Client ID)
# ══════════════════════════════════════════════════════════════
class TraktCrawler:
    """Trakt.tv - 影迷热度榜，和TMDB互通"""
    
    BASE_URL = "https://api.trakt.tv"
    
    def _headers(self):
        return {
            "Content-Type": "application/json",
            "trakt-api-version": "2",
            "trakt-api-key": TRAKT_CLIENT_ID
        }
    
    def is_available(self):
        return bool(TRAKT_CLIENT_ID)
    
    def fetch_trending(self, media_type="shows", limit=100):
        """获取热播榜"""
        items = []
        try:
            resp = requests.get(
                f"{self.BASE_URL}/{media_type}/trending?limit={limit}",
                headers=self._headers(),
                timeout=15
            )
            data = resp.json()
            for item in data:
                show = item.get(media_type.rstrip("s"), {})
                title = show.get("title", "")
                items.append({
                    "title": title,
                    "title_cn": "",
                    "year": show.get("year"),
                    "watchers": item.get("watchers"),
                    "source": f"Trakt-热播{'剧' if media_type == 'shows' else '电影'}",
                    "id": show.get("ids", {}).get("tmdb"),
                    "type": "tv" if media_type == "shows" else "movie"
                })
            log(f"  Trakt 热播{'剧' if media_type == 'shows' else '电影'}: {len(items)} 个")
        except Exception as e:
            log(f"  Trakt 热播失败: {e}")
        return items
    
    def fetch_popular(self, media_type="shows", limit=100):
        """获取热门榜"""
        items = []
        try:
            resp = requests.get(
                f"{self.BASE_URL}/{media_type}/popular?limit={limit}",
                headers=self._headers(),
                timeout=15
            )
            data = resp.json()
            for show in data:
                title = show.get("title", "")
                items.append({
                    "title": title,
                    "title_cn": "",
                    "year": show.get("year"),
                    "source": f"Trakt-热门{'剧' if media_type == 'shows' else '电影'}",
                    "id": show.get("ids", {}).get("tmdb"),
                    "type": "tv" if media_type == "shows" else "movie"
                })
            log(f"  Trakt 热门{'剧' if media_type == 'shows' else '电影'}: {len(items)} 个")
        except Exception as e:
            log(f"  Trakt 热门失败: {e}")
        return items
    
    def fetch_most_watched(self, media_type="shows", period="weekly", limit=50):
        """获取观看最多榜"""
        items = []
        try:
            resp = requests.get(
                f"{self.BASE_URL}/{media_type}/watched/{period}?limit={limit}",
                headers=self._headers(),
                timeout=15
            )
            data = resp.json()
            for item in data:
                show = item.get(media_type.rstrip("s"), {})
                items.append({
                    "title": show.get("title", ""),
                    "title_cn": "",
                    "year": show.get("year"),
                    "watchers": item.get("watchers"),
                    "source": f"Trakt-{'周' if period == 'weekly' else '月'}观看榜",
                    "id": show.get("ids", {}).get("tmdb"),
                    "type": "tv" if media_type == "shows" else "movie"
                })
            log(f"  Trakt 观看榜: {len(items)} 个")
        except Exception as e:
            log(f"  Trakt 观看榜失败: {e}")
        return items


# ══════════════════════════════════════════════════════════════
#  豆瓣 API (非官方)
# ══════════════════════════════════════════════════════════════
class DoubanCrawler:
    """豆瓣 - 国内首选，TOP250+热门"""
    
    BASE_URL = "https://movie.douban.com"
    
    def _headers(self):
        return {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://movie.douban.com/"
        }
    
    def fetch_top250(self, pages=10):
        """获取TOP250"""
        movies = []
        for page in range(pages):
            try:
                start = page * 25
                resp = requests.get(
                    f"{self.BASE_URL}/top250?start={start}&filter=",
                    headers=self._headers(),
                    timeout=15
                )
                # 解析HTML
                pattern = r'<span class="title">([^<]+)</span>'
                titles = re.findall(pattern, resp.text)
                for title in titles:
                    if "/" not in title:  # 排除英文名
                        movies.append({
                            "title": title.strip(),
                            "title_cn": title.strip(),
                            "source": "豆瓣-TOP250",
                            "type": "movie"
                        })
                log(f"  豆瓣 TOP250 页{page+1}: +{len(titles)//2} (共{len(movies)})")
                time.sleep(1)  # 豆瓣反爬严格，需要慢速
            except Exception as e:
                log(f"  豆瓣 TOP250 页{page+1} 失败: {e}")
                break
        return movies
    
    def fetch_hot(self, media_type="movie"):
        """获取热门电影/剧集"""
        items = []
        try:
            tag = "热门" if media_type == "movie" else "热门"
            resp = requests.get(
                f"{self.BASE_URL}/j/search_subjects?type={media_type}&tag={tag}&page_limit=50&page_start=0",
                headers=self._headers(),
                timeout=15
            )
            data = resp.json()
            for item in data.get("subjects", []):
                items.append({
                    "title": item.get("title", ""),
                    "title_cn": item.get("title", ""),
                    "rating": item.get("rate"),
                    "source": f"豆瓣-热门{'电影' if media_type == 'movie' else '剧集'}",
                    "type": media_type
                })
            log(f"  豆瓣 热门{'电影' if media_type == 'movie' else '剧集'}: {len(items)} 个")
        except Exception as e:
            log(f"  豆瓣 热门失败: {e}")
        return items
    
    def fetch_weekly(self):
        """获取一周口碑榜"""
        items = []
        try:
            resp = requests.get(
                f"{self.BASE_URL}/j/search_subjects?type=movie&tag=豆瓣一周口碑榜&page_limit=20&page_start=0",
                headers=self._headers(),
                timeout=15
            )
            data = resp.json()
            for item in data.get("subjects", []):
                items.append({
                    "title": item.get("title", ""),
                    "title_cn": item.get("title", ""),
                    "rating": item.get("rate"),
                    "source": "豆瓣-一周口碑榜",
                    "type": "movie"
                })
            log(f"  豆瓣 一周口碑榜: {len(items)} 个")
        except Exception as e:
            log(f"  豆瓣 一周口碑榜失败: {e}")
        return items


# ══════════════════════════════════════════════════════════════
#  AniList API (动漫，完全免费，无需认证)
# ══════════════════════════════════════════════════════════════
class AniListCrawler:
    """AniList - 动漫数据最全，完全免费"""

    API_URL = "https://graphql.anilist.co"

    def fetch_popular(self, media_type="ANIME", pages=3):
        items = []
        for page in range(1, pages + 1):
            try:
                query = """
                query ($page: Int, $type: MediaType) {
                    Page(page: $page, perPage: 50) {
                        media(type: $type, sort: POPULARITY_DESC) {
                            title { romaji english native }
                            episodes
                            averageScore
                            startDate { year }
                        }
                    }
                }
                """
                variables = {"page": page, "type": media_type}
                resp = requests.post(self.API_URL, json={"query": query, "variables": variables}, timeout=15)
                data = resp.json()
                media_list = data.get("data", {}).get("Page", {}).get("media", [])
                for m in media_list:
                    title = m["title"]["english"] or m["title"]["romaji"] or m["title"]["native"]
                    year = m["startDate"]["year"] if m.get("startDate") else None
                    items.append({
                        "title": title,
                        "title_cn": m["title"].get("native", ""),
                        "year": year,
                        "rating": m.get("averageScore"),
                        "source": f"AniList-{'动漫' if media_type == 'ANIME' else '漫画'}",
                        "type": "anime" if media_type == "ANIME" else "manga"
                    })
                log(f"  AniList {'动漫' if media_type == 'ANIME' else '漫画'} 页{page}: +{len(media_list)}")
                time.sleep(0.5)
            except Exception as e:
                log(f"  AniList 页{page} 失败: {e}")
                break
        return items

    def fetch_trending(self, media_type="ANIME", pages=2):
        items = []
        for page in range(1, pages + 1):
            try:
                query = """
                query ($page: Int, $type: MediaType) {
                    Page(page: $page, perPage: 50) {
                        media(type: $type, sort: TRENDING_DESC) {
                            title { romaji english native }
                            episodes
                            averageScore
                            startDate { year }
                        }
                    }
                }
                """
                variables = {"page": page, "type": media_type}
                resp = requests.post(self.API_URL, json={"query": query, "variables": variables}, timeout=15)
                data = resp.json()
                media_list = data.get("data", {}).get("Page", {}).get("media", [])
                for m in media_list:
                    title = m["title"]["english"] or m["title"]["romaji"] or m["title"]["native"]
                    year = m["startDate"]["year"] if m.get("startDate") else None
                    items.append({
                        "title": title,
                        "title_cn": m["title"].get("native", ""),
                        "year": year,
                        "rating": m.get("averageScore"),
                        "source": f"AniList-动漫趋势",
                        "type": "anime"
                    })
                log(f"  AniList 趋势 页{page}: +{len(media_list)}")
                time.sleep(0.5)
            except Exception as e:
                log(f"  AniList 趋势页{page} 失败: {e}")
                break
        return items


# ══════════════════════════════════════════════════════════════
#  TMDB API (已有)
# ══════════════════════════════════════════════════════════════
class TMDBCrawler:
    """TMDB - 趋势/热门/发现/相似推荐"""
    
    def fetch_trending(self, period="day", pages=3):
        """获取趋势榜"""
        items = []
        for page in range(1, pages + 1):
            try:
                data = self._api_call(f"trending/all/{period}?page={page}")
                if not data:
                    break
                for item in data.get("results", []):
                    items.append({
                        "title": item.get("title") or item.get("name", ""),
                        "title_cn": "",
                        "id": item.get("id"),
                        "type": item.get("media_type", ""),
                        "source": f"TMDB-趋势({period})"
                    })
                if page >= data.get("total_pages", 1):
                    break
                time.sleep(0.3)
            except Exception as e:
                log(f"  TMDB 趋势页{page} 失败: {e}")
                break
        return items
    
    def fetch_popular(self, media_type="movie", pages=3):
        """获取热门榜"""
        items = []
        for page in range(1, pages + 1):
            try:
                data = self._api_call(f"{media_type}/popular?page={page}")
                if not data:
                    break
                for item in data.get("results", []):
                    items.append({
                        "title": item.get("title") or item.get("name", ""),
                        "title_cn": "",
                        "id": item.get("id"),
                        "type": media_type,
                        "source": f"TMDB-热门{'电影' if media_type == 'movie' else '剧集'}"
                    })
                if page >= data.get("total_pages", 1):
                    break
                time.sleep(0.3)
            except Exception as e:
                log(f"  TMDB 热门页{page} 失败: {e}")
                break
        return items
    
    def _api_call(self, path):
        """TMDB API调用（绕过GFW）"""
        try:
            ip = socket.getaddrinfo("www.themoviedb.org", 443, socket.AF_INET)[0][4][0]
            full_path = f"/3/{path}&api_key={TMDB_API_KEY}&language=zh-CN" if "?" in path else f"/3/{path}?api_key={TMDB_API_KEY}&language=zh-CN"
            s = socket.create_connection((ip, 443), 10)
            ss = ssl.create_default_context().wrap_socket(s, server_hostname="api.themoviedb.org")
            ss.sendall(
                f"GET {full_path} HTTP/1.1\r\nHost: api.themoviedb.org\r\n"
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
                return None
            body = r[h + 4:]
            if b"chunked" in r[:h]:
                lines = body.split(b"\r\n")
                decoded = b""
                for line in lines:
                    if line and not all(c in b"0123456789abcdefABCDEF" for c in line[:8]):
                        decoded += line
                body = decoded
            return json.loads(body.decode())
        except Exception:
            return None


# ══════════════════════════════════════════════════════════════
#  搜索和数据库导入
# ══════════════════════════════════════════════════════════════
def search_api(kw):
    """搜索资源（带容错和重试）"""
    max_retries = 3
    for attempt in range(max_retries):
        try:
            resp = requests.post(SEARCH_API, json={"kw": kw, "limit": 30}, timeout=15)
            
            # 检查HTTP状态码
            if resp.status_code != 200:
                if attempt < max_retries - 1:
                    time.sleep(1)
                    continue
                log(f"  ✗ 搜[{kw}]: HTTP {resp.status_code}")
                return []
            
            # 检查响应内容
            content = resp.text.strip()
            if not content or content.startswith('<'):
                if attempt < max_retries - 1:
                    time.sleep(1)
                    continue
                return []
            
            # 解析JSON
            try:
                result = resp.json()
            except Exception as e:
                if attempt < max_retries - 1:
                    time.sleep(1)
                    continue
                return []
            
            merged = result.get("data", {}).get("merged_by_type", {})
            all_items = []
            for src, items in merged.items():
                all_items.extend(items)
            return all_items
            
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(1)
                continue
            log(f"  ✗ 搜[{kw}]: {e}")
            return []
    
    return []


def import_db(items, kw):
    """导入数据库"""
    if not items:
        return 0
    try:
        c = pymysql.connect(
            host=DB_HOST,
            port=DB_PORT,
            user=DB_USER,
            password=DB_PASSWORD,
            database=DB_NAME,
            charset="utf8mb4",
            connect_timeout=5,
        )
        cur = c.cursor()
        nw = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        existing = set()
        rows_to_insert = []
        batch_size = 500
        imp = 0
        for it in items:
            url = it.get("url") or ""
            if not url or url in existing:
                continue
            cur.execute("SELECT 1 FROM resources WHERE url=%s", (url,))
            if cur.fetchone():
                existing.add(url)
                continue
            title = it.get("note") or it.get("title") or kw
            pw = it.get("password") or ""
            src = (it.get("source") or it.get("type") or "other").lower()
            rows_to_insert.append((url, title, kw, pw, src, nw, nw))
            existing.add(url)
            if len(rows_to_insert) >= batch_size:
                cur.executemany(
                    "INSERT INTO resources(url,title,keyword,password,source,datetime,created_at) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s)",
                    rows_to_insert,
                )
                imp += cur.rowcount
                rows_to_insert.clear()
        if rows_to_insert:
            cur.executemany(
                "INSERT INTO resources(url,title,keyword,password,source,datetime,created_at) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s)",
                rows_to_insert,
            )
            imp += cur.rowcount
        c.commit()
        cur.close()
        c.close()
        return imp
    except Exception as e:
        log(f"  ✗ DB导入: {e}")
        return 0


# ══════════════════════════════════════════════════════════════
#  主程序
# ══════════════════════════════════════════════════════════════
def main():
    log("=" * 60)
    log("多源影视爬虫 v1.0 启动")
    log("=" * 60)
    
    all_titles = []
    seen = set()
    
    # ── 1. TVmaze ──
    log("\n📡 [1/4] TVmaze (美剧数据)")
    tvmaze = TVmazeCrawler()
    tvmaze_items = []
    tvmaze_items.extend(tvmaze.fetch_shows(pages=5))
    tvmaze_items.extend(tvmaze.fetch_schedule(country="US"))
    tvmaze_items.extend(tvmaze.fetch_schedule(country="CN"))
    
    for t in tvmaze_items:
        key = t["title"].lower()
        if key not in seen:
            seen.add(key)
            all_titles.append(t)
    log(f"  TVmaze 小计: {len(tvmaze_items)} → 去重后 {len(seen)}")
    
    # ── 2. Trakt.tv ──
    log("\n📡 [2/4] Trakt.tv (影迷热度)")
    trakt = TraktCrawler()
    if trakt.is_available():
        trakt_items = []
        trakt_items.extend(trakt.fetch_trending("shows", limit=50))
        trakt_items.extend(trakt.fetch_trending("movies", limit=50))
        trakt_items.extend(trakt.fetch_popular("shows", limit=50))
        trakt_items.extend(trakt.fetch_popular("movies", limit=50))
        trakt_items.extend(trakt.fetch_most_watched("shows", "weekly", limit=30))
        trakt_items.extend(trakt.fetch_most_watched("movies", "weekly", limit=30))
        
        for t in trakt_items:
            key = t["title"].lower()
            if key not in seen:
                seen.add(key)
                all_titles.append(t)
        log(f"  Trakt 小计: {len(trakt_items)} → 去重后 {len(seen)}")
    else:
        log("  ⚠️ Trakt 未配置 (设置 TRAKT_CLIENT_ID 环境变量)")
        log("  注册: https://trakt.tv/oauth/applications")
    
    # ── 3. 豆瓣 ──
    log("\n📡 [3/4] 豆瓣 (国内热门)")
    douban = DoubanCrawler()
    douban_items = []
    douban_items.extend(douban.fetch_hot("movie"))
    douban_items.extend(douban.fetch_hot("tv"))
    douban_items.extend(douban.fetch_weekly())
    
    for t in douban_items:
        key = t["title"].lower()
        if key not in seen:
            seen.add(key)
            all_titles.append(t)
    log(f"  豆瓣 小计: {len(douban_items)} → 去重后 {len(seen)}")
    
    # ── 4. AniList ──
    log("\n📡 [4/5] AniList (动漫)")
    anilist = AniListCrawler()
    anilist_items = []
    anilist_items.extend(anilist.fetch_popular("ANIME", pages=2))
    anilist_items.extend(anilist.fetch_trending("ANIME", pages=2))
    anilist_items.extend(anilist.fetch_popular("MANGA", pages=1))

    for t in anilist_items:
        key = t["title"].lower()
        if key not in seen:
            seen.add(key)
            all_titles.append(t)
    log(f"  AniList 小计: {len(anilist_items)} → 去重后 {len(seen)}")

    # ── 5. TMDB ──
    log("\n📡 [5/5] TMDB (趋势热门)")
    tmdb = TMDBCrawler()
    tmdb_items = []
    tmdb_items.extend(tmdb.fetch_trending("day", pages=3))
    tmdb_items.extend(tmdb.fetch_trending("week", pages=3))
    tmdb_items.extend(tmdb.fetch_popular("movie", pages=3))
    tmdb_items.extend(tmdb.fetch_popular("tv", pages=3))
    
    for t in tmdb_items:
        key = t["title"].lower()
        if key not in seen:
            seen.add(key)
            all_titles.append(t)
    log(f"  TMDB 小计: {len(tmdb_items)} → 去重后 {len(seen)}")
    
    # ── 搜索并导入 ──
    log(f"\n{'=' * 60}")
    log(f"📊 汇总: {len(all_titles)} 个作品，开始搜索资源")
    log(f"{'=' * 60}\n")
    
    imp = 0
    for i, t in enumerate(all_titles, 1):
        kw = t["title"]
        if i % 50 == 0 or i == len(all_titles):
            log(f"进度: {i}/{len(all_titles)}, 已导入: {imp}")
        
        its = search_api(kw)
        if its:
            n = import_db(its, kw)
            imp += n
        time.sleep(0.2)
    
    log(f"\n{'=' * 60}")
    log(f"🎉 完成!")
    log(f"  作品总数: {len(all_titles)}")
    log(f"  新增资源: {imp}")
    log(f"{'=' * 60}")


if __name__ == "__main__":
    main()
