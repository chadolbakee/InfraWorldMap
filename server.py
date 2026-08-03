# -*- coding: utf-8 -*-
"""
인프라 자산 뉴스 모니터링 - 백엔드 (표준 라이브러리만 사용)

Google News RSS 를 스크래핑해서 국가별로
인프라에 영향을 줄 수 있는 뉴스를 수집하고 심각도를 분류합니다.

실행:  python server.py
브라우저:  http://localhost:8000
"""

import json
import re
import time
import threading
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from html import unescape
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
import os

PORT = 8000
CACHE_TTL = 300           # 5분 캐시
MAX_ARTICLES = 8          # 국가별로 프론트에 넘길 최대 기사 수
MAX_AGE_HOURS = 48        # 이 시간보다 오래된 기사는 버림 (경고등은 최근 뉴스로만 결정)

# ---------------------------------------------------------------------------
# 심각도 키워드 사전 (제목/요약을 소문자로 검사)
#  - critical : 지금 실제로 벌어지는 급성 재난/인프라 타격 (빨간 경고등)
#  - warning  : 주의가 필요한 잠재적 위험 (노란불)
#
#  ⚠ 영어 키워드는 단어 경계(\b)로만 매칭한다.
#    (안 그러면 "war"가 "software"/"warning" 안에서 오탐)
#    한글은 단어 경계 개념이 없으므로 부분 문자열로 매칭.
# ---------------------------------------------------------------------------
CRITICAL_KEYWORDS = [
    # 자연재해 (급성)
    "earthquake", "지진", "magnitude", "tsunami", "쓰나미", "지진해일", "해일",
    "typhoon", "태풍", "hurricane", "허리케인", "cyclone",
    "flooding", "floods", "홍수", "침수", "wildfire", "wildfires", "산불",
    "volcano", "화산", "eruption", "landslide", "산사태", "mudslide",
    # 인프라 직접 타격
    "power outage", "blackout", "정전", "grid failure", "grid collapse",
    "explosion", "explosions", "폭발", "building collapse", "bridge collapse",
    "붕괴", "derailment", "pipeline rupture", "송유관",
    "data center outage", "데이터센터 화재", "submarine cable cut",
    # 안전/보안 (급성)
    "airstrike", "air strike", "공습", "missile strike", "invasion", "침공",
    "terror attack", "테러", "coup", "쿠데타", "sabotage",
    "evacuation ordered", "evacuate", "대피령", "대피",
    "state of emergency", "비상사태", "death toll", "meltdown",
]

WARNING_KEYWORDS = [
    # 기상/환경
    "storm", "폭풍", "heavy rain", "폭우", "heatwave", "heat wave", "폭염",
    "drought", "가뭄", "flood warning", "홍수 주의", "storm warning",
    # 사회/노동
    "protest", "시위", "strike", "파업", "unrest", "riot", "폭동",
    # 지정학 (상시 배경 소음이라 주의 단계로)
    "war", "전쟁", "missile", "미사일", "sanction", "제재",
    "tension", "긴장", "military", "border clash",
    # 보안/운영
    "cyberattack", "사이버공격", "ransomware", "랜섬웨어", "data breach",
    "유출", "outage", "장애", "disruption", "차질", "shortage", "부족",
]

# 단어 경계까지 반영한 정규식 패턴을 미리 컴파일
def _compile(keywords):
    pats = []
    for kw in keywords:
        if re.search(r"[a-zA-Z]", kw) and not re.search(r"[가-힣]", kw):
            # 영어(공백 포함 구문): 단어 경계로
            pats.append((kw, re.compile(r"\b" + re.escape(kw) + r"\b", re.I)))
        else:
            # 한글 등: 부분 문자열
            pats.append((kw, re.compile(re.escape(kw))))
    return pats

_CRIT_PATS = _compile(CRITICAL_KEYWORDS)
_WARN_PATS = _compile(WARNING_KEYWORDS)

# ---------------------------------------------------------------------------
# 과거 사건 / 회고 / 창작물 맥락 신호
#  위험 키워드(war, explosion 등)가 들어 있어도, 아래 맥락이면 '지금 벌어지는
#  사건'이 아니라 과거 회고·기념·다큐·영화 등이므로 경고를 강등(normal)한다.
#  예) "Vietnam War 50th anniversary", "Documentary on 1975 evacuation"
# ---------------------------------------------------------------------------
HISTORICAL_KEYWORDS = [
    # 회고 / 기념 / 종전
    "anniversary", "주년", "기념", "추모", "추도", "memorial", "commemorat",
    "remembrance", "회고", "회상", "on this day", "years ago", "decades ago",
    "veteran", "veterans", "참전", "documentary", "다큐멘터리", "다큐",
    "archive", "archival", "history of", "armistice", "정전협정", "종전",
    # 창작물 / 엔터
    "film", "movie", "영화", "drama", "드라마", "novel", "소설",
    "webtoon", "웹툰", "trailer", "예고편", "box office", "박스오피스",
    "actor", "actress", "배우", "album", "앨범",
]
_HIST_PATS = _compile(HISTORICAL_KEYWORDS)
_YEAR_RE = re.compile(r"\b(19\d{2})\b")   # 1900~1999년 언급 = 과거 사건 강력 신호


def looks_historical(text):
    """위험 키워드가 있어도 과거/회고/창작물 맥락이면 True -> 경고 강등."""
    if _YEAR_RE.search(text):
        return True
    for _kw, pat in _HIST_PATS:
        if pat.search(text):
            return True
    return False


# ---------------------------------------------------------------------------
# 지명 필터
#  검색어(국가명)가 기사 '내용'이 아니라 '언론사 이름'(예: Yahoo News Singapore)
#  에만 걸려서, 엉뚱한 지역 뉴스가 딸려오는 문제를 막는다.
#  -> 제목/요약 본문에 그 나라(또는 형용사/수도/주요도시)가 실제로 언급된
#     기사만 남긴다. (언론사 이름은 검사 대상에서 제외)
# ---------------------------------------------------------------------------
LOCATION_ALIASES = {
    "South Korea": ["south korea", "korea", "korean", "seoul", "한국", "대한민국", "서울"],
    "Japan": ["japan", "japanese", "tokyo", "osaka", "일본", "도쿄"],
    "China": ["china", "chinese", "beijing", "shanghai", "중국", "베이징", "상하이"],
    "Taiwan": ["taiwan", "taiwanese", "taipei", "대만", "타이완", "타이베이"],
    "Hong Kong": ["hong kong", "hongkong", "홍콩"],
    "Singapore": ["singapore", "singaporean", "싱가포르"],
    "India": ["india", "indian", "delhi", "mumbai", "인도"],
    "Indonesia": ["indonesia", "indonesian", "jakarta", "인도네시아", "자카르타"],
    "Vietnam": ["vietnam", "vietnamese", "hanoi", "베트남", "하노이"],
    "Thailand": ["thailand", "thai", "bangkok", "태국", "방콕"],
    "Philippines": ["philippines", "philippine", "filipino", "manila", "필리핀", "마닐라"],
    "Malaysia": ["malaysia", "malaysian", "kuala lumpur", "말레이시아"],
    "Australia": ["australia", "australian", "sydney", "melbourne", "canberra", "호주"],
    "United States": ["united states", "u.s.", "u.s.a", "america", "american",
                       "washington", "new york", "california", "texas", "florida", "미국"],
    "Canada": ["canada", "canadian", "ottawa", "toronto", "vancouver", "캐나다"],
    "Mexico": ["mexico", "mexican", "멕시코"],
    "Brazil": ["brazil", "brazilian", "brasilia", "sao paulo", "브라질"],
    "Chile": ["chile", "chilean", "santiago", "칠레"],
    "Argentina": ["argentina", "argentine", "buenos aires", "아르헨티나"],
    "United Kingdom": ["united kingdom", "u.k.", "britain", "british", "england",
                        "london", "scotland", "wales", "영국", "런던"],
    "Germany": ["germany", "german", "berlin", "munich", "독일", "베를린"],
    "France": ["france", "french", "paris", "프랑스", "파리"],
    "Netherlands": ["netherlands", "dutch", "amsterdam", "네덜란드"],
    "Spain": ["spain", "spanish", "madrid", "barcelona", "스페인", "마드리드"],
    "Italy": ["italy", "italian", "rome", "milan", "이탈리아", "로마"],
    "Ireland": ["ireland", "irish", "dublin", "아일랜드"],
    "Poland": ["poland", "polish", "warsaw", "폴란드"],
    "Sweden": ["sweden", "swedish", "stockholm", "스웨덴"],
    "Turkey": ["turkey", "turkish", "türkiye", "istanbul", "ankara", "튀르키예", "터키"],
    "Saudi Arabia": ["saudi", "riyadh", "jeddah", "사우디"],
    "United Arab Emirates": ["united arab emirates", "uae", "dubai", "abu dhabi",
                             "아랍에미리트", "두바이"],
    "Israel": ["israel", "israeli", "tel aviv", "jerusalem", "이스라엘"],
    "Egypt": ["egypt", "egyptian", "cairo", "이집트", "카이로"],
    "South Africa": ["south africa", "south african", "johannesburg", "cape town",
                     "남아프리카", "남아공"],
    "Nigeria": ["nigeria", "nigerian", "lagos", "abuja", "나이지리아"],
    "Russia": ["russia", "russian", "moscow", "러시아", "모스크바"],
    "Ukraine": ["ukraine", "ukrainian", "kyiv", "kiev", "우크라이나", "키이우", "키예프"],
}


def _loc_pattern(alias):
    """지명 별칭 -> 정규식. 라틴 문자는 단어 경계로, 한글은 부분 문자열로."""
    if re.search(r"[가-힣]", alias):
        return re.compile(re.escape(alias))
    esc = re.escape(alias)
    lead = r"\b" if alias[0].isalnum() else ""
    trail = r"\b" if alias[-1].isalnum() else ""
    return re.compile(lead + esc + trail, re.I)


_LOC_PATS = {c: [_loc_pattern(a) for a in aliases]
             for c, aliases in LOCATION_ALIASES.items()}


def strip_source(title, source):
    """Google News 제목 끝의 ' - 언론사명'을 떼어낸 순수 헤드라인."""
    if source and title.endswith(source):
        return title[:-len(source)].rstrip().rstrip("-–—·|").rstrip()
    return title


def mentions_country(text, country):
    """헤드라인에 해당 국가/도시가 실제로 언급됐는지."""
    pats = _LOC_PATS.get(country)
    if not pats:                       # 별칭 사전에 없는 국가 -> 이름 그대로 검사
        return country.lower() in text.lower()
    return any(p.search(text) for p in pats)


# 뉴스 검색 쿼리에 사용할 인프라/재난 키워드 (영어 위주 + 국가명)
QUERY_TERMS = (
    'earthquake OR flood OR typhoon OR hurricane OR wildfire OR "power outage" '
    'OR blackout OR explosion OR war OR missile OR cyberattack OR "data center" '
    'OR infrastructure OR strike OR protest OR "state of emergency"'
)

# 국가 -> Google News 지역 코드 (hl, gl, ceid). 없으면 기본 영어(US).
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

_cache = {}          # country -> (timestamp, payload)
_cache_lock = threading.Lock()

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _clean(text):
    """HTML 태그 제거 + 엔티티 디코드."""
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def parse_age_hours(pub_str):
    """pubDate 문자열 -> 지금으로부터 몇 시간 전인지. 파싱 실패 시 None."""
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
    """제목/요약 텍스트의 심각도 반환: (level, matched_keyword).

    위험 키워드가 걸려도 과거/회고/창작물 맥락이면 normal 로 강등한다.
    """
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
    params = urllib.parse.urlencode({
        "q": query, "hl": hl, "gl": gl, "ceid": ceid
    })
    return f"https://news.google.com/rss/search?{params}"


def fetch_country(country):
    """단일 국가 뉴스 스크래핑 + 심각도 집계."""
    url = build_rss_url(country)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    articles = []
    level_rank = {"normal": 0, "warning": 1, "critical": 2}
    worst = "normal"
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
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
            # 언론사명만으로 걸린 엉뚱한 지역 기사 제외
            # (예: Yahoo News Singapore 발 쿠바 정전 기사 -> 싱가포르에서 제외)
            headline = strip_source(title, source)
            if not mentions_country(headline, country):
                continue
            # 발행일 기준으로 오래된 기사는 제외 (최근 뉴스만 경고에 반영)
            age = parse_age_hours(pub)
            if age is None or age > MAX_AGE_HOURS:
                continue
            sev, kw = classify(title + " " + desc)
            if level_rank[sev] > level_rank[worst]:
                worst = sev
            articles.append({
                "title": title,
                "link": link,
                "pubDate": pub,
                "source": source,
                "severity": sev,
                "matched": kw or "",
                "age_hours": round(age, 1),
            })
    except Exception as e:  # noqa: BLE001
        return {"country": country, "level": "unknown",
                "error": str(e), "articles": [], "count": 0,
                "updated": int(time.time())}

    # 심각도 높은 순, 그 안에서는 최신순
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


# ---------------------------------------------------------------------------
# HTTP 핸들러
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # 콘솔 조용히

    def _send(self, code, body, content_type):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            fp = os.path.join(BASE_DIR, "index.html")
            try:
                with open(fp, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except FileNotFoundError:
                self._send(404, "index.html not found", "text/plain")
            return

        if path == "/api/news":
            qs = urllib.parse.parse_qs(parsed.query)
            countries = qs.get("countries", [""])[0]
            names = [c.strip() for c in countries.split(",") if c.strip()]
            results = {}
            # 병렬 스크래핑
            threads = []
            lock = threading.Lock()

            def work(name):
                data = get_country_cached(name)
                with lock:
                    results[name] = data

            for n in names[:40]:
                t = threading.Thread(target=work, args=(n,))
                t.start()
                threads.append(t)
            for t in threads:
                t.join()
            self._send(200, json.dumps(results, ensure_ascii=False),
                       "application/json; charset=utf-8")
            return

        self._send(404, "not found", "text/plain")


def main():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"인프라 뉴스 모니터 실행중 →  http://localhost:{PORT}")
    print("종료: Ctrl+C")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n종료합니다.")
        server.shutdown()


if __name__ == "__main__":
    main()
