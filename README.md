# ✈️ 일본 특가 알리미 — 웹앱

브라우저에서 바로 열리는 진짜 웹앱입니다.
Railway에 올리면 PC 없이 24시간 작동합니다.

---

## 로컬에서 바로 실행 (테스트용)

```bash
pip install -r requirements.txt
python app.py
```
브라우저에서 http://localhost:5000 접속

---

## Railway 무료 배포 (추천 — PC 안 켜도 됨)

### 1단계 — GitHub에 올리기
1. https://github.com 가입
2. New repository → 이름: `japan-flight-alert`
3. 이 폴더 안 파일 전부 업로드

### 2단계 — Railway 배포
1. https://railway.app 가입 (GitHub 계정으로 로그인)
2. **New Project** → **Deploy from GitHub repo**
3. `japan-flight-alert` 선택
4. 자동으로 빌드 & 배포 시작 (2~3분)
5. **Settings → Domains → Generate Domain** 클릭
6. 생성된 URL로 어디서든 접속 가능!

### 3단계 — 웹앱에서 설정
1. 브라우저로 Railway URL 접속
2. **설정 탭** → 봇 토큰 · chat_id 입력
3. **테스트 메시지 발송** 확인
4. **설정 저장**
5. 상단 **알림 ON** 토글 → 모니터링 시작

---

## 파일 구조

```
flight_webapp/
├── app.py              ← Flask 백엔드 (API + 모니터링)
├── requirements.txt    ← 패키지 목록
├── Procfile            ← Railway 실행 설정
├── data.json           ← 설정·검색결과 저장 (자동 생성)
└── templates/
    └── index.html      ← 웹앱 프론트엔드
```

---

## Railway 무료 플랜 한도

- 월 $5 크레딧 제공 (무료)
- 소형 앱은 한 달 내내 무료로 운영 가능
- 잠자기(sleep) 없음 — 24시간 상시 작동
