#!/usr/bin/env python3
"""
OMDb 评分爬虫
获取电影/剧集的 IMDb、烂番茄、Metacritic 评分
"""
import os
import sys
import json
import time
import requests
import pymysql
from datetime import datetime

# ── 配置（优先环境变量，无默认值强制设置） ──
OMDB_API_KEY = os.environ.get("OMDB_API_KEY")
if not OMDB_API_KEY:
    raise RuntimeError("环境变量 OMDB_API_KEY 未设置")
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


def log(m):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def search_omdb(title, year=None):
    """搜索OMDb获取评分"""
    try:
        params = {"t": title, "apikey": OMDB_API_KEY}
        if year:
            params["y"] = year
        resp = requests.get("http://www.omdbapi.com/", params=params, timeout=10)
        data = resp.json()
        if data.get("Response") == "True":
            ratings = {}
            for r in data.get("Ratings", []):
                source = r["Source"]
                value = r["Value"]
                if source == "Internet Movie Database":
                    ratings["imdb"] = value
                elif source == "Rotten Tomatoes":
                    ratings["rotten_tomatoes"] = value
                elif source == "Metacritic":
                    ratings["metacritic"] = value
            return {
                "title": data.get("Title"),
                "year": data.get("Year"),
                "imdb_rating": data.get("imdbRating"),
                "ratings": ratings,
                "poster": data.get("Poster"),
                "plot": data.get("Plot"),
                "genre": data.get("Genre"),
                "director": data.get("Director"),
                "actors": data.get("Actors"),
            }
    except Exception as e:
        log(f"  ✗ OMDb搜索[{title}]: {e}")
    return None


def get_db_titles(limit=100):
    """从数据库获取需要查询评分的标题"""
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
        # 获取没有评分数据的资源标题
        cur.execute("""
            SELECT keyword FROM (
                SELECT keyword, MAX(created_at) as latest
                FROM resources 
                WHERE keyword IS NOT NULL AND keyword != ''
                GROUP BY keyword
                ORDER BY latest DESC
                LIMIT %s
            ) t
        """, (limit,))
        titles = [row[0] for row in cur.fetchall()]
        cur.close()
        c.close()
        return titles
    except Exception as e:
        log(f"  ✗ 数据库查询: {e}")
        return []


def save_ratings(title, ratings_data):
    """保存评分数据到数据库"""
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
        
        # 创建评分表（如果不存在）
        cur.execute("""
            CREATE TABLE IF NOT EXISTS movie_ratings (
                id INT AUTO_INCREMENT PRIMARY KEY,
                title VARCHAR(500) NOT NULL,
                year VARCHAR(10),
                imdb_rating VARCHAR(20),
                rotten_tomatoes VARCHAR(20),
                metacritic VARCHAR(20),
                genre VARCHAR(500),
                director VARCHAR(500),
                actors TEXT,
                plot TEXT,
                poster VARCHAR(1000),
                updated_at DATETIME,
                UNIQUE KEY unique_title (title, year)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)
        
        # 插入或更新评分
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cur.execute("""
            INSERT INTO movie_ratings 
            (title, year, imdb_rating, rotten_tomatoes, metacritic, genre, director, actors, plot, poster, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
            imdb_rating = VALUES(imdb_rating),
            rotten_tomatoes = VALUES(rotten_tomatoes),
            metacritic = VALUES(metacritic),
            updated_at = VALUES(updated_at)
        """, (
            ratings_data.get("title", title),
            ratings_data.get("year"),
            ratings_data.get("imdb_rating"),
            ratings_data.get("ratings", {}).get("rotten_tomatoes"),
            ratings_data.get("ratings", {}).get("metacritic"),
            ratings_data.get("genre"),
            ratings_data.get("director"),
            ratings_data.get("actors"),
            ratings_data.get("plot"),
            ratings_data.get("poster"),
            now
        ))
        c.commit()
        cur.close()
        c.close()
        return True
    except Exception as e:
        log(f"  ✗ 保存评分: {e}")
        return False


def main():
    log("=" * 60)
    log("OMDb 评分爬虫启动")
    log(f"API Key: {OMDB_API_KEY[:8]}...")
    log("=" * 60)
    
    # 获取数据库中的标题
    log("\n📡 获取数据库中的标题...")
    titles = get_db_titles(limit=50)
    log(f"  获取到 {len(titles)} 个标题")
    
    if not titles:
        log("没有需要查询的标题")
        return
    
    # 查询评分
    log(f"\n📡 查询 OMDb 评分...")
    success = 0
    failed = 0
    
    for i, title in enumerate(titles, 1):
        if i % 10 == 0:
            log(f"  进度: {i}/{len(titles)}")
        
        ratings = search_omdb(title)
        if ratings:
            if save_ratings(title, ratings):
                success += 1
                if i <= 5:  # 显示前5个结果
                    log(f"  ✓ {title}")
                    log(f"    IMDb: {ratings.get('imdb_rating')}, RT: {ratings.get('ratings', {}).get('rotten_tomatoes')}")
        else:
            failed += 1
        
        # OMDb限制: 1000次/天
        time.sleep(0.5)
    
    log(f"\n{'=' * 60}")
    log(f"🎉 完成!")
    log(f"  成功: {success}")
    log(f"  失败: {failed}")
    log(f"{'=' * 60}")


if __name__ == "__main__":
    main()
