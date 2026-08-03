# -*- coding: utf-8 -*-
"""
Vercel 서버리스 함수 버전: /api/news

server.py 와 동일한 스크래핑/분류 로직을 Vercel의 Python 런타임
(BaseHTTPRequestHandler 기반) 형식에 맞게 옮긴 것입니다.
"""

import json
import re
import time
import threading
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler
from html import unescape
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone

MAX_ARTICLES = 8
MAX_AGE_HOURS = 48
CACHE_TTL = 300
REQUEST_TIMEOUT = 8   # 서버리스 함수 실행시간 제한 때문에 여유있게 짧게

CRITICAL_KEYWORDS = [
    "earthquake", "지진", "magnitude", "tsunami", "쓰나미", "지진해일", "해일",
    "typhoon", "태풍", "hurricane", "허리케인", "cyclone",
    "flooding", "floods", "홍수", "침수", "wildfire", "wildfires", "산불",
    "volcano", "화산", "eruption", "landslide", "산사태", "mudslide",
    "power outage", "blackout", "정전", "grid failure", "grid collapse",
    "explosion", "explosions", "폭발", "building collapse", "bridge collapse",
    "붕괴", "derailment", "pipeline rupture", "송유관",
    "data center outage", "데이터센터 화재", "submarine cable cut",
    "airstrike", "air strike", "공습", "missile strike", "invasion", "침공",
    "terror attack", "테러", "coup", "쿠데타", "sabotage",
    "evacuation ordered", "evacuate", "대피령", "대피",
    "state of emergency", "비상사태", "death toll", "meltdown",
]

WARNING_KEYWORDS = [
    "storm", "폭풍", "heavy rain", "폭우", "heatwave", "heat wave", "폭염",
    "drought", "가뭄", "flood warning", "홍수 주의", "storm warning",
    "protest", "시위", "strike", "파업", "unrest", "riot", "폭동",
    "war", "전쟁", "missile", "미사일", "sanction", "제재",
    "tension", "긴장", "military", "border clash",
    "cyberattack", "사이버공격", "ransomware", "랜섬웨어", "data breach",
    "유출", "outage", "장애", "disruption", "차질", "shortage", "부족",
]


def _compile(keywords):
    pats = []
    for kw in keywords:
        if re.search(r"[a-zA-Z]", kw) and not re.search(r"[가-힣]", kw):
            pats.append((kw, re.compile(r"\b" + re.escape(kw) + r"\b", re.I)))
        else:
            pats.append((kw, re.compile(re.escape(kw))))
    return pats


_CRIT_PATS = _compile(CRITICAL_KEYWORDS)
_WARN_PATS = _compile(WARNING_KEYWORDS)

# 과거 사건 / 회고 / 창작물 맥락 신호 -> 위험 키워드가 있어도 경고 강등
HISTORICAL_KEYWORDS = [
    "anniversary", "주년", "기념", "추모", "추도", "memorial", "commemorat",
    "remembrance", "회고", "회상", "on this day", "years ago", "decades ago",
    "veteran", "veterans", "참전", "documentary", "다큐멘터리", "다큐",
    "archive", "archival", "history of", "armistice", "정전협정", "종전",
    "film", "movie", "영화", "drama", "드라마", "novel", "소설",
    "webtoon", "웹툰", "trailer", "예고편", "box office", "박스오피스",
    "actor", "actress", "배우", "album", "앨범",
]
_HIST_PATS = _compile(HISTORICAL_KEYWORDS)
_YEAR_RE = re.compile(r"\b(19\d{2})\b")


def looks_historical(text):
    if _YEAR_RE.search(text):
        return True
    for _kw, pat in _HIST_PATS:
        if pat.search(text):
            return True
    return False


COUNTRY_LOCALE = {
    "South Korea": ("ko", "KR", "KR:ko"),
    "Japan":       ("ja", "JP", "JP:ja"),
    "Germany":     ("de", "DE", "DE:de"),
    "France":      ("fr", "FR", "FR:fr"),
    "Taiwan":      ("zh-TW", "TW", "TW:zh-Hant"),
    "Brazil":      ("pt-BR", "BR", "BR:pt-419"),
    "Mexico":      ("es-419", "MX", "MX:es-419"),
    "Spain":       ("es", "ES", "ES:es"),
    "Italy":       ("it", "IT", "IT:it"),
    "India":       ("en-IN", "IN", "IN:en"),
}
DEFAULT_LOCALE = ("en-US", "US", "US:en")

QUERY_TERMS = (
    'earthquake OR flood OR typhoon OR hurricane OR wildfire OR "power outage" '
    'OR blackout OR explosion OR war OR missile OR cyberattack OR "data center" '
    'OR infrastructure OR strike OR protest OR "state of emergency"'
)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# 같은 서버리스 인스턴스가 재사용(warm)되는 동안만 유효한 캐시
_cache = {}
_cache_lock = threading.Lock()


def _clean(text):
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def parse_age_hours(pub_str):
    if not pub_str:
        return None
    try:
        dt = parsedate_to_datetime(pub_str)
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - dt
        return delta.total_seconds() / 3600.0
    except (TypeError, ValueError):
        return None


def classify(text):
    # 위험 키워드가 걸려도 과거/회고/창작물 맥락이면 normal 로 강등
    for kw, pat in _CRIT_PATS:
        if pat.search(text):
            return ("normal", None) if looks_historical(text) else ("critical", kw)
    for kw, pat in _WARN_PATS:
        if pat.search(text):
            return ("normal", None) if looks_historical(text) else ("warning", kw)
    return "normal", None


def build_rss_url(country):
    hl, gl, ceid = COUNTRY_LOCALE.get(country, DEFAULT_LOCALE)
    query = f'{country} ({QUERY_TERMS}) when:2d'
    params = urllib.parse.urlencode({"q": query, "hl": hl, "gl": gl, "ceid": ceid})
    return f"https://news.google.com/rss/search?{params}"


def fetch_country(country):
    url = build_rss_url(country)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    articles = []
    level_rank = {"normal": 0, "warning": 1, "critical": 2}
    worst = "normal"
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            raw = resp.read()
        root = ET.fromstring(raw)
        for item in root.iter("item"):
            title = _clean(item.findtext("title"))
            desc = _clean(item.findtext("description"))
            link = (item.findtext("link") or "").strip()
            pub = (item.findtext("pubDate") or "").strip()
            source_el = item.find("source")
            source = _clean(source_el.text) if source_el is not None else ""
            if not title:
                continue
            age = parse_age_hours(pub)
            if age is None or age > MAX_AGE_HOURS:
                continue
            sev, kw = classify(title + " " + desc)
            if level_rank[sev] > level_rank[worst]:
                worst = sev
            articles.append({
                "title": title, "link": link, "pubDate": pub, "source": source,
                "severity": sev, "matched": kw or "", "age_hours": round(age, 1),
            })
    except Exception as e:  # noqa: BLE001
        return {"country": country, "level": "unknown", "error": str(e),
                "articles": [], "count": 0, "updated": int(time.time())}

    order = {"critical": 0, "warning": 1, "normal": 2}
    articles.sort(key=lambda a: (order[a["severity"]], a.get("age_hours", 9e9)))
    return {
        "country": country,
        "level": worst,
        "count": len(articles),
        "critical_count": sum(1 for a in articles if a["severity"] == "critical"),
        "warning_count": sum(1 for a in articles if a["severity"] == "warning"),
        "articles": articles[:MAX_ARTICLES],
        "updated": int(time.time()),
    }


def get_country_cached(country):
    now = time.time()
    with _cache_lock:
        hit = _cache.get(country)
        if hit and now - hit[0] < CACHE_TTL:
            return hit[1]
    data = fetch_country(country)
    with _cache_lock:
        _cache[country] = (now, data)
    return data


class handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        countries = qs.get("countries", [""])[0]
        names = [c.strip() for c in countries.split(",") if c.strip()]

        results = {}
        lock = threading.Lock()

        def work(name):
            data = get_country_cached(name)
            with lock:
                results[name] = data

        threads = []
        for n in names[:15]:  # 서버리스 실행시간 한도 보호
            t = threading.Thread(target=work, args=(n,))
            t.start()
            threads.append(t)
        for t in threads:
            t.join()

        body = json.dumps(results, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
