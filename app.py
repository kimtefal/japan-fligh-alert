"""
Japan Flight Deal Alert — Flask Web App v3
- 모니터링: 노선별 분할 검색 (타임아웃 방지)
- 알림 로그: 성공/실패/사유 기록
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
from flask import Flask, Response, jsonify, render_template, request

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

DATA_FILE   = Path("/tmp/data.json")
LOG_FILE    = Path("/tmp/alert_log.json")

DEFAULT_CONFIG = {
    "telegram_token": "",
    "telegram_chat_id": "",
    "alert_on": False,
    "interval_minutes": 30,
    "price_limits": {
        "NRT": 200000, "HND": 200000,
        "KIX": 200000, "ITM": 200000,
        "FUK": 160000,
        "CTS": 300000, "OKA": 300000,
        "NGO": 250000,
        "FSZ": 250000, "HKD": 250000,
        "TAK": 250000, "MYJ": 250000,
    },
}

CITY_NAMES = {
    "NRT": "도쿄 나리타", "HND": "도쿄 하네다",
    "KIX": "오사카 간사이", "ITM": "오사카 이타미",
    "FUK": "후쿠오카",     "CTS": "삿포로",
    "OKA": "오키나와",     "NGO": "나고야",
    "FSZ": "시즈오카",     "HKD": "하코다테",
    "TAK": "다카마쓰",     "MYJ": "마쓰야마",
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


# ── 데이터 ───────────────────────────────────────────────────

def load_data() -> dict:
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"config": DEFAULT_CONFIG.copy(), "sent_keys": [],
            "last_deals": [], "last_events": [], "last_checked": None}


def save_data(d: dict):
    DATA_FILE.write_text(
        json.dumps(d, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


# ── 알림 로그 ────────────────────────────────────────────────

def load_logs() -> list:
    if LOG_FILE.exists():
        try:
            return json.loads(LOG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def append_log(level: str, route: str, message: str, detail: str = ""):
    """level: 'success' | 'fail' | 'info' | 'skip'"""
    logs = load_logs()
    logs.append({
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "level": level,
        "route": route,
        "message": message,
        "detail": detail,
    })
    logs = logs[-300:]   # 최대 300건 유지
    LOG_FILE.write_text(
        json.dumps(logs, ensure_ascii=False, indent=2), encoding="utf-8")


# ── 일정 생성 ────────────────────────────────────────────────

HOLIDAYS = [
    ("2026-01-27", "2026-01-30"), ("2026-03-01", "2026-03-01"),
    ("2026-05-05", "2026-05-05"), ("2026-06-06", "2026-06-06"),
    ("2026-08-15", "2026-08-15"), ("2026-09-24", "2026-09-27"),
    ("2026-10-03", "2026-10-03"), ("2026-10-09", "2026-10-09"),
]


def get_trips(max_per_type=3):
    trips, today = [], date.today()
    holiday_dates = set()
    for s, e in HOLIDAYS:
        sd, ed = date.fromisoformat(s), date.fromisoformat(e)
        d = sd
        while d <= ed:
            holiday_dates.add(d); d += timedelta(days=1)

    seen, hcount = set(), 0
    for hday in sorted(holiday_dates):
        if hday < today or hcount >= max_per_type:
            continue
        for offset in range(-1, 2):
            dep, ret = hday + timedelta(days=offset), hday + timedelta(days=offset+3)
            if (dep, ret) in seen:
                continue
            if any((dep + timedelta(days=i)) in holiday_dates for i in range(4)):
                seen.add((dep, ret))
                trips.append((dep, ret, "연휴 3박4일"))
                hcount += 1; break

    start, cur, wcount = max(today, date(2026, 8, 1)), max(today, date(2026, 8, 1)), 0
    while cur <= today + timedelta(days=90) and wcount < max_per_type:
        wd = cur.weekday()
        if wd == 4:
            trips.append((cur, cur + timedelta(days=2), "금토일 2박3일")); wcount += 1
        elif wd == 5 and wcount < max_per_type:
            trips.append((cur, cur + timedelta(days=2), "토일월 2박3일")); wcount += 1
        cur += timedelta(days=1)
    return trips


# ── 가격 조회 ────────────────────────────────────────────────

def fetch_naver_price(dep, arr, dep_date, ret_date) -> Optional[int]:
    d1, d2 = dep_date.strftime("%Y%m%d"), ret_date.strftime("%Y%m%d")
    url = (f"https://flight.naver.com/flights/international/"
           f"{dep}-{arr}-{d1}/{arr}-{dep}-{d2}?adult=1")
    try:
        r = requests.get(url, headers=HEADERS, timeout=8)
        nums = re.findall(r'"totalPrice"\s*:\s*(\d+)', r.text)
        prices = [int(n) for n in nums if 50000 < int(n) < 2000000]
        return min(prices) if prices else None
    except requests.Timeout:
        raise TimeoutError("네이버항공 응답 시간 초과")
    except Exception as e:
        raise RuntimeError(str(e))


def build_naver_url(dep, arr, dep_date, ret_date):
    d1, d2 = dep_date.strftime("%Y%m%d"), ret_date.strftime("%Y%m%d")
    return (f"https://flight.naver.com/flights/international/"
            f"{dep}-{arr}-{d1}/{arr}-{dep}-{d2}?adult=1")


def build_skyscanner_url(dep, arr, dep_date, ret_date):
    d1, d2 = dep_date.strftime("%y%m%d"), ret_date.strftime("%y%m%d")
    return (f"https://www.skyscanner.co.kr/transport/flights/"
            f"{dep.lower()}/{arr.lower()}/{d1}/{d2}/?adults=1&currency=KRW")


def search_one_route(dep, arr, limit, trips) -> Optional[dict]:
    """노선 하나 검색 → 특가 있으면 deal dict 반환, 없으면 None"""
    best_price, best_trip = None, None
    for dep_date, ret_date, label in trips:
        if dep_date <= date.today():
            continue
        try:
            price = fetch_naver_price(dep, arr, dep_date, ret_date)
            time.sleep(0.5)
        except Exception:
            time.sleep(0.5)
            continue
        if price and (best_price is None or price < best_price):
            best_price, best_trip = price, (dep_date, ret_date, label)

    if best_price and best_trip and best_price <= limit:
        dep_date, ret_date, label = best_trip
        saving = round((1 - best_price / limit) * 100)
        return {
            "route": f"{dep} → {CITY_NAMES.get(arr, arr)}({arr})",
            "dep": dep, "arr": arr,
            "dep_date": dep_date.strftime("%Y.%m.%d"),
            "ret_date": ret_date.strftime("%Y.%m.%d"),
            "schedule": label,
            "price": best_price, "limit": limit, "saving": saving,
            "airline": "네이버항공",
            "url": build_naver_url(dep, arr, dep_date, ret_date),
            "skyscanner_url": build_skyscanner_url(dep, arr, dep_date, ret_date),
            "reason": f"기준가 {limit:,}원 대비 {saving}% 저렴",
        }
    return None


# ── SSE 스트리밍 검색 (프론트엔드용) ─────────────────────────

def search_all_routes_stream(config: dict):
    trips = get_trips(max_per_type=3)
    routes = list(config["price_limits"].items())
    total = len(routes) * 2
    done, found_deals = 0, []

    def sse(obj):
        return f"data: {json.dumps(obj, ensure_ascii=False, default=str)}\n\n"

    for arr, limit in routes:
        city = CITY_NAMES.get(arr, arr)
        for dep in ["ICN", "GMP"]:
            done += 1
            yield sse({"type": "progress", "current": done,
                       "total": total, "label": f"{dep} → {city} 검색 중..."})
            try:
                deal = search_one_route(dep, arr, limit, trips)
                if deal:
                    found_deals.append(deal)
                    yield sse({**deal, "type": "deal"})
            except Exception as e:
                log.warning(f"스트림 검색 오류 {dep}-{arr}: {e}")

    try:
        d = load_data()
        d["last_deals"] = found_deals
        d["last_checked"] = datetime.now().isoformat()
        save_data(d)
    except Exception:
        pass

    yield sse({"type": "done", "count": len(found_deals)})


# ── 모니터링 루프 (노선별 분할) ──────────────────────────────

def monitor_loop():
    append_log("info", "시스템", "모니터링 시작")
    while not stop_event.is_set():
        d = load_data()
        cfg = d["config"]

        if not cfg.get("alert_on"):
            stop_event.wait(60); continue

        if not cfg.get("telegram_token") or not cfg.get("telegram_chat_id"):
            append_log("fail", "시스템", "텔레그램 미설정", "토큰 또는 chat_id 없음")
            stop_event.wait(60); continue

        append_log("info", "시스템", "검색 사이클 시작",
                   datetime.now().strftime("%Y-%m-%d %H:%M"))

        trips = get_trips(max_per_type=3)
        sent = set(d.get("sent_keys", []))
        all_deals = []

        for arr, limit in cfg["price_limits"].items():
            if stop_event.is_set():
                break
            city = CITY_NAMES.get(arr, arr)
            for dep in ["ICN", "GMP"]:
                if stop_event.is_set():
                    break
                route_label = f"{dep}→{city}({arr})"
                try:
                    deal = search_one_route(dep, arr, limit, trips)
                    if deal:
                        all_deals.append(deal)
                        key = f"{deal['dep']}-{deal['arr']}-{deal['dep_date']}"
                        if key in sent:
                            append_log("skip", route_label,
                                       "특가 발견 (이미 알림 발송됨)",
                                       f"{deal['price']:,}원")
                        else:
                            ok = send_deal_alert(
                                cfg["telegram_token"], cfg["telegram_chat_id"], deal)
                            if ok:
                                sent.add(key)
                                append_log("success", route_label,
                                           "텔레그램 알림 발송 성공",
                                           f"{deal['price']:,}원 · {deal['schedule']}")
                            else:
                                append_log("fail", route_label,
                                           "텔레그램 알림 발송 실패",
                                           "API 응답 오류 — 토큰/chat_id 확인")
                    else:
                        append_log("info", route_label, "특가 없음",
                                   f"기준가 {limit:,}원 이하 항공권 미발견")
                except Exception as e:
                    append_log("fail", route_label, "검색 오류", str(e))

                # 노선 사이 짧은 대기 (서버 부하 방지)
                stop_event.wait(1)

        # 결과 저장
        d = load_data()
        d["last_deals"] = all_deals
        d["last_checked"] = datetime.now().isoformat()
        d["sent_keys"] = list(sent)[-200:]
        save_data(d)

        append_log("info", "시스템",
                   f"검색 사이클 완료 — 특가 {len(all_deals)}건",
                   f"다음 검색: {cfg.get('interval_minutes', 30)}분 후")

        stop_event.wait(cfg.get("interval_minutes", 30) * 60)

    append_log("info", "시스템", "모니터링 중지")


def start_monitor():
    global monitor_thread, stop_event
    stop_event.clear()
    monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
    monitor_thread.start()


# ── 텔레그램 ─────────────────────────────────────────────────

def send_telegram(token: str, chat_id: str, text: str) -> bool:
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text,
                  "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=10,
        )
        return r.status_code == 200
    except Exception:
        return False


def send_deal_alert(token, chat_id, deal) -> bool:
    msg = (
        f"✈️ <b>일본 항공권 특가!</b>\n\n"
        f"🛫 <b>노선</b>: {deal['route']} 왕복\n"
        f"📅 <b>일정</b>: {deal['dep_date']} ~ {deal['ret_date']} ({deal['schedule']})\n"
        f"💴 <b>가격</b>: <b>{deal['price']:,}원</b> (기준가 대비 {deal['saving']}% ↓)\n"
        f"💡 {deal['reason']}\n"
        f"🔗 <a href=\"{deal['url']}\">네이버항공 예약</a>"
    )
    return send_telegram(token, chat_id, msg)


# ── 이벤트 검색 ──────────────────────────────────────────────

def check_events() -> list:
    results = []
    japan_kw = ["일본","도쿄","오사카","후쿠오카","삿포로","오키나와","나고야"]
    deal_kw  = ["특가","세일","프로모션","이벤트","할인"]
    for cfg in AIRLINE_EVENTS:
        try:
            r = requests.get(cfg["url"], headers=HEADERS, timeout=8)
            soup = BeautifulSoup(r.text, "html.parser")
            seen = set()
            for tag in soup.find_all(["a","div","li","h3"], limit=100):
                text = tag.get_text(" ", strip=True)
                if len(text) < 6 or len(text) > 150: continue
                if not any(k in text for k in deal_kw): continue
                if not any(k in text for k in japan_kw): continue
                key = text[:30]
                if key in seen: continue
                seen.add(key)
                href = tag.get("href","") if tag.name == "a" else ""
                if href and not href.startswith("http"):
                    from urllib.parse import urljoin
                    href = urljoin(cfg["url"], href)
                results.append({"airline": cfg["name"], "code": cfg["code"],
                                 "name": text[:80], "url": href or cfg["url"]})
                if len(results) >= 20: break
        except Exception as e:
            log.warning(f"{cfg['name']} 스크래핑 실패: {e}")
        time.sleep(0.5)
    return results


# ── Flask 라우트 ─────────────────────────────────────────────

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
        stop_event.set(); time.sleep(0.3); start_monitor()
    else:
        stop_event.set()
        append_log("info", "시스템", "모니터링 OFF")
    return jsonify({"ok": True})


@app.route("/api/search/deals/stream")
def api_search_deals_stream():
    d = load_data()
    return Response(
        search_all_routes_stream(d["config"]),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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
    return jsonify({"deals": d.get("last_deals", []),
                    "events": d.get("last_events", []),
                    "last_checked": d.get("last_checked")})


@app.route("/api/logs", methods=["GET"])
def api_logs():
    logs = load_logs()
    return jsonify({"logs": list(reversed(logs))})   # 최신순


@app.route("/api/logs/clear", methods=["POST"])
def api_logs_clear():
    LOG_FILE.write_text("[]", encoding="utf-8")
    return jsonify({"ok": True})


@app.route("/api/telegram/test", methods=["POST"])
def api_test_telegram():
    body = request.json
    token, chat_id = body.get("token",""), body.get("chat_id","")
    if not token or not chat_id:
        return jsonify({"ok": False, "error": "토큰과 chat_id를 입력하세요"})
    ok = send_telegram(token, chat_id,
                       "✅ <b>연결 테스트 성공!</b>\n일본 특가 알리미가 정상 연결됐습니다.")
    append_log("success" if ok else "fail", "시스템",
               "텔레그램 테스트 " + ("성공" if ok else "실패"),
               f"chat_id: {chat_id}")
    return jsonify({"ok": ok, "error": "" if ok else "전송 실패. 토큰/chat_id 확인 필요"})


@app.route("/api/telegram/chatid", methods=["POST"])
def api_get_chatid():
    token = (request.json or {}).get("token","")
    if not token:
        return jsonify({"ok": False, "error": "토큰을 입력하세요"})
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=10)
        results = r.json().get("result", [])
        if not results:
            return jsonify({"ok": False, "error": "봇에게 /start 메시지를 먼저 보내세요"})
        chat = results[-1].get("message", {}).get("chat", {})
        return jsonify({"ok": True, "chat_id": str(chat.get("id","")),
                        "name": chat.get("first_name","") or chat.get("title","")})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


if __name__ == "__main__":
    start_monitor()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
