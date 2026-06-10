# SpaceX 뉴스 텔레그램 봇

매일 SpaceX 관련 뉴스를 NewsAPI에서 수집해 Claude로 한국어 요약하고
텔레그램 채널 `@stayhungry_asi` 로 발송하는 봇입니다.

## 동작 개요

1. NewsAPI에서 `SpaceX OR Starship OR Starlink OR Falcon` 키워드로
   지난 24시간 영문 뉴스 수집 (관련도 정렬, 중복 제목 제거)
2. 상위 5건을 Claude(`claude-sonnet-4-6`)로 한국어 제목 + 3줄 요약 생성
3. HTML 포맷으로 정리해 텔레그램 채널에 발송 (4096자 초과 시 자동 분할)

## 초기 설정

### 1. API 키 준비

`.env.example` 을 `.env` 로 복사한 뒤 4개 값 채우기.

| 키 | 발급처 |
|---|---|
| `NEWS_API_KEY` | https://newsapi.org/ |
| `ANTHROPIC_API_KEY` | https://console.anthropic.com/ |
| `TELEGRAM_BOT_TOKEN` | 텔레그램 `@BotFather` 에서 봇 생성 후 발급 |
| `TELEGRAM_CHANNEL` | 기본값 `@stayhungry_asi` (봇을 채널 관리자로 추가 필요) |

### 2. venv 생성 및 의존성 설치

PowerShell 또는 cmd에서:

```
cd C:\Users\Home\spacex-news-bot
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

### 3. 수동 테스트

```
.venv\Scripts\python main.py
```

성공 시 텔레그램 채널에 브리핑이 도착합니다.

## Windows 작업 스케줄러 등록 (매일 KST 08:00)

1. `Win + R` → `taskschd.msc` 실행
2. 우측 패널 **작업 만들기** (Create Task — *Create Basic Task* 아님)
3. **일반** 탭
   - 이름: `SpaceX News Bot`
   - **사용자가 로그온할 때만 실행** 또는 **로그온 여부에 관계없이 실행** 선택
   - **가장 높은 수준의 권한으로 실행** 체크 권장
4. **트리거** 탭 → **새로 만들기**
   - 작업 시작: `매일`
   - 시작 시각: `08:00:00` (한국 시간 기준 PC인 경우)
   - 활성화 체크
5. **동작** 탭 → **새로 만들기**
   - 동작: `프로그램 시작`
   - 프로그램/스크립트: `C:\Users\Home\spacex-news-bot\run.bat`
   - 시작 위치: `C:\Users\Home\spacex-news-bot`
6. **조건** 탭
   - `AC 전원에서만 실행` 체크 해제 권장 (노트북인 경우)
7. **설정** 탭
   - `예약된 시작 시간을 놓치면 가능한 한 빨리 작업 시작` 체크 권장

등록 후 작업을 우클릭 → **실행** 으로 즉시 테스트해볼 수 있습니다.
실행 로그는 `run.log` 에 누적됩니다.

## 파일 구조

```
spacex-news-bot/
├── .env              ← 직접 생성 (커밋 금지)
├── .env.example
├── .gitignore
├── main.py           ← 메인 스크립트
├── requirements.txt
├── run.bat           ← 작업 스케줄러용 진입점
└── README.md
```
