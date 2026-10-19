# AI Serving Backend — 작업 요약 자료

> 작성일: 2026-05-29 · 대상 커밋: `a806181` (origin/main 동기화 완료)
> 이 문서는 **지금까지 수행한 작업의 기록(report)**입니다. "이어서 작업"용 안내는 [`HANDOFF.md`](HANDOFF.md), 최초 설계는 [`PLAN.md`](PLAN.md) 참고.

---

## 1. 프로젝트 개요

| 항목 | 내용 |
|------|------|
| **무엇** | 금융권용 **AI Gateway MVP** — OpenAI 호환(`/v1/chat/completions`) FastAPI 백엔드 |
| **목적** | 사내 LLM 사용을 단일 게이트웨이로 통제: 비용·토큰 추적, 멀티테넌트 접근통제, **PII 마스킹·금지표현 필터·감사 로그** 등 컴플라이언스 일원화 |
| **왜 지금** | 기업 AI 지출 급증 → 비용 가시성 + 금융권 규제(개인정보·감사추적) 대응이 동시에 필요. 각 팀이 provider를 직접 호출하는 대신 게이트웨이를 경유시켜 통제점을 확보 |
| **기간** | 2026-05-28 ~ 2026-05-29 (커밋 21개) |
| **스택** | Python 3.13, FastAPI, async SQLAlchemy(+aiosqlite/PostgreSQL), PyJWT[crypto], Redis(Lua), Docker/Compose |
| **규모** | 앱 코드 **3,939줄 / 34모듈**, 테스트 **4,394줄 / 136건**, 스크립트 442줄 |
| **현재 상태** | Phase 1 완료 + Phase 2 대부분 완료. **136/136 테스트 그린(플레이키 0)**, main 푸시 완료 |

---

## 2. 아키텍처

```
                    ┌─────────────────────────── 미들웨어 스택 ───────────────────────────┐
Client ── HTTP ──►  RequestID/관측성 → BodySizeLimit → (pre-auth IP limiter)
                                                              │
                                                              ▼
                        Auth(API key → JWT/OIDC) → RateLimit(Redis 토큰버킷)
                                                              │
                                                              ▼
                     ┌──────────────── /v1/chat/completions ────────────────┐
                     │  금지표현 필터(정책모드) → PII 마스킹 → Provider 라우팅 │
                     └───────────────────────────────────────────────────────┘
                                                              │
                              ┌───────────────────────────────┼───────────────┐
                              ▼                                ▼               ▼
                        OpenAI provider                Anthropic provider   (stream/non-stream 공통)
                              │                                │
                              ▼                                ▼
                        External API ── SSE/JSON ──► 응답
                                                              │
                  (응답 본문 완료 후) ──► 감사 로그 + 비용 + policy_event 단일 트랜잭션 기록
                                          DB 실패 시 JSONL 폴백 → /admin/audit/replay
```

핵심 원칙: **인증·정책·PII·감사는 스트리밍/논스트리밍 공통 경로**에서 처리하여 우회 불가.

---

## 3. 구현 기능

### 3.1 코어 게이트웨이
- OpenAI 호환 `/v1/chat/completions` (스트리밍 SSE + 논스트리밍), `/v1/models`, `/health`
- Provider 추상화(`AIProvider`) + 모델 기반 라우팅(`gpt-*`→OpenAI, `claude-*`→Anthropic)
- 정규화 계층(`NormalizedChatRequest/Response/StreamChunk`)으로 provider 차이 흡수
- 파일: `app/routers/chat.py`, `app/providers/`, `app/normalized.py`, `app/schemas.py`

### 3.2 인증/인가 — API key + JWT/OIDC 병행
- **API key**: `Authorization: Bearer sk-...`, DB 저장은 prefix+SHA-256(평문 미저장, 생성 시 1회만 노출). env 키는 부트스트랩 super_admin.
- **JWT/OIDC** (Stage 1+2): JWKS/kid 서명 + iss/aud/exp + 허용 alg 화이트리스트(RS256). alg confusion/none 차단. claims→scope/org 매핑, **기존 Org 조회만**(자동생성 X). audit엔 원문 JWT 미저장(`sha256("jwt:{iss}:{sub}")`).
- `AUTH_MODE`(기본 `api_key`, CSV로 `jwt` 추가) — 인증 순서 API key → JWT.
- scope: `chat` / `admin` / `super_admin`. 파일: `app/auth.py`, `app/auth_jwt.py`

### 3.3 멀티테넌시 & Admin
- Org/User/APIKey 데이터 모델. `/admin/*`: orgs·keys 발급/조회/폐기, usage 롤업, audit 페이지네이션, audit replay.
- **테넌트 격리**: non-super admin은 자기 org만 조회/관리. 파일: `app/routers/admin.py`, `app/db/models.py`

### 3.4 레이트리밋
- Redis Lua **원자적 토큰버킷**(다중 워커 일관), 메모리 폴백. `RATE_LIMIT_STRICT=true`면 Redis 실패 시 startup 실패.
- 스트림 동시성 제한기(per-principal lease), pre-auth IP 리미터(공유 백엔드). 파일: `app/ratelimit/`, `app/streaming.py`

### 3.5 감사 로그 & 비용 추적
- 요청별 (request_id, principal_hash, org/key, provider, model, tokens, **cost_usd**, latency_ms, status, error_type, stream) 기록.
- **Durability**: `AUDIT_SYNC=true`면 commit까지 await. DB 실패 시 JSONL 폴백 → super_admin `POST /admin/audit/replay`로 재주입(원자적 스냅샷 + `.failed` 격리). 파일: `app/observability.py`, `app/audit_fallback.py`, `app/pricing.py`

### 3.6 컴플라이언스 (Phase 2 핵심)
- **PII 마스킹** (`app/compliance/pii.py`): 주민번호(생년월일+성별자리 **실제 달력 검증**)·카드(Luhn)·전화·이메일을 모델 전송 **전** 비가역 마스킹. 스팬 기반 비중복, 동일 값=동일 토큰(`[REDACTED:TYPE:n]`, 코어퍼런스 유지). 원문은 로그/응답/DB **어디에도 미저장**(카운트만).
- **금지표현 필터** (`app/compliance/filter.py`): `FORBIDDEN_PATTERNS`(CSV `rule_id=regex`)로 입력 검사, PII 마스킹 이전. 원문·캡처그룹 무저장(rule_id/count/severity만). ReDoS 완화(길이/개수 상한 + 컴파일 캐시).
- **정책 모드**: `POLICY_MODE` = `block`(provider 미호출 + 403) / `log_only`(통과+기록) / `disabled`.
- **영속화**: `policy_event` 테이블 + `AUDIT_STORE_MESSAGES=true`면 **마스킹된** 본문만 `audit_message`에 저장. audit 행과 단일 트랜잭션.

### 3.7 관측성 / 미들웨어
- request_id 부여 + 구조화 로그(`extra_fields` 재귀 secret sanitize). 스트리밍은 body_iterator 래핑으로 본문 완료 후 finalize.
- BodySizeLimit(청크 포함 413), 신뢰 프록시 기반 XFF rightmost-trust. 파일: `app/middleware.py`, `app/net.py`, `app/observability.py`

### 3.8 안정성 / 하드닝
- SQLite WAL + busy_timeout(파일 DB), 의존성 완전 고정, 줄바꿈 정규화(`.gitattributes`), last_used_at 디바운스.

---

## 4. 작업 타임라인 (마일스톤별)

| # | 마일스톤 | 커밋 | 핵심 내용 |
|---|----------|------|-----------|
| M0 | 시드 게이트웨이 | `285e466` | FastAPI 골격, provider 추상화, 기본 인증/레이트리밋 |
| M1 | **Phase 1 — 인프라·보안·감사** | `c7ece12` | 감사 로그+비용, Org/User/APIKey, Redis 레이트리밋, Admin, durability. HIGH 4 + MEDIUM 2 보안패치. 59/59 그린 |
| M2 | **Phase 2 — PII 마스킹** | `06e1323`, `2fae292` | RRN/카드/전화/이메일 비가역 마스킹, provider 전송 전 적용 |
| M3 | 리포 위생·재현성 | `29d6d80` | 의존성 완전 고정(pip freeze), `.gitattributes`, `pytest.ini`, README/HANDOFF 명령 일치 |
| M4 | **P1 — 키 없는 E2E 품질** | `2540c14` | fake provider로 PII가 payload에 실제 반영/audit·로그 무유출/스트림 패리티 검증(앱 변경 0) |
| M5 | **Phase 2 — 컴플라이언스 확장** | `d6c0d6f`, `0e05f34` | 금지표현 필터 + 정책모드(block/log_only/disabled) + policy_event/audit_message 영속화 |
| M6 | 코드 리뷰 수정 | `9bbdda6`, `8ea07e2` | MEDIUM 2(audit replay 데이터손실, 스트림 limiter 누수) + LOW/하드닝 5(ReDoS 상한, 컴파일 캐시, 디바운스, 로그스팸, SQLite WAL) |
| M7 | 실제 API smoke 하네스 | `84e6688` | `scripts/smoke_provider.py` — 기본 dry-run, `--run`만 실제 유료 호출. pytest 비수집 강제 |
| M8 | **JWT/OIDC 인증** | `87cf248` | API key와 병행하는 JWKS 기반 bearer 인증(Stage 1+2) |
| M9 | **랄프 루프 버그헌트(8R)** | `a90408e`~`b8189fb` | Codex 반복 버그헌트 → Claude 검토 → 회귀테스트. **16건 수정 / 1건 반려**. 102→136 테스트 |
| M10 | 문서화 | `a806181` | HANDOFF 갱신(8라운드, 136 테스트) |

---

## 5. 품질 검증

### 5.1 테스트 분포 (총 136건, 25파일)

| 영역 | 파일 | 건수 |
|------|------|------|
| JWT/OIDC 인증 | `test_jwt_auth.py` | 17 |
| Provider 정규화 | `test_providers.py` | 13 |
| PII 마스킹(단위 + E2E) | `test_pii.py` / `test_pii_e2e.py` | 10 + 9 |
| 보안 | `test_security.py` | 8 |
| 정책(필터/모드 + 영속화) | `test_policy.py` / `test_policy_persistence.py` | 8 + 6 |
| 하드닝(엣지케이스) | `test_hardening.py` | 7 |
| 에러 본문 | `test_errors.py` | 7 |
| 프록시 IP/XFF | `test_client_ip.py` | 7 |
| 인증/스코프 | `test_auth.py` / `test_scopes.py` | 7 + 1 |
| 감사(스트림/논스트림) | `test_audit.py` / `test_audit_fallback.py` | 5 + 3 |
| 채팅 흐름 | `test_chat.py` | 6 |
| 레이트리밋 | `test_ratelimit_redis.py` / `test_ratelimit_strict.py` | 5 + 2 |
| Admin 격리/플로우 | `test_admin_isolation.py` / `test_admin.py` | 5 + 1 |
| 스트리밍 | `test_streaming.py` | 3 |
| smoke 안전장치 | `test_smoke_harness_safety.py` | 2 |
| 기타(db_auth/models/health/observability) | 각 1 | 4 |

검증 명령: `.\.venv\Scripts\python.exe -m pytest -q` → **136 passed**.

### 5.2 랄프 루프 (8라운드, 버그 16건 수정)
Codex 반복 버그헌트 → Claude 보안·로직·사이드이펙트 검토 → 테스트 3회 독립 검증 → 라운드별 커밋. 발견 추세 **1,2,2,4,4,1,1,1**(수렴). 대표 수정:
- **[HIGH]** 스트리밍 audit 집계 오류 — 스트림은 HTTP 200 고정이라 실패가 "성공·0토큰"으로 잡히던 문제. body 완료 후 finalize + **논리적 status_code**(타임아웃 504/provider 502/예외 500) 분리 기록.
- **[HIGH]** scope 콤마 인젝션 권한상승 차단(`"admin,super_admin"`), 중첩 list 안 secret 로그 유출 차단.
- **[견고성]** 청크 본문 초과 413, invalid UTF-8 → 422, 빈 금지규칙 전체매칭 방지, RRN 실제 달력 검증, Anthropic usage/finish_reason None, OpenAI usage-only 청크 `choices:[]`, OAuth scope 공백구분, `/admin/audit` 페이지네이션 off-by-one.
- **반려 1건**: JWT `aud` 검증 약화 시도 → 스펙(audience 검증 필수)에 반해 **원복**.

### 5.3 실제 API smoke 하네스
`scripts/smoke_provider.py` — 게이트웨이 ASGI 전체 경로로 stream/non-stream 실호출 → 응답+usage/cost+audit_log 행까지 검증. 기본 dry-run, `--run`일 때만 유료 호출(max_tokens 상한 64). `pytest`는 절대 수집 안 함(`tests/test_smoke_harness_safety.py`로 강제).

---

## 6. 핵심 엔지니어링 결정 (의사결정 기록)

| 결정 | 이유 |
|------|------|
| **PII 비가역 마스킹**(detokenize 안 함) | 역매핑 저장 자체가 보안 표면이 됨. 응답 복원 포기하고 누출면 제거 우선 |
| **전화번호 과마스킹 허용 / 계좌번호 제외** | 금융권은 미탐(누출)보다 과탐(과마스킹)이 안전. 계좌번호는 형식 가변·오탐률 높아 범위 제외 |
| **정책·PII를 스트림 분기 이전** 공통 경로에 배치 | 스트리밍 우회로 통제 누락 방지 |
| **스트리밍 audit를 body 완료 후 finalize** | SSE는 헤더가 먼저 나가 200 고정 → 사후에 논리적 status/토큰을 정확히 기록 |
| **JWT는 API key와 병행, Org 자동생성 X** | 기존 동작 보존 + 무단 테넌트 생성 차단. 자동 프로비저닝(Stage 3)은 후속 |
| **JWT 검증 strict 유지**(aud 약화 반려) | 컴플라이언스 스펙이 audience 검증을 요구 |
| **모든 코딩 Codex 위임 + Claude 최종검토** | CLAUDE.md 워크플로우. 검토 단계에서 권한상승 버그 등 실제 차단 |

---

## 7. 현재 상태 & 남은 작업

### 완료
- ✅ Phase 1 전체(인프라·보안·감사·durability)
- ✅ Phase 2 대부분: PII 마스킹, 금지표현 필터, 정책 모드, policy_event/마스킹 본문 영속화
- ✅ JWT/OIDC 인증(Stage 1+2), 키 없는 E2E 품질 검증, 실제 API smoke 하네스
- ✅ 코드 리뷰(MEDIUM 2 + LOW 5) + 랄프 루프 8R(16건)

### 남은 작업
- **Phase 2 잔여**: 응답 근거 표시(citation) — RAG 의존이라 Phase 3와 함께.
- **Phase 3 — RAG**: 문서 인덱스(pgvector), 임베딩, retrieval→system 주입, citation 메타.
- **Phase 4 — Admin UI**: 사용량/비용 차트, 키 관리, audit 검색.
- **Phase 5 — 운영 강화**: **Alembic 마이그레이션 정식화(배포 전 필수)**, Postgres 통합 테스트, `docker compose up` 실부팅 검증, audit 로테이션/아카이빙, JWT 자동 프로비저닝(Stage 3).

### 알려진 LOW/INFO (운영 전 점검)
- `ALLOWED_HOSTS=*`, `DOCS_ENABLED=true` 기본값 → 운영 전 변경.
- Postgres 기본 PW `change-me` → 교체.
- `JWT_AUDIENCE` 미설정 시 PyJWT가 aud 값 검증 생략(존재만 요구) → 운영 시 필수 설정 권장.
- 단가표 하드코딩(`app/pricing.py`) → 외부 동기화 검토.

---

## 8. 빠른 참조

### 실행/검증
```powershell
cd C:\projects\ai-serving-backend
.\.venv\Scripts\python.exe -m pytest -q          # 전체 테스트 (136 passed)
.\.venv\Scripts\python.exe scripts\smoke_provider.py        # dry-run (호출 안 함)
.\.venv\Scripts\python.exe scripts\smoke_provider.py --run  # 실제 유료 호출(키 필요)
```

### 핵심 파일
| 역할 | 경로 |
|------|------|
| 진입점 | `app/main.py` |
| 라우터 | `app/routers/{chat,admin,health,models}.py` |
| 인증 | `app/auth.py`(API key) · `app/auth_jwt.py`(JWT/OIDC) |
| 컴플라이언스 | `app/compliance/pii.py` · `app/compliance/filter.py` |
| 정책/에러 | `app/routers/chat.py` · `app/errors.py`(PolicyViolationError) |
| 감사/관측성 | `app/observability.py` · `app/audit_fallback.py` · `app/db/models.py` |
| 레이트리밋 | `app/ratelimit/` · `app/streaming.py` |
| 설정 | `app/config.py` |

### 작업 워크플로우 (CLAUDE.md)
Claude 계획 → Codex 구현 → Codex 결과 검토 → **Claude 최종 검토(보안·로직·사이드이펙트)** → pytest 다회 실행으로 플레이키 확인.
