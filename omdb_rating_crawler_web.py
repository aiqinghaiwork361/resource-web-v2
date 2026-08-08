#!/usr/bin/env python3
"""
OMDb 评分爬虫 - Web集成版
独立进程运行，通过 JSON 文件与 Flask 应用通信
"""
import os
import sys
import json
import time
import requests
import pymysql
from datetime import datetime

STATUS_FILE = "/app/data/omdb_status.json"
OMDB_API_KEY = os.environ.get("OMDB_API_KEY")
if not OMDB_API_KEY:
    raise RuntimeError("环境变量 OMDB_API_KEY 未设置")
SEARCH_API = os.environ.get("SEARCH_API")
if not SEARCH_API:
    raise RuntimeError("环境变量 SEARCH_API 未设置")
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

status = {
    "running": True,
    "progress": 0,
    "total": 0,
    "success": 0,
    "failed": 0,
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
        c = pymysql.connect(**DB_CONFIG)
        cur = c.cursor()
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
        c = pymysql.connect(**DB_CONFIG)
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
    try:
        log("=" * 60)
        log("OMDb 评分爬虫启动")
        log("=" * 60)
        
        # 获取数据库中的标题
        log("\n📡 获取数据库中的标题...")
        titles = get_db_titles(limit=50)
        status["total"] = len(titles)
        log(f"  获取到 {len(titles)} 个标题")
        
        if not titles:
            log("没有需要查询的标题")
            return
        
        # 查询评分
        log(f"\n📡 查询 OMDb 评分...")
        
        for i, title in enumerate(titles, 1):
            status["progress"] = i
            if i % 10 == 0:
                log(f"  进度: {i}/{len(titles)}")
            
            ratings = search_omdb(title)
            if ratings:
                if save_ratings(title, ratings):
                    status["success"] += 1
            else:
                status["failed"] += 1
            
            save_status()
            time.sleep(0.5)
        
        log(f"\n🎉 完成! 成功: {status['success']}, 失败: {status['failed']}")
        
    except Exception as e:
        log(f"\nError: {e}")
    finally:
        status["running"] = False
        save_status()


if __name__ == "__main__":
    main()
