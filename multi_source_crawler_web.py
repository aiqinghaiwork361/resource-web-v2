#!/usr/bin/env python3
"""
多源影视爬虫 v1.0 - Web集成版
支持：TVmaze / Trakt.tv / 豆瓣 / TMDB
独立进程运行，通过 JSON 文件与 Flask 应用通信
"""
import os
import sys
import json
import time
import socket
import ssl
import re
from datetime import datetime

import requests
import pymysql

STATUS_FILE = "/app/data/multi_source_status.json"
SEARCH_API = os.environ.get("SEARCH_API", "https://ps.aiqinghaiwork.cn/api/search")
DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "resource_db"),
    "port": int(os.environ.get("DB_PORT", "3306")),
    "user": os.environ.get("DB_USER", "panuser"),
    "password": os.environ.get("DB_PASSWORD"),
    "database": os.environ.get("DB_NAME", "pan_resource"),
    "charset": "utf8mb4",
}
if not DB_CONFIG["password"]:
    raise RuntimeError("环境变量 DB_PASSWORD 未设置")

# Trakt.tv API Key (免费注册: https://trakt.tv/oauth/applications)
TRAKT_CLIENT_ID = os.environ.get("TRAKT_CLIENT_ID")
if not TRAKT_CLIENT_ID:
    raise RuntimeError("环境变量 TRAKT_CLIENT_ID 未设置")

# TMDB API Key
TMDB_API_KEY = os.environ.get("TMDB_API_KEY")
if not TMDB_API_KEY:
    raise RuntimeError("环境变量 TMDB_API_KEY 未设置")

status = {
    "running": True,
    "phase": "",
    "source": "",
    "progress": 0,
    "total": 0,
    "imported": 0,
    "log": []
}


def save_status():
    os.makedirs(os.path.dirname(STATUS_FILE), exist_ok=True)
    with open(STATUS_FILE, "w") as f:
        json.dump(status, f, ensure_ascii=False)


def log(m):
    ts = datetime.now().strftime("%H:%M:%S")
    status["log"].append(f"[{ts}] {m}")
    if len(status["log"]) > 100:
        status["log"] = status["log"][-100:]
    save_status()


# ══════════════════════════════════════════════════════════════
#  TVmaze API
# ══════════════════════════════════════════════════════════════
class TVmazeCrawler:
    BASE_URL = "https://api.tvmaze.com"
    
    def fetch_shows(self, pages=5):
        shows = []
        for page in range(pages):
            try:
                resp = requests.get(f"{self.BASE_URL}/shows?page={page}", timeout=15)
                data = resp.json()
                for item in data:
                    shows.append({
                        "title": item.get("name", ""),
                        "year": (item.get("premiered") or "")[:4],
                        "rating": item.get("rating", {}).get("average"),
                        "source": "TVmaze-热门",
                        "type": "tv"
                    })
                log(f"  TVmaze 页{page+1}: +{len(data)}")
                time.sleep(0.5)
            except Exception as e:
                log(f"  TVmaze 页{page+1} 失败: {e}")
                break
        return shows
    
    def fetch_schedule(self, country="US"):
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
                        "year": (show.get("premiered") or "")[:4],
                        "rating": show.get("rating", {}).get("average"),
                        "source": f"TVmaze-今日播出({country})",
                        "type": "tv"
                    })
            log(f"  TVmaze 今日播出({country}): {len(shows)} 个")
        except Exception as e:
            log(f"  TVmaze 播出表失败: {e}")
        return shows


# ══════════════════════════════════════════════════════════════
#  Trakt.tv API
# ══════════════════════════════════════════════════════════════
class TraktCrawler:
    BASE_URL = "https://api.trakt.tv"
    
    def _headers(self):
        return {
            "Content-Type": "application/json",
            "trakt-api-version": "2",
            "trakt-api-key": TRAKT_CLIENT_ID
        }
    
    def is_available(self):
        return bool(TRAKT_CLIENT_ID)
    
    def fetch_trending(self, media_type="shows", limit=50):
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
                items.append({
                    "title": show.get("title", ""),
                    "year": show.get("year"),
                    "watchers": item.get("watchers"),
                    "source": f"Trakt-热播{'剧' if media_type == 'shows' else '电影'}",
                    "type": "tv" if media_type == "shows" else "movie"
                })
            log(f"  Trakt 热播{'剧' if media_type == 'shows' else '电影'}: {len(items)} 个")
        except Exception as e:
            log(f"  Trakt 热播失败: {e}")
        return items


# ══════════════════════════════════════════════════════════════
#  豆瓣 API
# ══════════════════════════════════════════════════════════════
class DoubanCrawler:
    BASE_URL = "https://movie.douban.com"
    
    def _headers(self):
        return {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://movie.douban.com/"
        }
    
    def fetch_hot(self, media_type="movie"):
        items = []
        try:
            resp = requests.get(
                f"{self.BASE_URL}/j/search_subjects?type={media_type}&tag=热门&page_limit=50&page_start=0",
                headers=self._headers(),
                timeout=15
            )
            data = resp.json()
            for item in data.get("subjects", []):
                items.append({
                    "title": item.get("title", ""),
                    "rating": item.get("rate"),
                    "source": f"豆瓣-热门{'电影' if media_type == 'movie' else '剧集'}",
                    "type": media_type
                })
            log(f"  豆瓣 热门{'电影' if media_type == 'movie' else '剧集'}: {len(items)} 个")
        except Exception as e:
            log(f"  豆瓣 热门失败: {e}")
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
                        "source": "AniList-动漫趋势",
                        "type": "anime"
                    })
                log(f"  AniList 趋势 页{page}: +{len(media_list)}")
                time.sleep(0.5)
            except Exception as e:
                log(f"  AniList 趋势页{page} 失败: {e}")
                break
        return items


# ══════════════════════════════════════════════════════════════
#  搜索和数据库导入
# ══════════════════════════════════════════════════════════════
def search_api(kw):
    try:
        resp = requests.post(SEARCH_API, json={"kw": kw, "limit": 30}, timeout=15)
        result = resp.json()
        merged = result.get("data", {}).get("merged_by_type", {})
        all_items = []
        for src, items in merged.items():
            all_items.extend(items)
        return all_items
    except Exception as e:
        log(f"  ✗ 搜[{kw}]: {e}")
        return []


def import_db(items, kw):
    if not items:
        return 0
    try:
        c = pymysql.connect(**DB_CONFIG)
        cur = c.cursor()
        nw = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        imp = 0
        for it in items:
            url = it.get("url") or ""
            if not url:
                continue
            title = it.get("note") or it.get("title") or kw
            pw = it.get("password") or ""
            src = (it.get("source") or it.get("type") or "other").lower()
            cur.execute("SELECT 1 FROM resources WHERE url=%s", (url,))
            if cur.fetchone():
                continue
            cur.execute(
                "INSERT INTO resources(url,title,keyword,password,source,datetime,created_at) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s)",
                (url, title, kw, pw, src, nw, nw),
            )
            imp += 1
        c.commit()
        cur.close()
        c.close()
        return imp
    except Exception as e:
        log(f"  ✗ DB导入: {e}")
        return 0


def main():
    try:
        status["phase"] = "收集标题"
        log("=" * 60)
        log("多源影视爬虫 v1.0 启动")
        log("=" * 60)
        
        all_titles = []
        seen = set()
        
        # ── 1. TVmaze ──
        status["source"] = "TVmaze"
        log("\n📡 [1/4] TVmaze (美剧数据)")
        tvmaze = TVmazeCrawler()
        tvmaze_items = []
        tvmaze_items.extend(tvmaze.fetch_shows(pages=3))
        tvmaze_items.extend(tvmaze.fetch_schedule(country="US"))
        tvmaze_items.extend(tvmaze.fetch_schedule(country="CN"))
        
        for t in tvmaze_items:
            key = t["title"].lower()
            if key not in seen:
                seen.add(key)
                all_titles.append(t)
        log(f"  TVmaze 小计: {len(tvmaze_items)} → 去重后 {len(seen)}")
        
        # ── 2. Trakt.tv ──
        status["source"] = "Trakt.tv"
        log("\n📡 [2/4] Trakt.tv (影迷热度)")
        trakt = TraktCrawler()
        if trakt.is_available():
            trakt_items = []
            trakt_items.extend(trakt.fetch_trending("shows", limit=30))
            trakt_items.extend(trakt.fetch_trending("movies", limit=30))
            
            for t in trakt_items:
                key = t["title"].lower()
                if key not in seen:
                    seen.add(key)
                    all_titles.append(t)
            log(f"  Trakt 小计: {len(trakt_items)} → 去重后 {len(seen)}")
        else:
            log("  ⚠️ Trakt 未配置 (设置 TRAKT_CLIENT_ID 环境变量)")
        
        # ── 3. 豆瓣 ──
        status["source"] = "豆瓣"
        log("\n📡 [3/4] 豆瓣 (国内热门)")
        douban = DoubanCrawler()
        douban_items = []
        douban_items.extend(douban.fetch_hot("movie"))
        douban_items.extend(douban.fetch_hot("tv"))
        
        for t in douban_items:
            key = t["title"].lower()
            if key not in seen:
                seen.add(key)
                all_titles.append(t)
        log(f"  豆瓣 小计: {len(douban_items)} → 去重后 {len(seen)}")
        
        # ── 4. AniList ──
        status["source"] = "AniList"
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
        status["source"] = "TMDB"
        log("\n📡 [5/5] TMDB (趋势热门)")
        # 简化版TMDB，只抓1页
        try:
            ip = socket.getaddrinfo("www.themoviedb.org", 443, socket.AF_INET)[0][4][0]
            for ep, label in [("trending/all/day", "TMDB-趋势日"), ("trending/all/week", "TMDB-趋势周")]:
                path = f"/3/{ep}?api_key={TMDB_API_KEY}&language=zh-CN&page=1"
                s = socket.create_connection((ip, 443), 10)
                ss = ssl.create_default_context().wrap_socket(s, server_hostname="api.themoviedb.org")
                ss.sendall(f"GET {path} HTTP/1.1\r\nHost: api.themoviedb.org\r\nAccept: application/json\r\nConnection: close\r\n\r\n".encode())
                r = b""
                while True:
                    c = ss.recv(65536)
                    if not c:
                        break
                    r += c
                ss.close()
                h = r.find(b"\r\n\r\n")
                if h >= 0:
                    body = r[h+4:]
                    if b"chunked" in r[:h]:
                        lines = body.split(b"\r\n")
                        decoded = b""
                        for line in lines:
                            if line and not all(c in b"0123456789abcdefABCDEF" for c in line[:8]):
                                decoded += line
                        body = decoded
                    data = json.loads(body.decode())
                    for item in data.get("results", []):
                        title = item.get("title") or item.get("name", "")
                        if title:
                            all_titles.append({"title": title.strip(), "source": label, "type": item.get("media_type", "")})
                time.sleep(0.3)
        except Exception as e:
            log(f"  TMDB 失败: {e}")
        
        log(f"\n📊 汇总: {len(all_titles)} 个作品")
        
        # ── 搜索并导入 ──
        status["phase"] = "搜索导入"
        status["total"] = len(all_titles)
        log("开始搜索资源...")
        
        imported = 0
        for i, t in enumerate(all_titles, 1):
            status["progress"] = i
            kw = t["title"]
            
            if i % 50 == 0 or i == len(all_titles):
                log(f"  进度: {i}/{len(all_titles)}, 已导入: {imported}")
            
            items = search_api(kw)
            if items:
                n = import_db(items, kw)
                imported += n
            status["imported"] = imported
            save_status()
            time.sleep(0.2)
        
        log(f"\n🎉 完成! {len(all_titles)} 作品, {imported} 新资源")
        
    except Exception as e:
        log(f"\nError: {e}")
    finally:
        status["running"] = False
        status["phase"] = "完成"
        save_status()


if __name__ == "__main__":
    main()
