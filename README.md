# valet-bot

아마노 주차대행 예약 자동화 프로토타입입니다.

## 로컬 실행 (macOS)

```bash
cd /Users/park-yanghee/Documents/codexProjects/valet-bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
python run.py
```

브라우저에서 `http://localhost:8000` 접속 후 설정을 수정합니다.

초기 설정:

```bash
cp config.example.yaml config.yaml
```

## Docker 실행

```bash
cp config.example.yaml config.yaml
docker compose build
docker compose up -d
```

접속: `http://<SERVER_IP>:8000`

## 현재 동작

- 설정파일(`config.yaml`) 기반
- 대시보드에서 입력값/시작시각/종료시각/간격 관리
- 다중 예약자 큐(`queue`)를 켜면 성공 후 다음 예약자로 자동 전환
- 큐 현재 예약자를 대시보드에서 이전/다음/초기화 수동 제어 가능
- 지정 시간 창에서 30초 간격(설정값)으로 예약 시도
- 날짜 선택 실패 시 `date_not_open`으로 기록
- 성공 추정 시 Discord Webhook 발송
- 예약 성공 후 `booking-list` 조회로 검증(예약현황 표시)
- 로그 행에서 `예약확인`/`예약취소` 버튼으로 수동 확인 및 취소 가능
- 시도마다 스크린샷 저장 및 대시보드 조회

## 주의

- 대상 사이트 DOM 구조가 바뀌면 셀렉터 수정이 필요합니다.
- 실제 성공 판정 키워드는 첫 성공 사례 후 보정하는 것을 권장합니다.
