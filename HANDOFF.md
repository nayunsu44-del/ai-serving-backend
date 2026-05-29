# AI Serving Backend — Session Handoff

마지막 작업: 2026-05-29

## 한 줄 요약

금융권용 AI Gateway MVP. Phase 1(기반 인프라) + 보안 패치 완료. Phase 2 대부분 완료: PII 마스킹 + 금지표현 필터 + 정책 모드(block/log_only/disabled) + policy_event/audit_message 영속화. 코드 리뷰 후 MEDIUM 2건 + LOW 5건 수정 완료. 102/102 테스트 그린, 플레이키 0건. Phase 2 잔여는 응답 근거 표시(RAG 의존)뿐.

## 현재 상태

### 구현 완료 (Phase 1)

- **감사 로그 + 비용 추적**: async SQLAlchemy + AuditLog 테이블. 요청별 (request_id, principal, provider, model, tokens, cost_usd, latency_ms, status, error_type, stream) 저장. `app/db/`, `app/pricing.py`.
- **Org/User/APIKey 데이터 모델**: 멀티테넌트 인증. env 키는 부트스트랩 super_admin, DB 키는 prefix+sha256 저장. `app/db/models.py`, `app/auth.py`.
- **Redis 기반 rate limit**: Lua atomic 토큰버킷. 메모리 폴백. `RATE_LIMIT_STRICT=true`면 Redis 실패 시 startup 실패. `app/ratelimit/redis.py`.
- **Pre-auth IP limiter 공유 백엔드**: 다중 워커에서도 일관된 제한. `build_rate_limit_backend(..., key_prefix="preauth:")`.
- **Docker + docker-compose**: app + redis + postgres, non-root user, multi-stage build. `Dockerfile`, `docker-compose.yml`.
- **Admin 라우터** `/admin/*`: orgs, keys (sk-xxx 평문 1회 노출), usage 롤업, audit 페이지네이션, audit/replay. 멀티테넌트 격리 (non-super는 자기 org만). `app/routers/admin.py`.
- **Audit durability**: `AUDIT_SYNC=true`로 미들웨어가 commit까지 await. DB 실패 시 JSONL 폴백 + super_admin이 `POST /admin/audit/replay`로 재주입. `app/audit_fallback.py`.

### 보안 패치 완료

**HIGH 4건 전부 해결**:
1. 멀티테넌트 격리 (super_admin vs org-scoped admin)
2. X-Forwarded-For rightmost-trust (체인을 우→좌 워킹, 첫 untrusted 반환)
3. Audit log durability (sync 옵션 + JSONL 폴백 + replay)
4. Pre-auth limiter god env-key 블래스트 (super_admin 분리)

**MEDIUM 2건 전부 해결**:
1. Redis silent fallback (`RATE_LIMIT_STRICT` 옵션)
2. Pre-auth 단일 워커 고립 (공유 백엔드)

### 안정성

- 플레이키 원인: APIKeyResolver의 `_touch_last_used_at` fire-and-forget asyncio.create_task가 audit insert와 StaticPool 단일 SQLite 연결 경쟁.
- 해결: resolver가 SELECT + last_used_at UPDATE를 같은 세션에서 처리. auth_task 폐기.
- 검증: 50회 연속 실행 59/59 PASS.

### 코드 리뷰 수정 (MEDIUM 2건)

- **감사 replay 데이터 손실 수정** (`admin.py`): replay가 `os.replace`로 폴백 파일을 원자적 스냅샷 후 처리 → 동시 append 유실 방지. 재주입 실패/미처리 라인은 `<fallback>.failed`로 격리(영구 손실 방지). 스냅샷 rename 실패 시 503. (이전: 무조건 truncate로 실패 라인 손실.)
- **스트림 동시성 제한기 메모리 누수 수정** (`streaming.py`): `StreamLease.release()`가 limiter 경유, 마지막 lease 해제 시 `_semaphores`/`_active`에서 키 제거(idle 정리). reject-at-capacity 동작 보존, 이중 해제 멱등.
### 코드 리뷰 수정 (LOW/하드닝 5건, 완료)

- **#3 금지 정규식 상한** (`filter.py`): 패턴 길이 `MAX_RULE_PATTERN_CHARS=512`·개수 `MAX_RULES=200` 초과 시 컴파일 거부(부분 ReDoS 완화 — 순수 `re`는 타임아웃 불가, 완전 면역은 범위 밖).
- **#4 금지필터 컴파일 캐싱** (`filter.py`): `_compile_rules_cached` `lru_cache`(패턴 튜플 키) → 매 요청 재컴파일 제거. 런타임 패턴 변경도 안전(다른 튜플=재컴파일).
- **#5 last_used_at 디바운스** (`auth.py`/`config.py`): `API_KEY_LAST_USED_MIN_INTERVAL_SECONDS`(기본 60) 경과 시에만 UPDATE. naive datetime은 UTC로 정규화. 0이면 항상 업데이트. (플레이키 픽스 유지 — 같은 세션 처리.)
- **#6 pricing 로그 스팸** (`pricing.py`): 미등록 모델당 1회만 경고.
- **#7 SQLite 동시성** (`db/engine.py`): 파일 DB에 `PRAGMA journal_mode=WAL`+`busy_timeout=5000`+`synchronous=NORMAL`. `:memory:`는 StaticPool 유지.

### 리포 위생 / 재현성

- **줄바꿈 고정**: `.gitattributes` 추가(`* text=auto eol=lf` + 바이너리 규칙). 전역 `core.autocrlf=true`로 인한 워킹트리 LF/CRLF 혼재 제거. 워킹트리 재정규화 완료.
- **의존성 완전 고정**: `requirements.txt`를 검증된 venv의 전체 `pip freeze`로 교체(47개 전부 `==`). 기존 `>=` 6개 + 누락 전이 의존성(greenlet/lupa/Mako/MarkupSafe/sortedcontainers) 포함. `asyncpg==0.31.0` 설치로 venv가 선언 의존성과 일치.
- **pytest 설정 고정**: `pytest.ini`(`asyncio_mode=strict`, `asyncio_default_fixture_loop_scope=function`, `testpaths=tests`).
- **Python 3.13** 기준. README/HANDOFF 테스트 명령 일치: `.\.venv\Scripts\python.exe -m pytest -q`.

### P1 — 키 없는 E2E 품질 검증 (완료)

`tests/test_pii_e2e.py` (앱 코드 변경 0, fake provider만 사용). 실제 OpenAI/Anthropic 키 없이 gateway 흐름 전체 검증:
- **PII가 provider payload에 실제 반영**: `CaptureProvider.last_request`로 모델이 받는 메시지 확인 — 스트림/논스트림 모두 원문 PII 4종(RRN/카드/전화/이메일) 부재 + `[REDACTED:*]` 존재.
- **audit 무유출**: `AuditLog` row 전 컬럼 직렬화 후 원문 PII 부재 단언(stream True/False 파라미터화). `stream` 플래그·status도 검증.
- **구조화 로그 무유출**: caplog + root 핸들러로 모든 로그 수집, 원문 PII 부재 + `pii_masked` 이벤트는 카운트만(`{rrn:1,card:1,phone:1,email:1}`).
- **음성 대조**: `PII_MASKING_ENABLED=false`면 원문 그대로 전달 / `PII_TYPES=["email"]`면 이메일만 마스킹·RRN 원문 유지 (둘 다 `app.state.settings` 런타임 mutate로 검증).
- **스트림/논스트림 패리티**: 동일 입력에 플레이스홀더 타입 집합 동일.
- **멀티턴 라운드트립**: 4-메시지 대화 role/순서 보존 + OpenAI 응답 형태 검증.

## 다음에 해야 할 것

### Phase 2 — 컴플라이언스 (우선순위 高)

- [x] **PII 마스킹** ✅ 완료. `app/compliance/pii.py`. 요청 메시지에서 주민번호(생년월일+성별자리[1-8] 검증)·카드번호(Luhn 검증)·전화번호(휴대/02/지역/+82)·이메일 탐지 후 모델 전송 전 **비가역 마스킹**. 스팬 기반 비중복 치환, 우선순위 email>rrn>card>phone. 플레이스홀더 `[REDACTED:TYPE:n]` — 동일 raw 값은 동일 토큰(코어퍼런스 유지). 통합: `chat.py` `to_normalized()` 직후(스트림/논스트림 공통). 원문 PII는 로그/반환 어디에도 안 남김(카운트만 `log_event("pii_masked")`). 설정: `PII_MASKING_ENABLED`(기본 true), `PII_TYPES`(기본 rrn,card,phone,email). `request.state.pii_masked`에 카운트 저장(메시지 본문 저장 기능 연계용).
  - **결정/주의**: ① 전화번호 탐지는 광범위 → 전화번호 형태 식별자 오탐 가능(금융권에선 과마스킹이 안전). ② 계좌번호(계좌번호)는 형식 가변·오탐률 높아 **이번 범위에서 제외**(향후 보수적 추가 검토). ③ 마스킹은 system/assistant 포함 전 role에 적용(컴플라이언스 우선; few-shot 예시 영향은 트레이드오프). ④ 비가역 — 응답에서 detokenize 안 함(매핑 저장이 보안 표면이 되므로 의도적).
- [x] **금지표현 필터** ✅ 완료. `app/compliance/filter.py`. 설정 `FORBIDDEN_PATTERNS`(CSV `rule_id=regex`, 대소문자 무시)로 **입력** 메시지 검사(출력 필터는 스트리밍 버퍼링 복잡도로 의도적 보류). 원본 정규화 입력에 대해 **PII 마스킹 이전** 검사. 원문·매칭·캡처그룹 무저장(rule_id/count/severity만).
- [x] **정책 위반 모드** ✅ 완료 (goal 10). `POLICY_MODE` = block | log_only | disabled (기본 log_only). block=provider 미호출 + HTTP 403(OpenAI식 본문 `param:null`, code `content_policy_violation`) / log_only=통과+이벤트 기록 / disabled=미적용. 통합: `chat.py` 정규화 직후, 스트림 분기 이전(양 경로 공통).
- [ ] **응답 근거 표시**: 응답에 사용된 컨텍스트/문서 ID를 메타데이터로 첨부 (RAG 연계 → Phase 3 의존, 보류).
- [x] **메시지 본문 옵셔널 저장** ✅ 완료 (goal 11). `AUDIT_STORE_MESSAGES=true`(기본 false)면 **마스킹된** 본문만 `audit_message` 테이블에 저장(seq/role/content). 마스킹 분기 안에서만 `request.state.stored_messages` 설정 → 원문 저장 불가능 구조. block 요청은 마스킹 전 차단이라 저장 0건.
- [x] **policy event audit 구조** ✅ 완료 (goal 12). `policy_event` 테이블: request_id/principal_hash/org_id/api_key_id/event_type/action/rule_id/count/severity/stream/ts. forbidden_content(action block|log) + pii_mask(action mask, rule_id=타입) 통합 기록. audit 행과 **단일 트랜잭션**으로 영속화(`observability._insert_audit_log`). 원문 저장 안 함. (참고: DB 실패 시 JSONL 폴백은 audit 행만 — policy_event/audit_message는 이번 범위 폴백 제외.)

### Phase 3 — RAG (우선순위 中)

- [ ] 문서 인덱스 (Postgres + pgvector 또는 별도 벡터 DB)
- [ ] 임베딩 호출 (OpenAI/Anthropic)
- [ ] `/v1/chat/completions` 요청 전 retrieval → system context 주입
- [ ] 응답에 citation 메타데이터

### Phase 4 — 관리자 UI (우선순위 中)

- [ ] React/Vue 등으로 admin 대시보드
- [ ] 사용량/비용 차트, 키 관리 UI, audit 검색

### Phase 5 — 운영 강화 (꾸준히)

- [ ] **Alembic 마이그레이션 정식화** (현재 `Base.metadata.create_all` 사용 중. 운영 배포 전 필수).
- [x] **last_used_at debouncing** ✅ 완료. `API_KEY_LAST_USED_MIN_INTERVAL_SECONDS`(기본 60) 경과 시에만 UPDATE. 코드 리뷰 수정 섹션 #5 참고.
- [ ] **Audit log rotation/archival**: 1년+ 보관 후 콜드 스토리지로 이관.
- [ ] **Postgres 통합 테스트**: 현재 sqlite만 검증됨. compose로 띄워서 실제 테스트.
- [ ] **Docker compose 부팅 검증**: 현재 코드만 작성됨. `docker compose up` 실제 동작 확인 (Docker CLI 없는 환경에서 검증 안 됨).
- [ ] **Env key 부트스트랩 후 비활성 옵션**: `BOOTSTRAP_ONLY=true`면 시동 시 super admin 키 생성 후 env 키 비활성.

### 잔존 LOW/INFO

- [ ] `ALLOWED_HOSTS=*` 기본값 → 운영 전 변경 (README에 명시되어 있으나 강제 안 됨)
- [ ] `DOCS_ENABLED=true` 기본값 → 운영 전 false
- [ ] Postgres 기본 PW `change-me` → docker-compose에 경고 주석
- [ ] Cost 단가표 하드코딩 (`app/pricing.py`) → 환경설정 분리 또는 외부 API 동기화
- [ ] scope 값 검증 없음 (admin이 임의 scope 발급 가능)
- [ ] env 키와 DB 키가 같은 audit principal_hash로 들어가면 구분 어려움 (super_admin 사용자가 한 명만이면 무관)

## 핵심 파일 위치

- 진입점: `app/main.py`
- 라우터: `app/routers/{chat,admin,health,models}.py`
- 인증: `app/auth.py`
- DB: `app/db/{engine.py, models.py}`
- 미들웨어: `app/middleware.py`, `app/observability.py`
- 단가표: `app/pricing.py`
- 폴백 audit: `app/audit_fallback.py`
- 프록시 IP: `app/net.py`
- 설정: `app/config.py`, `.env.example`
- 컴플라이언스: `app/compliance/pii.py`(PII), `app/compliance/filter.py`(금지표현). 정책 모드/403은 `app/routers/chat.py` + `app/errors.py`(PolicyViolationError). 영속화는 `app/observability.py` + `app/db/models.py`(PolicyEvent, AuditMessage).
- 테스트: `tests/` (104건; PII `test_pii.py`/`test_pii_e2e.py`, 정책 `test_policy.py`/`test_policy_persistence.py`, 스트림 `test_streaming.py`, 하드닝 `test_hardening.py`, smoke 안전 `test_smoke_harness_safety.py`)
- 실제 API smoke: `scripts/smoke_provider.py` (pytest 비수집). 기본 dry-run, `--run`일 때만 실제 호출.

## 빠른 검증 명령

```powershell
cd C:\projects\ai-serving-backend
.\.venv\Scripts\python.exe -m pytest -q
```

실제 OpenAI/Anthropic 키 연결 점검(수동, 유료 호출):

```powershell
# dry-run (호출 안 함, 계획만)
.\.venv\Scripts\python.exe scripts\smoke_provider.py
# 실제 호출 (env에 OPENAI_API_KEY/ANTHROPIC_API_KEY 필요)
.\.venv\Scripts\python.exe scripts\smoke_provider.py --run
```

- 게이트웨이(ASGI) 전체 경로로 stream/non-stream 호출 → 실제 응답 + usage/cost + audit_log 행까지 검증. max_tokens 기본 16(상한 64). 키 없는 provider는 SKIP. `pytest`는 이 스크립트를 절대 실행 안 함(`testpaths=tests` + `tests/test_smoke_harness_safety.py`로 강제).

## 작업 흐름 (CLAUDE.md 기준)

1. 모든 코딩은 Codex 위임 (`mcp__codex__codex`, sandbox=danger-full-access, approval-policy=never).
2. Claude 계획 → Codex 구현 → Codex 결과 검토 → Claude 최종 검토.
3. 한 번에 너무 큰 스펙 던지면 한도 걸림. Phase 단위로 쪼개서 보내기.
4. 패치 후 항상 `pytest` 실행 + 다회 반복 실행으로 플레이키 확인.
