# -*- coding: utf-8 -*-
"""
Vercel 서버리스 함수: /api/disasters

재난 관측기관의 '실측 데이터'를 모아 전 세계 자연재해를 빠짐없이 제공한다.
 - USGS: 전 세계 지진 (규모 4.5+ 최근 1일)
 - GDACS(UN/EU): 태풍·홍수·화산·가뭄·산불 (주황/빨강 경보만; 지진은 USGS로 커버)

뉴스 스크래핑과 독립적이라 '자연재해 누락'을 방지한다. API 키 불필요.
"""

import json
import time
import threading
import urllib.request
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler

UA = {"User-Agent": "infra-monitor-disasters"}
USGS_URL = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/significant_week.geojson"
MIN_QUAKE_MAG = 6.0   # 규모 6.0 미만 지진은 표시하지 않음
GDACS_URL = "https://www.gdacs.org/xml/rss.xml"
GDACS_NS = {"gdacs": "http://www.gdacs.org",
            "geo": "http://www.w3.org/2003/01/geo/wgs84_pos#"}
GDACS_TYPE = {"EQ": "지진", "TC": "태풍", "FL": "홍수",
              "VO": "화산", "DR": "가뭄", "WF": "산불", "TS": "쓰나미"}


def _get(url, timeout=8):
    return urllib.request.urlopen(
        urllib.request.Request(url, headers=UA), timeout=timeout).read()


def _bounded(fn, seconds=12):
    """느린 피드가 응답을 찔끔찔끔 보내도 절대 매달리지 않게 하드 데드라인."""
    box = [[]]

    def run():
        try:
            box[0] = fn()
        except Exception:  # noqa: BLE001
            box[0] = []
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(seconds)
    return box[0]


def fetch_usgs():
    out = []
    try:
        d = json.loads(_get(USGS_URL))
        for f in d.get("features", []):
            p = f.get("properties", {}) or {}
            c = (f.get("geometry", {}) or {}).get("coordinates") or [None, None, None]
            mag = p.get("mag") or 0
            if mag < MIN_QUAKE_MAG:      # 규모 6.0 미만 제외
                continue
            place = p.get("place", "") or ""
            out.append({
                "source": "USGS", "type": "지진", "severity": "critical",
                "title": f"규모 {mag:.1f} 지진 · {place}",
                "place": place, "mag": round(mag, 1),
                "lat": c[1], "lon": c[0],
                "time": p.get("time"),           # epoch ms
                "url": p.get("url", ""),
                "tsunami": bool(p.get("tsunami")),
            })
    except Exception:  # noqa: BLE001
        pass
    return out


def fetch_gdacs():
    out = []
    try:
        root = ET.fromstring(_get(GDACS_URL))
        for it in root.findall(".//item"):
            al = (it.findtext("gdacs:alertlevel", namespaces=GDACS_NS) or "").strip()
            et = (it.findtext("gdacs:eventtype", namespaces=GDACS_NS) or "").strip()
            if et == "EQ":            # 지진은 USGS(정밀)로 커버
                continue
            if al not in ("Orange", "Red"):   # Green(경미) 제외
                continue
            lat = it.findtext(".//geo:lat", namespaces=GDACS_NS)
            lon = it.findtext(".//geo:long", namespaces=GDACS_NS)
            country = it.findtext("gdacs:country", namespaces=GDACS_NS) or ""
            out.append({
                "source": "GDACS",
                "type": GDACS_TYPE.get(et, et),
                "severity": "critical" if al == "Red" else "warning",
                "title": (it.findtext("title", "") or "").strip(),
                "place": country.strip(),
                "lat": float(lat) if lat else None,
                "lon": float(lon) if lon else None,
                "time": (it.findtext("pubDate", "") or "").strip(),
                "url": (it.findtext("link", "") or "").strip(),
            })
    except Exception:  # noqa: BLE001
        pass
    return out


class handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        try:
            # 두 피드를 각각 하드 데드라인으로 (한쪽이 느려도 다른 쪽은 표시)
            ru, rg = [[]], [[]]
            tu = threading.Thread(target=lambda: ru.__setitem__(0, _bounded(fetch_usgs)), daemon=True)
            tg = threading.Thread(target=lambda: rg.__setitem__(0, _bounded(fetch_gdacs)), daemon=True)
            tu.start(); tg.start(); tu.join(14); tg.join(14)
            events = ru[0] + rg[0]
            order = {"critical": 0, "warning": 1, "normal": 2}
            events.sort(key=lambda e: order.get(e["severity"], 3))
            payload = {"events": events[:80], "updated": int(time.time())}
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
        except Exception as e:  # noqa: BLE001
            body = json.dumps({"events": [], "error": str(e)},
                              ensure_ascii=False).encode("utf-8")
            self.send_response(500)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
