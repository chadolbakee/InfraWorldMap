# -*- coding: utf-8 -*-
"""
인프라 뉴스 모니터 - 경고 메일 알림 (로컬 실행용)

배포된 API를 조회해서 '경고(critical)' 등급 뉴스가 새로 뜨면
지정한 메일로 알림을 보낸다. 같은 경보는 중복 발송하지 않는다.

설정: mail_config.json (mail_config.example.json 을 복사해서 채우기)
      또는 환경변수 (ALERT_GMAIL_USER / ALERT_GMAIL_APP_PASSWORD / ALERT_TO ...)

실행:
  python alert_mailer.py            # 1회 확인·발송
  python alert_mailer.py --loop 600 # 600초(10분)마다 반복
  python alert_mailer.py --test     # 테스트 메일 1통 발송 (설정 확인용)
  python alert_mailer.py --dry-run  # 메일 대신 화면에 출력 (자격증명 없이 확인)

⚠ 앱 비밀번호 등 자격증명은 mail_config.json 에 직접 넣으세요.
  이 파일은 .gitignore 로 제외되어 저장소에 올라가지 않습니다.
"""

import argparse
import json
import os
import smtplib
import ssl
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formataddr

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "mail_config.json")
STATE_PATH = os.path.join(HERE, "alert_seen.json")
DEFAULT_API = "https://infra-world-map.vercel.app"
DEFAULT_COUNTRIES = ["South Korea", "Japan", "United States",
                     "Saudi Arabia", "China", "Canada", "Mexico"]


def load_config():
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
    # 환경변수로 덮어쓰기 허용
    cfg.setdefault("gmail_user", os.environ.get("ALERT_GMAIL_USER", ""))
    cfg.setdefault("gmail_app_password", os.environ.get("ALERT_GMAIL_APP_PASSWORD", ""))
    cfg.setdefault("to", os.environ.get("ALERT_TO", "shawnrock619@gmail.com"))
    cfg.setdefault("api_base", os.environ.get("ALERT_API", DEFAULT_API))
    cfg.setdefault("countries", DEFAULT_COUNTRIES)
    return cfg


def load_seen():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:  # noqa: BLE001
            return set()
    return set()


def save_seen(seen):
    # 너무 커지지 않게 최근 1000개만
    data = list(seen)[-1000:]
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def fetch_news(api_base, countries):
    q = urllib.parse.quote(",".join(countries))
    url = f"{api_base.rstrip('/')}/api/news?countries={q}"
    req = urllib.request.Request(url, headers={"User-Agent": "infra-alert-mailer"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def collect_critical(data):
    """국가별 데이터에서 경고(critical) 기사만 뽑는다."""
    out = []
    for country, v in (data or {}).items():
        if not isinstance(v, dict):
            continue
        for a in v.get("articles", []):
            if a.get("severity") == "critical":
                out.append((country, a))
    return out


def build_email(cfg, fresh):
    """fresh: [(country, article)] -> (subject, text, html)."""
    regions = []
    for country, a in fresh:
        r = a.get("region")
        regions.append(f"{country}" + (f"({r})" if r else ""))
    uniq = []
    for x in regions:
        if x not in uniq:
            uniq.append(x)
    subject = f"[인프라 경보] 경고 {len(fresh)}건 · " + ", ".join(uniq[:4])

    lines = ["글로벌 인프라 자산 뉴스 모니터 — 경고(critical) 알림", ""]
    rows_html = []
    for country, a in fresh:
        tag = f"#{a.get('tag')}" if a.get("tag") else ""
        infra = " [인프라피해]" if a.get("infra") else ""
        region = f" · 📍{a.get('region')}" if a.get("region") else ""
        title = a.get("title", "")
        src = a.get("source", "")
        link = a.get("link", "")
        lines.append(f"🔴 {country}{region}  {tag}{infra}")
        lines.append(f"   {title}")
        lines.append(f"   {src}")
        lines.append(f"   {link}")
        lines.append("")
        rows_html.append(f"""
          <div style="border-left:4px solid #f85149;padding:8px 12px;margin:10px 0;background:#161b22;">
            <div style="color:#8b949e;font-size:12px;">🔴 {country}{region} &nbsp; <b style="color:#f85149;">{tag}</b>{infra}</div>
            <div style="color:#e6edf3;font-size:15px;font-weight:600;margin:4px 0;">
              <a href="{link}" style="color:#e6edf3;text-decoration:none;">{title}</a></div>
            <div style="color:#8b949e;font-size:12px;">{src}</div>
          </div>""")
    lines.append("대시보드: " + cfg["api_base"])
    text = "\n".join(lines)
    html = f"""<div style="font-family:sans-serif;background:#0d1117;padding:20px;">
      <h2 style="color:#f85149;">🔴 인프라 경보 {len(fresh)}건</h2>
      {''.join(rows_html)}
      <p style="margin-top:16px;"><a href="{cfg['api_base']}" style="color:#388bfd;">대시보드 열기</a></p>
    </div>"""
    return subject, text, html


def recipients_of(cfg):
    """to 를 리스트로 정규화 (리스트 또는 콤마구분 문자열 모두 허용)."""
    to = cfg.get("to", "")
    if isinstance(to, list):
        return [x.strip() for x in to if x and x.strip()]
    return [x.strip() for x in str(to).split(",") if x.strip()]


def send_email(cfg, subject, text, html):
    user = cfg.get("gmail_user", "").strip()
    pw = cfg.get("gmail_app_password", "").replace(" ", "").strip()
    to_list = recipients_of(cfg)
    if not user or not pw:
        raise RuntimeError("gmail_user / gmail_app_password 가 설정되지 않았습니다. "
                           "mail_config.json 을 확인하세요.")
    if not to_list:
        raise RuntimeError("수신자(to) 가 비어 있습니다.")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr(("인프라 뉴스 모니터", user))
    msg["To"] = ", ".join(to_list)
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as server:
        server.login(user, pw)
        server.sendmail(user, to_list, msg.as_string())


_SEV_EMOJI = {"critical": "🔴", "warning": "🟡", "normal": "🟢", "unknown": "⚪"}
_SEV_ORDER = {"critical": 0, "warning": 1, "normal": 2, "unknown": 3}


def build_daily_email(cfg, data):
    """모든 감시 국가의 현재 상태를 요약한 일일 메일."""
    date = datetime.now().strftime("%Y-%m-%d")
    items = [(c, v) for c, v in (data or {}).items() if isinstance(v, dict)]
    items.sort(key=lambda kv: _SEV_ORDER.get(kv[1].get("level", "unknown"), 3))
    ncrit = sum(v.get("critical_count", 0) for _, v in items)
    nwarn = sum(v.get("warning_count", 0) for _, v in items)
    subject = f"[인프라 일일요약] {date} · 경고 {ncrit} · 주의 {nwarn}"

    lines = [f"글로벌 인프라 자산 뉴스 모니터 — 일일 요약 ({date})", ""]
    rows_html = []
    for c, v in items:
        lvl = v.get("level", "unknown")
        em = _SEV_EMOJI.get(lvl, "⚪")
        regions = v.get("regions") or []
        rtxt = ("  📍" + ", ".join(regions)) if regions else ""
        lines.append(f"{em} {c} — 심각 {v.get('critical_count',0)} · 주의 {v.get('warning_count',0)}{rtxt}")
        # 위험 기사 최대 2건
        haz = [a for a in v.get("articles", []) if a.get("severity") in ("critical", "warning")][:2]
        for a in haz:
            tag = f"#{a.get('tag')}" if a.get("tag") else ""
            lines.append(f"    - {tag} {a.get('title','')}")
        rows_html.append(f"""
          <div style="padding:8px 12px;margin:6px 0;background:#161b22;border-radius:6px;">
            <div style="color:#e6edf3;font-size:15px;font-weight:600;">{em} {c}
              <span style="color:#8b949e;font-size:12px;font-weight:400;">
              심각 {v.get('critical_count',0)} · 주의 {v.get('warning_count',0)}{('  📍'+', '.join(regions)) if regions else ''}</span></div>
            {''.join(f'<div style="color:#8b949e;font-size:13px;margin-top:3px;">• {("#"+a["tag"]) if a.get("tag") else ""} {a.get("title","")}</div>' for a in haz)}
          </div>""")
    lines.append("")
    lines.append("대시보드: " + cfg["api_base"])
    text = "\n".join(lines)
    html = f"""<div style="font-family:sans-serif;background:#0d1117;padding:20px;">
      <h2 style="color:#e6edf3;">📋 인프라 일일 요약 <span style="color:#8b949e;font-size:14px;">{date}</span></h2>
      <div style="color:#8b949e;margin-bottom:10px;">경고 {ncrit}건 · 주의 {nwarn}건</div>
      {''.join(rows_html)}
      <p style="margin-top:16px;"><a href="{cfg['api_base']}" style="color:#388bfd;">대시보드 열기</a></p>
    </div>"""
    return subject, text, html


def run_daily(cfg, dry_run=False):
    data = fetch_news(cfg["api_base"], cfg["countries"])
    subject, text, html = build_daily_email(cfg, data)
    ts = datetime.now().strftime("%H:%M:%S")
    if dry_run:
        print(f"[{ts}] (DRY-RUN) 일일요약:\n{'='*60}\n{subject}\n{'-'*60}\n{text}\n{'='*60}")
    else:
        send_email(cfg, subject, text, html)
        print(f"[{ts}] 일일 요약 메일 발송 완료: {subject}")


def run_once(cfg, dry_run=False):
    data = fetch_news(cfg["api_base"], cfg["countries"])
    crit = collect_critical(data)
    seen = load_seen()
    fresh = [(c, a) for c, a in crit if a.get("link") and a["link"] not in seen]
    ts = datetime.now().strftime("%H:%M:%S")
    if not fresh:
        print(f"[{ts}] 새 경고 없음 (경고 기사 총 {len(crit)}건, 모두 기존).")
        return
    subject, text, html = build_email(cfg, fresh)
    if dry_run:
        print(f"[{ts}] (DRY-RUN) 발송할 메일:\n{'='*60}\n{subject}\n{'-'*60}\n{text}\n{'='*60}")
    else:
        send_email(cfg, subject, text, html)
        print(f"[{ts}] 메일 발송 완료: {subject}  → {cfg['to']}")
    # 첫 실행에서 모든 기존 경고까지 한꺼번에 안 보내도록, 발송했든 dry든 seen 갱신
    for _c, a in crit:
        if a.get("link"):
            seen.add(a["link"])
    save_seen(seen)


def send_test(cfg):
    subject = "[인프라 경보] 테스트 메일"
    text = "설정이 정상입니다. 실제 경고 발생 시 이런 형식으로 메일이 옵니다.\n대시보드: " + cfg["api_base"]
    html = f'<div style="font-family:sans-serif;"><h3>✅ 설정 정상</h3><p>실제 경고 발생 시 알림이 발송됩니다.</p><p><a href="{cfg["api_base"]}">대시보드</a></p></div>'
    send_email(cfg, subject, text, html)
    print("테스트 메일 발송 완료 →", cfg["to"])


def main():
    try:  # Windows 콘솔(cp949)에서 이모지/특수문자 출력 시 크래시 방지
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", type=int, default=0, help="N초마다 반복 (0=1회)")
    ap.add_argument("--dry-run", action="store_true", help="메일 대신 화면 출력")
    ap.add_argument("--test", action="store_true", help="테스트 메일 1통")
    ap.add_argument("--daily", action="store_true", help="일일 요약 메일 1통")
    args = ap.parse_args()
    cfg = load_config()
    if args.test:
        send_test(cfg); return
    if args.daily:
        run_daily(cfg, args.dry_run); return
    if args.loop > 0:
        print(f"감시 시작: {args.loop}초 간격, 대상 {cfg['countries']}")
        while True:
            try:
                run_once(cfg, args.dry_run)
            except Exception as e:  # noqa: BLE001
                print("오류:", e, file=sys.stderr)
            time.sleep(args.loop)
    else:
        run_once(cfg, args.dry_run)


if __name__ == "__main__":
    main()
