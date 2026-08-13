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
PER_TAG_LIMIT = 2
CACHE_TTL = 300
REQUEST_TIMEOUT = 6   # 서버리스 함수 실행시간 제한 때문에 여유있게 짧게

# 위험 유형별 (한국어 태그 -> 키워드들). 기사 옆에 #홍수 같은 한국어 태그 표시용.
CRITICAL_CATEGORIES = {
    "지진":     ["earthquake", "지진", "magnitude"],
    "쓰나미":   ["tsunami", "쓰나미", "지진해일", "해일"],
    "태풍":     ["typhoon", "태풍", "hurricane", "허리케인", "cyclone"],
    "홍수":     ["flooding", "floods", "홍수", "침수"],
    "산불":     ["wildfire", "wildfires", "산불"],
    "화산":     ["volcano", "화산", "eruption"],
    "산사태":   ["landslide", "산사태", "mudslide"],
    "정전":     ["power outage", "blackout", "정전", "grid failure", "grid collapse"],
    "폭발":     ["explosion", "explosions", "폭발"],
    "붕괴":     ["building collapse", "bridge collapse", "붕괴", "derailment"],
    "인프라피해": ["pipeline rupture", "송유관", "data center outage", "데이터센터 화재"],
    "LNG터미널": ["lng terminal", "lng plant", "LNG 터미널", "액화천연가스 터미널"],
    "해저케이블": ["submarine cable", "subsea cable", "해저케이블", "해저 케이블"],
    "공습":     ["airstrike", "air strike", "drone strike", "공습",
                 "missile strike", "invasion", "침공"],
    "테러":     ["terror attack", "테러", "sabotage"],
    "쿠데타":   ["coup", "쿠데타"],
    "대피":     ["evacuation ordered", "evacuate", "대피령", "대피"],
    "비상사태": ["state of emergency", "비상사태"],
    "사상자":   ["death toll"],
    "원전사고": ["meltdown"],
}

WARNING_CATEGORIES = {
    "폭풍":     ["storm", "폭풍", "storm warning"],
    "폭우":     ["heavy rain", "폭우"],
    "폭염":     ["heatwave", "heat wave", "폭염"],
    "가뭄":     ["drought", "가뭄"],
    "홍수주의": ["flood warning", "홍수 주의"],
    "시위":     ["protest", "시위", "unrest", "riot", "폭동"],
    "파업":     ["strike", "파업"],
    "전쟁":     ["war", "전쟁"],
    "미사일":   ["missile", "미사일"],
    "제재":     ["sanction", "제재"],
    "긴장":     ["tension", "긴장", "military", "border clash"],
    "장애":     ["outage", "장애", "disruption", "차질"],
    "부족":     ["shortage", "부족"],
}

CRITICAL_KEYWORDS = [kw for kws in CRITICAL_CATEGORIES.values() for kw in kws]
WARNING_KEYWORDS = [kw for kws in WARNING_CATEGORIES.values() for kw in kws]
_KW_TAG = {}
for _tag, _kws in {**CRITICAL_CATEGORIES, **WARNING_CATEGORIES}.items():
    for _kw in _kws:
        _KW_TAG[_kw] = _tag
_KW_TAG["refinery"] = "정유시설"

# 정유시설 특례: 사고 상황일 때만 + 아래 지역에서만 노출
REFINERY_REGIONS = {"South Korea", "Saudi Arabia", "Mexico",
                    "United States", "Canada"}
_REFINERY_RE = re.compile(r"\brefinery\b|정유공장|정유시설", re.I)
_ACCIDENT_RE = re.compile(
    r"\b(fire|blaze|explosion|blast|exploded|leak|leaks|spill|outage|"
    r"evacuat\w*|killed|injured|injuries|damage\w*|destroyed|blackout)\b"
    r"|화재|폭발|누출|유출|사고|폭음", re.I)


def refinery_incident(text):
    return bool(_REFINERY_RE.search(text) and _ACCIDENT_RE.search(text))


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

# 인프라 영향 신호 — 경고(빨강)는 인프라 자산/운영 피해가 있을 때만 (한국 인명피해 예외)
INFRA_IMPACT_KEYWORDS = [
    "power plant", "power station", "발전소", "wind turbine", "wind farm",
    "풍력", "solar farm", "태양광", "substation", "변전소", "transformer",
    "변압기", "power line", "transmission line", "송전", "power grid",
    "grid", "전력망", "power outage", "blackout", "정전", "단전",
    "pipeline", "송유관", "가스관", "refinery", "정유공장", "정유시설",
    "lng terminal", "oil terminal", "gas terminal",
    "port", "항만", "항구", "airport", "공항", "railway", "railroad",
    "철도", "highway", "고속도로", "bridge", "교량", "tunnel", "터널",
    "data center", "데이터센터", "submarine cable", "subsea cable",
    "해저케이블", "telecom", "통신망", "network outage",
    "dam", "댐", "reservoir", "저수지", "water treatment", "정수장",
    "factory", "공장", "plant closure", "가동중단", "가동 중단",
    "infrastructure", "인프라", "critical infrastructure",
    "damaged", "damage to", "destroyed", "파손", "손상",
    "shutdown", "shut down", "supply disruption", "공급 중단",
    "operations suspended", "운영 중단", "service disruption", "outage",
]
_INFRA_PATS = _compile(INFRA_IMPACT_KEYWORDS)


def has_infra_impact(text):
    for _kw, pat in _INFRA_PATS:
        if pat.search(text):
            return True
    return False


CASUALTY_KEYWORDS = [
    "death toll", "kill", "kills", "killed", "killing", "dead", "deaths",
    "die", "dies", "died", "fatal", "fatalities", "casualty", "casualties",
    "injure", "injures", "injured", "injury", "injuries",
    "missing", "trapped", "buries", "buried",
    "사망", "숨져", "숨진", "숨졌", "부상", "실종", "매몰", "인명피해", "희생", "사상자",
]
_CASUALTY_PATS = _compile(CASUALTY_KEYWORDS)


def has_casualty(text):
    for _kw, pat in _CASUALTY_PATS:
        if pat.search(text):
            return True
    return False


_STORM_RE = re.compile(
    r"\b(?:typhoon|hurricane|cyclone|tropical storm|tropical depression|storm)"
    r"\s+([A-Z][a-z]+)", re.I)


def event_key(a):
    m = _STORM_RE.search(a.get("title", ""))
    if m:
        return "storm:" + m.group(1).lower()
    return a.get("tag") or None


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


# 스포츠 / 연예 / 가십 맥락 -> 비유적 위험 키워드를 실제 재난으로 오인하지 않게 강등
NOISE_KEYWORDS = [
    "football", "soccer", "축구", "basketball", "농구", "baseball", "야구",
    "world cup", "월드컵", "olympic", "올림픽", "league", "리그",
    "fans", "striker", "midfielder", "goalkeeper", "transfer window",
    "river plate", "boca juniors", "real madrid", "manchester united",
    "derby", "playoff", "플레이오프", "nba", "nfl", "mlb",
    "trophy", "tournament", "championship", "선수권", "coach",
    "rising star", "the story of", "idol", "아이돌", "celebrity",
    "가수", "rapper", "influencer",
    # 야구 등 스포츠 동음이의어 방지 (baseball 'strike' 오탐)
    "strikeout", "strike three", "삼진", "투수", "홈런", "home run",
    "no-hitter", "grand slam", "이닝",
    # 비즈니스/마케팅 경쟁 비유 ("war"를 시장 경쟁·유행에 비유)
    "price war", "trade war", "tariff war", "burger war", "streaming war",
    "bidding war", "turf war", "culture war", "war of words", "fare war",
    "console war", "format war", "talent war", "chip war", "brand war",
    "price battle", "burger", "noodles", "market share", "startup",
    "ipo", "e-commerce", "quarterly earnings",
    # 외교 분쟁 (물리적 위협 아님) — 영유권 항의/외교적 항의
    "lodge a protest", "lodges a protest", "lodged a protest",
    "diplomatic protest", "formal protest", "protest note",
    "territorial dispute", "disputed island", "독도", "dokdo",
    "takeshima", "senkaku",
    # 구호 / 모금 / 자선 (실제 재난은 다른 곳/과거)
    "appeal for", "relief effort", "relief fund", "relief appeal",
    "aid appeal", "fundraiser", "fundraising", "charity", "donation",
    "성금", "모금", "구호",
    # 스포츠 대회 (축구 등)
    "cup", "matchday", "goalless", "final eight", "quarter-final", "semi-final",
    # 게임 / 소프트웨어 (예: "US-China war" 가 게임 mod 이야기)
    "video game", "videogame", "게임", "gaming", "gameplay", "game mod",
    "modding", "esports", "e-sports", "playstation", "xbox", "nintendo",
    "company of heroes",
]
_NOISE_PATS = _compile(NOISE_KEYWORDS)

# 출처(언론사) 기반 노이즈 — 게임·스포츠·연예 전문 매체는 통째로 강등
_NOISE_SOURCE_RE = re.compile(
    r"\.games\b|\bign\b|polygon|kotaku|pc\s*gamer|gamesradar|eurogamer|"
    r"gamespot|rock paper shotgun|gamerant|dexerto|dot esports|"
    r"goal\.com|\bespn\b|sky\s*sports|football|soccer|"
    r"billboard|pitchfork|variety|hollywood", re.I)


def is_noise_source(source):
    return bool(source and _NOISE_SOURCE_RE.search(source))


def looks_noise(text):
    for _kw, pat in _NOISE_PATS:
        if pat.search(text):
            return True
    return False


# 기상 '기록/마일스톤' 뉴스 (온도 기록이 주제). 기록적 폭우/홍수는 안 건드림.
_WEATHER_RECORD_RE = re.compile(
    r"\bhottest\b.{0,25}\b(ever|on record|in history)\b"
    r"|\bwarmest\b.{0,25}\b(ever|on record|in history)\b"
    r"|\brecord[- ]high temperature"
    r"|\bhighest temperature\b.{0,20}\b(ever|recorded|on record)\b"
    r"|가장\s*더운|역대\s*최고\s*기온|최고\s*기온\s*경신",
    re.I)


def looks_weather_record(text):
    return bool(_WEATHER_RECORD_RE.search(text))


def should_demote(text):
    return (looks_historical(text) or looks_noise(text)
            or looks_weather_record(text))


# 지명 필터: 언론사명(예: Yahoo News Singapore)만으로 걸린 엉뚱한 지역 기사 제외.
# 헤드라인 본문에 그 나라/도시가 실제 언급된 기사만 남긴다.
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
    if re.search(r"[가-힣]", alias):
        return re.compile(re.escape(alias))
    esc = re.escape(alias)
    lead = r"\b" if alias[0].isalnum() else ""
    trail = r"\b" if alias[-1].isalnum() else ""
    return re.compile(lead + esc + trail, re.I)


_LOC_PATS = {c: [_loc_pattern(a) for a in aliases]
             for c, aliases in LOCATION_ALIASES.items()}


def strip_source(title, source):
    if source and title.endswith(source):
        return title[:-len(source)].rstrip().rstrip("-–—·|").rstrip()
    return title


def mentions_country(text, country):
    pats = _LOC_PATS.get(country)
    if not pats:
        return country.lower() in text.lower()
    return any(p.search(text) for p in pats)


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
    'OR blackout OR explosion OR war OR missile OR "data center" '
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
    # 위험 키워드가 걸려도 과거/창작물/스포츠/연예 맥락이면 normal 로 강등
    for kw, pat in _CRIT_PATS:
        if pat.search(text):
            return ("normal", None) if should_demote(text) else ("critical", kw)
    for kw, pat in _WARN_PATS:
        if pat.search(text):
            return ("normal", None) if should_demote(text) else ("warning", kw)
    return "normal", None


def build_rss_url(country):
    hl, gl, ceid = COUNTRY_LOCALE.get(country, DEFAULT_LOCALE)
    query = f'{country} ({QUERY_TERMS}) when:2d'
    params = urllib.parse.urlencode({"q": query, "hl": hl, "gl": gl, "ceid": ceid})
    return f"https://news.google.com/rss/search?{params}"


def dedupe_by_tag(articles):
    """같은 사건(event_key: 폭풍 이름 또는 태그)은 최신 PER_TAG_LIMIT 개만 남긴다."""
    total = {}
    for a in articles:
        k = event_key(a)
        if k:
            total[k] = total.get(k, 0) + 1
    seen, first_of, kept = {}, {}, []
    for a in articles:
        k = event_key(a)
        if not k:
            kept.append(a)
            continue
        if seen.get(k, 0) < PER_TAG_LIMIT:
            first_of.setdefault(k, a)
            seen[k] = seen.get(k, 0) + 1
            kept.append(a)
    for k, a in first_of.items():
        extra = total[k] - seen[k]
        if extra > 0:
            a["dup_count"] = extra
    return kept


def fetch_country(country, refinery_ok=None):
    if refinery_ok is None:
        refinery_ok = country in REFINERY_REGIONS
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
            # 언론사명만으로 걸린 엉뚱한 지역 기사 제외
            headline = strip_source(title, source)
            if not mentions_country(headline, country):
                continue
            age = parse_age_hours(pub)
            if age is None or age > MAX_AGE_HOURS:
                continue
            # 심각도는 순수 헤드라인으로만 판정 (요약은 관련기사·언론사명이 섞임)
            sev, kw = classify(headline)
            # 게임/스포츠/연예 전문 매체발 기사는 강등
            if sev != "normal" and is_noise_source(source):
                sev, kw = "normal", None
            # 정유시설 특례: 허용 지역 + 사고 상황일 때만 경고로 승격
            if refinery_ok and refinery_incident(headline) \
                    and not should_demote(headline):
                sev, kw = "critical", "refinery"
            # 인프라 영향 게이팅: 경고는 인프라 피해가 있을 때만 (한국 인명피해 예외)
            infra = (kw == "refinery") or has_infra_impact(headline)
            if sev == "critical" and not infra:
                if not (country == "South Korea" and has_casualty(headline)):
                    sev = "warning"
            if level_rank[sev] > level_rank[worst]:
                worst = sev
            articles.append({
                "title": title, "link": link, "pubDate": pub, "source": source,
                "severity": sev, "matched": kw or "",
                "tag": _KW_TAG.get(kw, "") if kw else "",
                "infra": infra,
                "age_hours": round(age, 1),
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
        "articles": dedupe_by_tag(articles)[:MAX_ARTICLES],
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

    def _write_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        try:
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)
            countries = qs.get("countries", [""])[0]
            names = [c.strip() for c in countries.split(",") if c.strip()]

            # 파라미터 없이 호출 -> 헬스체크 (함수 생존 확인용)
            if not names:
                self._write_json(200, {
                    "ok": True,
                    "message": "news function is alive",
                    "usage": "/api/news?countries=Japan,United States",
                })
                return

            results = {}
            lock = threading.Lock()

            def work(name):
                try:
                    data = get_country_cached(name)
                except Exception as e:  # noqa: BLE001
                    data = {"country": name, "level": "unknown",
                            "error": str(e), "articles": [], "count": 0}
                with lock:
                    results[name] = data

            threads = []
            for n in names[:15]:  # 서버리스 실행시간 한도 보호
                t = threading.Thread(target=work, args=(n,))
                t.start()
                threads.append(t)
            for t in threads:
                t.join()

            self._write_json(200, results)
        except Exception as e:  # noqa: BLE001 - 절대 HTML 에러페이지로 죽지 않게
            self._write_json(500, {"error": "internal", "detail": str(e)})
