# Fly.io 배포 안내

봇은 심사가 끝날 때까지 꺼지면 안 됩니다. 지난 대회는 집 PC에서 돌려 전원 · 네트워크 · 권한 문제를 직접 막아야 했습니다. 이번에는 Fly.io 머신 1대에서 상시로 돌립니다.

> 이 문서는 준비만 합니다. 로그인 · 결제 · 배포는 사람이 직접 합니다.

## 무엇이 준비돼 있나

| 항목 | 어디 | 내용 |
| --- | --- | --- |
| 이미지 | `Dockerfile` | `app/`, `kb/`, `rules.yaml`까지 넣음 (봇이 실행 중에 읽음). 대회 자료는 넣지 않고 볼륨에 올림 |
| 상태 확인 | `app/health.py`, `GET :8080/health` | 게이트웨이 연결 상태, 마지막 메시지 · heartbeat 시각, kb 버전. 2분 넘게 끊기면 503 |
| 감시 | `WATCHDOG_SECONDS`(기본 600) | 게이트웨이가 10분 넘게 끊기면 프로세스를 끝냄 → 재시작 정책이 다시 띄움 |
| 재시작 | `fly.toml [[restart]] policy = "always"` | 프로세스가 죽으면 다시 띄움 |
| 멈추지 않기 | `fly.toml`에 `[http_service]` 없음 | auto stop은 http_service에만 걸리므로 요청이 없어도 멈추지 않음. 머신은 1대만 만든다 |
| 데이터 | 볼륨 `bot_data` → `/data` | 대화 기록 DB · 로그 · `knowledge/` · kb 카드(`kb/current`) · 대회용 `rules.yaml` |

## 비용 · 크기 어림

- 머신: shared-cpu-1x, 메모리 512MB, 1대. 봇은 대부분 LLM · 액션 API를 기다리는 일이라 이 크기로 충분합니다. 지난 대회 PC에서 메모리는 수백 MB 안쪽이었습니다.
- 볼륨: 1GB면 충분합니다(대화 DB · 로그 · 카드).
- 비용은 머신 상시 1대 + 볼륨 1GB로 **월 몇 달러 수준으로 어림**합니다. 대회 기간만 켜 두면 더 적습니다. 정확한 금액은 배포 전에 Fly 가격표(Pricing)로 확인하세요.
- 지역: `nrt`(도쿄). 카카오 게이트웨이와 가까운 지역으로 고릅니다.

## 비밀값 (`fly secrets`로만, 파일 · 저장소에 넣지 않음)

| 이름 | 무엇 |
| --- | --- |
| `BOT_TOKEN` | 카카오 봇 게이트웨이 토큰 |
| `LLM_API_KEY` | Gemini 키(유료) |
| `LLM_API_KEY_FALLBACK` | 예비 Gemini 키 (주 키가 결제 오류를 내면 전환) |
| `ACTION_TOKEN` | 대회 액션 API 토큰 |

비밀이 아닌 설정은 `fly.toml`의 `[env]`나 `fly secrets`에 둡니다: `ACTION_BASE_URL`, `LLM_MODEL`(모델 이름을 고정할 것), `USE_EXPLICIT_CACHE=true`(유료 키), `ESCALATION_*`(대회 이관 코드), `RULES_FILE=/data/rules.yaml`.

## 처음 배포 순서

```powershell
# 0. flyctl 설치 · 로그인 (사람이 직접)
fly auth login

# 1. 앱 만들기 (배포하지 않음). 앱 이름을 정하면 fly.toml의 app 값을 그 이름으로 바꾼다
fly launch --no-deploy --copy-config

# 2. 볼륨 만들기 (fly.toml의 [mounts] source와 같은 이름, 지역도 같게)
fly volumes create bot_data --region nrt --size 1

# 3. 비밀값 넣기 (값은 화면 · 기록에 남지 않게 직접 입력)
fly secrets set BOT_TOKEN=... LLM_API_KEY=... LLM_API_KEY_FALLBACK=... ACTION_TOKEN=...

# 4. 배포
fly deploy

# 5. 머신이 1대인지, 상태 확인이 통과하는지
fly status
fly checks list
fly logs
```

상태 확인을 직접 보려면 `fly ssh console -C "python -c \"import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8080/health').read().decode())\""`.

## 대회 자료 올리기 (볼륨 `/data`)

이미지에는 대회 자료가 없습니다. 볼륨에 올립니다.

| 볼륨 경로 | 무엇 |
| --- | --- |
| `/data/knowledge/` | 손으로 정리한 knowledge 문서 · 액션 API 명세(`openapi.json`) |
| `/data/kb/current/` | `build_kb.py`가 공개한 카드 (`kb.txt`, `cards.json`, `conflicts.json`, `version.json`) |
| `/data/rules.yaml` | 대회용 규칙 사본 (올린 뒤 `RULES_FILE=/data/rules.yaml`) |

```powershell
# sftp로 파일 올리기 (예: 카드)
fly ssh sftp shell
# sftp 안에서: put knowledge\kb\current\cards.json /data/kb/current/cards.json  (kb.txt, conflicts.json도)
#              version.json은 맨 마지막에 올린다 → 봇이 version.json이 바뀐 걸 보고 다음 메시지부터 새 카드를 씀
```

카드 갱신은 재시작이 필요 없습니다(`version.json`을 마지막에 올리는 것만 지키면 됨). `rules.yaml`도 바뀌면 다음 메시지부터 반영됩니다.

## 대회 당일 확인할 것

- [ ] 대회 전날 리허설: 배포 → 카톡으로 "안녕하세요" → 답이 오는지, `fly logs`에 "게이트웨이 연결 성공"이 있는지
- [ ] `fly status`에 머신이 1대만 있는지 (2대면 같은 메시지에 답이 두 번 갈 수 있음)
- [ ] `/health`의 `kb.builtAt`이 방금 올린 카드인지
- [ ] Gemini 유료 키 잔액, 예비 키
- [ ] 로컬 PC 실행(`run_bot.ps1`)은 예비로만. 클라우드와 동시에 켜면 답이 두 번 감
- [ ] 심사가 끝나면 `fly scale count 0` 또는 앱 삭제로 비용을 멈춤

## 확인이 필요한 것 (배포 전에)

- `fly.toml`의 `[[restart]]` · `[checks]` 문법은 Fly 문서의 현재 형식과 맞는지 `fly config validate`로 확인하세요. 이 저장소에서는 배포를 해 보지 않았습니다.
- 카카오 게이트웨이가 클라우드 IP에서 접속을 막지 않는지는 리허설에서만 확인할 수 있습니다.
