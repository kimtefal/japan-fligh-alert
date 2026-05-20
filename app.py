"""
Japan Flight Deal Alert — Flask Web App
"""
import json
import logging
import os
import re
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

DATA_FILE = Path("/tmp/data.json")

# ── 기본 설정 ────────────────────────────────────────────────
DEFAULT_CONFIG = {
    "telegram_token": "",
    "telegram_chat_id": "",
    "alert_on": False,
    "interval_minutes": 30,
    "price_limits": {
        "NRT": 200000, "HND": 200000,
        "KIX": 200000, "ITM": 200000,
        "FUK": 160000,
        "CTS": 300000,
        "OKA": 300000,
        "NGO": 250000,
        "FSZ": 250000, "HKD": 250000,
        "TAK": 250000, "MYJ": 250000,
    },
}

CITY_NAMES = {
    "NRT": "도쿄 나리타", "HND": "도쿄 하네다",
    "KIX": "오사카 간사이", "ITM": "오사카 이타미",
    "FUK": "후쿠오카", "CTS": "삿포로",
    "OKA": "오키나와", "NGO": "나고야",
    "FSZ": "시즈오카", "HKD": "하코다테",
    "TAK": "다카마쓰", "MYJ": "마쓰야마",
}

AIRLINE_EVENTS = [
    {"name": "제주항공", "code": "JJ", "url": "https://www.jejuair.net/ko/main/default.do"},
    {"name": "진에어",   "code": "LJ", "url": "https://www.jinair.com/ko/promotion/list"},
    {"name": "에어부산", "code": "BX", "url": "https://www.airbusan.com/w/ko/event/eventList"},
    {"name": "티웨이",   "code": "TW", "url": "https://www.twayair.com/app/promotionEvent/list"},
    {"name": "에어서울", "code": "RS", "url": "https://flyairseoul.com/CW/ko/eventList.do"},
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9",
}

monitor_thread: Optional[threading.Thread] = None
stop_event = threading.Event()


# ── 데이터 저장/로드 ─────────────────────────────────────────
def load_data() -> dict:
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"config": DEFAULT_CONFIG.copy(), "sent_keys": [], "last_deals": [], "last_events": [], "last_checked": None}


def save_data(d: dict):
    DATA_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


# ── 일정 생성 ────────────────────────────────────────────────
HOLIDAYS = [
    ("2026-01-27", "2026-01-30"), ("2026-03-01", "2026-03-01"),
    ("2026-05-05", "2026-05-05"), ("2026-06-06", "2026-06-06"),
    ("2026-08-15", "2026-08-15"), ("2026-09-24", "2026-09-27"),
    ("2026-10-03", "2026-10-03"), ("2026-10-09", "2026-10-09"),
]


def get_trips():
    trips = []
    today = date.today()

    holiday_dates = set()
    for s, e in HOLIDAYS:
        sd, ed = date.fromisoformat(s), date.fromisoformat(e)
        d = sd
        while d <= ed:
            holiday_dates.add(d)
            d += timedelta(days=1)

    seen = set()
    for hday in sorted(holiday_dates):
        if hday < today:
            continue
        for offset in range(-1, 2):
            dep = hday + timedelta(days=offset)
            ret = dep + timedelta(days=3)
            key = (dep, ret)
            if key in seen:
                continue
            trip_days = [dep + timedelta(days=i) for i in range(4)]
            if any(d in holiday_dates for d in trip_days):
                seen.add(key)
                trips.append((dep, ret, "연휴 3박4일"))

    start = max(today, date(2026, 8, 1))
    cur = start
    while cur <= today + timedelta(days=120):
        wd = cur.weekday()
        if wd == 4:
            trips.append((cur, cur + timedelta(days=2), "금토일 2박3일"))
        elif wd == 5:
            trips.append((cur, cur + timedelta(days=2), "토일월 2박3일"))
        cur += timedelta(days=1)

    return trips[:40]


# ── 가격 조회 ────────────────────────────────────────────────
def fetch_naver_price(dep, arr, dep_date, ret_date) -> Optional[int]:
    d1, d2 = dep_date.strftime("%Y%m%d"), ret_date.strftime("%Y%m%d")
    url = f"https://flight.naver.com/flights/international/{dep}-{arr}-{d1}/{arr}-{dep}-{d2}?adult=1"
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        nums = re.findall(r'"totalPrice"\s*:\s*(\d+)', r.text)
        prices = [int(n) for n in nums if 50000 < int(n) < 2000000]
        return min(prices) if prices else None
    except Exception:
        return None


def build_naver_url(dep, arr, dep_date, ret_date):
    d1, d2 = dep_date.strftime("%Y%m%d"), ret_date.strftime("%Y%m%d")
    return f"https://flight.naver.com/flights/international/{dep}-{arr}-{d1}/{arr}-{dep}-{d2}?adult=1"


def build_skyscanner_url(dep, arr, dep_date, ret_date):
    d1, d2 = dep_date.strftime("%y%m%d"), ret_date.strftime("%y%m%d")
    return f"https://www.skyscanner.co.kr/transport/flights/{dep.lower()}/{arr.lower()}/{d1}/{d2}/?adults=1&currency=KRW"


def check_deals(config: dict) -> list:
    results = []
    trips = get_trips()
    for dep in ["ICN", "GMP"]:
        for arr, limit in config["price_limits"].items():
            for dep_date, ret_date, label in trips[:15]:
                if dep_date <= date.today():
                    continue
                price = fetch_naver_price(dep, arr, dep_date, ret_date)
                time.sleep(1.5)
                if price and price <= limit:
                    saving = round((1 - price / limit) * 100)
                    results.append({
                        "route": f"{dep} → {CITY_NAMES.get(arr, arr)}({arr})",
                        "dep": dep, "arr": arr,
                        "dep_date": dep_date.strftime("%Y.%m.%d"),
                        "ret_date": ret_date.strftime("%Y.%m.%d"),
                        "schedule": label,
                        "price": price,
                        "limit": limit,
                        "saving": saving,
                        "airline": "네이버항공",
                        "url": build_naver_url(dep, arr, dep_date, ret_date),
                        "skyscanner_url": build_skyscanner_url(dep, arr, dep_date, ret_date),
                        "reason": f"기준가 {limit:,}원 대비 {saving}% 저렴",
                    })
    return results


def check_events() -> list:
    results = []
    japan_kw = ["일본", "도쿄", "오사카", "후쿠오카", "삿포로", "오키나와", "나고야"]
    deal_kw = ["특가", "세일", "프로모션", "이벤트", "할인"]
    for cfg in AIRLINE_EVENTS:
        try:
            r = requests.get(cfg["url"], headers=HEADERS, timeout=10)
            soup = BeautifulSoup(r.text, "html.parser")
            seen = set()
            for tag in soup.find_all(["a", "div", "li", "h3", "h4"], limit=150):
                text = tag.get_text(" ", strip=True)
                if len(text) < 6 or len(text) > 150:
                    continue
                if not any(k in text for k in deal_kw):
                    continue
                if not any(k in text for k in japan_kw):
                    continue
                key = text[:30]
                if key in seen:
                    continue
                seen.add(key)
                href = tag.get("href", "") if tag.name == "a" else ""
                if href and not href.startswith("http"):
                    from urllib.parse import urljoin
                    href = urljoin(cfg["url"], href)
                results.append({
                    "airline": cfg["name"],
                    "code": cfg["code"],
                    "name": text[:80],
                    "url": href or cfg["url"],
                    "source": cfg["url"],
                })
                if len(results) >= 20:
                    break
        except Exception as e:
            log.warning(f"{cfg['name']} 스크래핑 실패: {e}")
        time.sleep(1)
    return results


# ── 텔레그램 알림 ────────────────────────────────────────────
def send_telegram(token: str, chat_id: str, text: str) -> bool:
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=10,
        )
        return r.status_code == 200
    except Exception:
        return False


def send_deal_alert(token, chat_id, deal):
    msg = (
        f"✈️ <b>일본 항공권 특가!</b>\n\n"
        f"🛫 <b>노선</b>: {deal['route']} 왕복\n"
        f"📅 <b>일정</b>: {deal['dep_date']} ~ {deal['ret_date']} ({deal['schedule']})\n"
        f"💴 <b>가격</b>: <b>{deal['price']:,}원</b> (기준가 대비 {deal['saving']}% ↓)\n"
        f"🏢 <b>판매처</b>: {deal['airline']}\n"
        f"💡 {deal['reason']}\n"
        f"🔗 <a href=\"{deal['url']}\">네이버항공 예약</a>  |  <a href=\"{deal['skyscanner_url']}\">스카이스캐너</a>"
    )
    send_telegram(token, chat_id, msg)


# ── 백그라운드 모니터 ────────────────────────────────────────
def monitor_loop():
    while not stop_event.is_set():
        d = load_data()
        cfg = d["config"]
        if not cfg.get("alert_on") or not cfg.get("telegram_token"):
            stop_event.wait(60)
            continue

        log.info("모니터링 검색 시작")
        try:
            deals = check_deals(cfg)
            d["last_deals"] = deals
            d["last_checked"] = datetime.now().isoformat()
            sent = set(d.get("sent_keys", []))
            for deal in deals:
                key = f"{deal['dep']}-{deal['arr']}-{deal['dep_date']}"
                if key not in sent:
                    send_deal_alert(cfg["telegram_token"], cfg["telegram_chat_id"], deal)
                    sent.add(key)
            d["sent_keys"] = list(sent)[-200:]
            save_data(d)
        except Exception as e:
            log.error(f"모니터 오류: {e}")

        interval = cfg.get("interval_minutes", 30) * 60
        stop_event.wait(interval)


def start_monitor():
    global monitor_thread, stop_event
    stop_event.clear()
    monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
    monitor_thread.start()


# ── API 라우트 ───────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/config", methods=["GET"])
def get_config():
    d = load_data()
    cfg = d["config"].copy()
    if cfg.get("telegram_token"):
        cfg["telegram_token_masked"] = cfg["telegram_token"][:8] + "••••••••"
    return jsonify(cfg)


@app.route("/api/config", methods=["POST"])
def set_config():
    d = load_data()
    body = request.json
    d["config"].update({k: v for k, v in body.items() if k in DEFAULT_CONFIG})
    save_data(d)

    if d["config"].get("alert_on"):
        stop_event.set()
        time.sleep(0.2)
        start_monitor()
    else:
        stop_event.set()

    return jsonify({"ok": True})


@app.route("/api/search/deals", methods=["POST"])
def api_search_deals():
    d = load_data()
    cfg = d["config"]
    try:
        deals = check_deals(cfg)
        d["last_deals"] = deals
        d["last_checked"] = datetime.now().isoformat()
        save_data(d)
        return jsonify({"ok": True, "deals": deals, "count": len(deals)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/search/events", methods=["POST"])
def api_search_events():
    d = load_data()
    try:
        events = check_events()
        d["last_events"] = events
        save_data(d)
        return jsonify({"ok": True, "events": events, "count": len(events)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/last", methods=["GET"])
def api_last():
    d = load_data()
    return jsonify({
        "deals": d.get("last_deals", []),
        "events": d.get("last_events", []),
        "last_checked": d.get("last_checked"),
    })


@app.route("/api/telegram/test", methods=["POST"])
def api_test_telegram():
    body = request.json
    token = body.get("token", "")
    chat_id = body.get("chat_id", "")
    if not token or not chat_id:
        return jsonify({"ok": False, "error": "토큰과 chat_id를 입력하세요"})
    ok = send_telegram(token, chat_id, "✅ <b>연결 테스트 성공!</b>\n일본 특가 알리미가 정상 연결됐습니다.")
    return jsonify({"ok": ok, "error": "" if ok else "전송 실패. 토큰/chat_id 확인 필요"})


@app.route("/api/telegram/chatid", methods=["POST"])
def api_get_chatid():
    token = (request.json or {}).get("token", "")
    if not token:
        return jsonify({"ok": False, "error": "토큰을 입력하세요"})
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=10)
        data = r.json()
        results = data.get("result", [])
        if not results:
            return jsonify({"ok": False, "error": "봇에게 /start 메시지를 먼저 보내세요"})
        chat = results[-1].get("message", {}).get("chat", {})
        return jsonify({"ok": True, "chat_id": str(chat.get("id", "")), "name": chat.get("first_name", "")})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


if __name__ == "__main__":
    start_monitor()
    app.run(host="0.0.0.0", port=5000, debug=False)
