# -*- coding: utf-8 -*-
"""通用工具：路径、时间、JSON 读写、HTTP、重试。"""
import json
import os
import random
import socket
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(ROOT, "config")
DATA_DIR = os.path.join(ROOT, "data")
CACHE_DIR = os.path.join(DATA_DIR, "cache")

TZ = ZoneInfo("Asia/Shanghai")
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# 避免 yfinance 底层请求无限挂起
socket.setdefaulttimeout(45)


def bj_now() -> datetime:
    return datetime.now(TZ)


def bj_now_str(fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    return bj_now().strftime(fmt)


def bj_date_str() -> str:
    return bj_now().strftime("%Y-%m-%d")


def load_json(path: str, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def save_json(path: str, obj) -> bool:
    """原子化写 JSON，返回是否成功。"""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def http_get(url: str, timeout: int = 20, retries: int = 2, headers=None):
    """带重试与退避的 GET，返回 text，失败返回 None。"""
    h = {"User-Agent": UA}
    if headers:
        h.update(headers)
    last = None
    for i in range(retries + 1):
        try:
            r = requests.get(url, headers=h, timeout=timeout)
            if r.status_code == 200:
                return r.text
            last = f"HTTP {r.status_code}"
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
        if i < retries:
            time.sleep(1.5 * (i + 1) + random.random())
    print(f"[http] GET 失败 {url} -> {last}")
    return None


def http_post_json(url: str, payload: dict, headers=None, timeout: int = 60):
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    try:
        r = requests.post(url, json=payload, headers=h, timeout=timeout)
        return r.status_code, r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    except Exception as e:  # noqa: BLE001
        return None, {"error": f"{type(e).__name__}: {e}"}


def timer_ms() -> float:
    return time.time()


def elapsed_sec(t0: float) -> float:
    return round(time.time() - t0, 1)


def now_utc_str() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def minutes_ago(iso_str: str | None) -> float | None:
    """距现在多少分钟（按北京时间解析 YYYY-MM-DD HH:MM[:SS]）。"""
    if not iso_str:
        return None
    try:
        t = datetime.strptime(iso_str[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)
        return (bj_now() - t).total_seconds() / 60.0
    except ValueError:
        return None


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def stdio(msg: str):
    print(msg, flush=True)
    sys.stdout.flush()
