"""
Japan Flight Deal Alert — Flask Web App v4
- 검색기간: 현재 +10개월
- 도착지별 개별 검색 + 전체 비동기 병렬 검색
- 설정탭: 이메일 인증코드 보호
"""
import json, logging, os, re, secrets, smtplib, threading, time
from datetime import date, datetime, timedelta
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup
from flask import Flask, Response, jsonify, render_template, request, session

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

DATA_FILE = Path("/tmp/data.json")
LOG_FILE  = Path("/tmp/alert_log.json")

# 관리자 비밀번호 — Render 환경변수 ADMIN_PASSWORD 로 설정
# 미설정 시 기본값 "stardew1234" 사용
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "stardew1234")

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

# 도착지 그룹 (탭 구성용)
DEST_GROUPS = [
    {"id": "tokyo",    "label": "도쿄",   "codes": ["NRT", "HND"]},
    {"id": "osaka",    "label": "오사카",  "codes": ["KIX", "ITM"]},
    {"id": "fukuoka",  "label": "후쿠오카","codes": ["FUK"]},
    {"id": "sapporo",  "label": "삿포로",  "codes": ["CTS"]},
    {"id": "okinawa",  "label": "오키나와","codes": ["OKA"]},
    {"id": "nagoya",   "label": "나고야",  "codes": ["NGO"]},
    {"id": "other",    "label": "소도시",  "codes": ["FSZ","HKD","TAK","MYJ"]},
]

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
    DATA_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


# ── 로그 ────────────────────────────────────────────────────

def load_logs() -> list:
    if LOG_FILE.exists():
        try:
            return json.loads(LOG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []

def append_log(level: str, route: str, message: str, detail: str = ""):
    logs = load_logs()
    logs.append({"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                 "level": level, "route": route, "message": message, "detail": detail})
    LOG_FILE.write_text(json.dumps(logs[-300:], ensure_ascii=False, indent=2), encoding="utf-8")


# ── 이메일 인증 ──────────────────────────────────────────────

def verify_password(pw: str) -> bool:
    return pw.strip() == ADMIN_PASSWORD


def verify_auth_code(email: str, code: str) -> bool:
    return verify_password(code)


def send_deal_alert(token, chat_id, deal) -> bool:
    msg = (f"✈️ <b>일본 항공권 특가!</b>\n\n"
           f"🛫 <b>노선</b>: {deal['route']} 왕복\n"
           f"📅 <b>일정</b>: {deal['dep_date']} ~ {deal['ret_date']} ({deal['schedule']})\n"
           f"💴 <b>가격</b>: <b>{deal['price']:,}원</b> (기준가 대비 {deal['saving']}% ↓)\n"
           f"💡 {deal['reason']}\n"
           f"🔗 <a href=\"{deal['url']}\">네이버항공 예약</a>")
    return send_telegram(token, chat_id, msg)


# ── 모니터링 루프 ────────────────────────────────────────────

def monitor_loop():
    append_log("info","시스템","모니터링 시작")
    while not stop_event.is_set():
        d = load_data(); cfg = d["config"]
        if not cfg.get("alert_on"):
            stop_event.wait(60); continue
        if not cfg.get("telegram_token") or not cfg.get("telegram_chat_id"):
            append_log("fail","시스템","텔레그램 미설정","토큰 또는 chat_id 없음")
            stop_event.wait(60); continue

        append_log("info","시스템","검색 사이클 시작", datetime.now().strftime("%Y-%m-%d %H:%M"))
        trips = get_trips(max_per_type=5)
        sent = set(d.get("sent_keys",[]))
        all_deals = []

        for arr, limit in cfg["price_limits"].items():
            if stop_event.is_set(): break
            city = CITY_NAMES.get(arr, arr)
            for dep in ["ICN","GMP"]:
                if stop_event.is_set(): break
                route_label = f"{dep}→{city}({arr})"
                try:
                    deal = search_one_route(dep, arr, limit, trips)
                    if deal:
                        all_deals.append(deal)
                        key = f"{deal['dep']}-{deal['arr']}-{deal['dep_date']}"
                        if key in sent:
                            append_log("skip", route_label, "특가 발견 (이미 발송됨)", f"{deal['price']:,}원")
                        else:
                            ok = send_deal_alert(cfg["telegram_token"], cfg["telegram_chat_id"], deal)
                            if ok:
                                sent.add(key)
                                append_log("success", route_label, "텔레그램 알림 발송 성공",
                                           f"{deal['price']:,}원 · {deal['schedule']}")
                            else:
                                append_log("fail", route_label, "텔레그램 알림 발송 실패",
                                           "API 응답 오류 — 토큰/chat_id 확인")
                    else:
                        append_log("info", route_label, "특가 없음", f"기준가 {limit:,}원 이하 미발견")
                except Exception as e:
                    append_log("fail", route_label, "검색 오류", str(e))
                stop_event.wait(1)

        d = load_data()
        d["last_deals"] = all_deals
        d["last_checked"] = datetime.now().isoformat()
        d["sent_keys"] = list(sent)[-200:]
        save_data(d)
        append_log("info","시스템",f"사이클 완료 — 특가 {len(all_deals)}건",
                   f"다음 검색: {cfg.get('interval_minutes',30)}분 후")
        stop_event.wait(cfg.get("interval_minutes",30) * 60)
    append_log("info","시스템","모니터링 중지")

def start_monitor():
    global monitor_thread, stop_event
    stop_event.clear()
    monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
    monitor_thread.start()


# ── 인증 미들웨어 ────────────────────────────────────────────

def is_admin():
    return session.get("admin_verified") is True


# ── Flask 라우트 ─────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html",
                           dest_groups=json.dumps(DEST_GROUPS, ensure_ascii=False))

@app.route("/api/config", methods=["GET"])
def get_config():
    if not is_admin():
        return jsonify({"ok": False, "error": "인증 필요"}), 403
    d = load_data(); cfg = d["config"].copy()
    if cfg.get("telegram_token"):
        cfg["telegram_token_masked"] = cfg["telegram_token"][:8] + "••••••••"
    return jsonify(cfg)

@app.route("/api/config", methods=["POST"])
def set_config():
    if not is_admin():
        return jsonify({"ok": False, "error": "인증 필요"}), 403
    d = load_data(); body = request.json
    d["config"].update({k: v for k, v in body.items() if k in DEFAULT_CONFIG})
    save_data(d)
    if d["config"].get("alert_on"):
        stop_event.set(); time.sleep(0.3); start_monitor()
    else:
        stop_event.set(); append_log("info","시스템","모니터링 OFF")
    return jsonify({"ok": True})

# 인증 API
@app.route("/api/auth/send", methods=["POST"])
def auth_send():
    # 비밀번호 방식 — 프론트에서 바로 verify로 가도록 ok 반환
    return jsonify({"ok": True, "message": "비밀번호를 입력하세요", "mode": "password"})


@app.route("/api/auth/verify", methods=["POST"])
def auth_verify():
    body = request.json or {}
    pw = body.get("code", "").strip()
    if verify_password(pw):
        session["admin_verified"] = True
        session.permanent = True
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "비밀번호가 틀렸습니다"})


@app.route("/api/auth/status", methods=["GET"])
def auth_status():
    return jsonify({"verified": is_admin()})

@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    session.pop("admin_verified", None)
    return jsonify({"ok": True})

# 검색 API
@app.route("/api/search/group/<group_id>")
def api_search_group(group_id):
    d = load_data(); cfg = d["config"]
    group = next((g for g in DEST_GROUPS if g["id"] == group_id), None)
    if not group:
        return jsonify({"error": "그룹 없음"}), 404
    return Response(stream_group(group["codes"], cfg),
                    mimetype="text/event-stream",
                    headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

@app.route("/api/search/all")
def api_search_all():
    d = load_data(); cfg = d["config"]
    return Response(stream_all_parallel(cfg),
                    mimetype="text/event-stream",
                    headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

@app.route("/api/search/events", methods=["POST"])
def api_search_events():
    d = load_data()
    try:
        events = check_events()
        d["last_events"] = events; save_data(d)
        return jsonify({"ok": True, "events": events, "count": len(events)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/last", methods=["GET"])
def api_last():
    d = load_data()
    return jsonify({"deals": d.get("last_deals",[]),
                    "events": d.get("last_events",[]),
                    "last_checked": d.get("last_checked")})

@app.route("/api/logs", methods=["GET"])
def api_logs():
    return jsonify({"logs": list(reversed(load_logs()))})

@app.route("/api/logs/clear", methods=["POST"])
def api_logs_clear():
    if not is_admin():
        return jsonify({"ok": False, "error": "인증 필요"}), 403
    LOG_FILE.write_text("[]", encoding="utf-8")
    return jsonify({"ok": True})

@app.route("/api/telegram/test", methods=["POST"])
def api_test_telegram():
    if not is_admin():
        return jsonify({"ok": False, "error": "인증 필요"}), 403
    body = request.json; token, chat_id = body.get("token",""), body.get("chat_id","")
    if not token or not chat_id:
        return jsonify({"ok": False, "error": "토큰과 chat_id를 입력하세요"})
    ok = send_telegram(token, chat_id, "✅ <b>연결 테스트 성공!</b>\n일본 특가 알리미가 정상 연결됐습니다.")
    append_log("success" if ok else "fail","시스템","텔레그램 테스트 "+("성공" if ok else "실패"), f"chat_id: {chat_id}")
    return jsonify({"ok": ok, "error": "" if ok else "전송 실패"})

@app.route("/api/telegram/chatid", methods=["POST"])
def api_get_chatid():
    if not is_admin():
        return jsonify({"ok": False, "error": "인증 필요"}), 403
    token = (request.json or {}).get("token","")
    if not token:
        return jsonify({"ok": False, "error": "토큰을 입력하세요"})
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=10)
        results = r.json().get("result",[])
        if not results:
            return jsonify({"ok": False, "error": "봇에게 /start 메시지를 먼저 보내세요"})
        chat = results[-1].get("message",{}).get("chat",{})
        return jsonify({"ok": True, "chat_id": str(chat.get("id","")),
                        "name": chat.get("first_name","") or chat.get("title","")})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

if __name__ == "__main__":
    start_monitor()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
