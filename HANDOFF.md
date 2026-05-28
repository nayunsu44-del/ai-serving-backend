# AI Serving Backend — Session Handoff

마지막 작업: 2026-05-29

## 한 줄 요약

금융권용 AI Gateway MVP. Phase 1(기반 인프라) + 보안 패치 완료. Phase 2 착수: PII 마스킹 구현 완료. 68/68 테스트 그린, 플레이키 0건. 다음은 Phase 2 잔여(금지표현 필터, 메시지 본문 저장).

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

### 리포 위생 / 재현성

- **줄바꿈 고정**: `.gitattributes` 추가(`* text=auto eol=lf` + 바이너리 규칙). 전역 `core.autocrlf=true`로 인한 워킹트리 LF/CRLF 혼재 제거. 워킹트리 재정규화 완료.
- **의존성 완전 고정**: `requirements.txt`를 검증된 venv의 전체 `pip freeze`로 교체(47개 전부 `==`). 기존 `>=` 6개 + 누락 전이 의존성(greenlet/lupa/Mako/MarkupSafe/sortedcontainers) 포함. `asyncpg==0.31.0` 설치로 venv가 선언 의존성과 일치.
- **pytest 설정 고정**: `pytest.ini`(`asyncio_mode=strict`, `asyncio_default_fixture_loop_scope=function`, `testpaths=tests`).
- **Python 3.13** 기준. README/HANDOFF 테스트 명령 일치: `.\.venv\Scripts\python.exe -m pytest -q`.

## 다음에 해야 할 것

### Phase 2 — 컴플라이언스 (우선순위 高)

- [x] **PII 마스킹** ✅ 완료. `app/compliance/pii.py`. 요청 메시지에서 주민번호(생년월일+성별자리[1-8] 검증)·카드번호(Luhn 검증)·전화번호(휴대/02/지역/+82)·이메일 탐지 후 모델 전송 전 **비가역 마스킹**. 스팬 기반 비중복 치환, 우선순위 email>rrn>card>phone. 플레이스홀더 `[REDACTED:TYPE:n]` — 동일 raw 값은 동일 토큰(코어퍼런스 유지). 통합: `chat.py` `to_normalized()` 직후(스트림/논스트림 공통). 원문 PII는 로그/반환 어디에도 안 남김(카운트만 `log_event("pii_masked")`). 설정: `PII_MASKING_ENABLED`(기본 true), `PII_TYPES`(기본 rrn,card,phone,email). `request.state.pii_masked`에 카운트 저장(메시지 본문 저장 기능 연계용).
  - **결정/주의**: ① 전화번호 탐지는 광범위 → 전화번호 형태 식별자 오탐 가능(금융권에선 과마스킹이 안전). ② 계좌번호(계좌번호)는 형식 가변·오탐률 높아 **이번 범위에서 제외**(향후 보수적 추가 검토). ③ 마스킹은 system/assistant 포함 전 role에 적용(컴플라이언스 우선; few-shot 예시 영향은 트레이드오프). ④ 비가역 — 응답에서 detokenize 안 함(매핑 저장이 보안 표면이 되므로 의도적).
- [ ] **금지표현 필터**: 입력 + 출력에 대해 금지 키워드/패턴 검사. 차단 또는 로깅. (PII와 같은 `app/compliance/` 패키지에 추가 예정.)
- [ ] **응답 근거 표시**: 응답에 사용된 컨텍스트/문서 ID를 메타데이터로 첨부 (RAG 연계 → Phase 3 의존, 보류).
- [ ] **메시지 본문 옵셔널 저장**: `AUDIT_STORE_MESSAGES=true`일 때 PII 마스킹된 본문을 audit_log에 저장 (별도 테이블 권장). PII 마스킹 완료됐으므로 `request.state.pii_masked`/마스킹된 normalized 메시지 활용 가능.

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
- [ ] **last_used_at debouncing**: 매 요청 UPDATE는 부하. N초마다만 UPDATE하도록 조정. (현재는 race 회피용으로 일단 sync 처리.)
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
- 컴플라이언스(PII): `app/compliance/pii.py`
- 테스트: `tests/` (68건, PII는 `tests/test_pii.py`)

## 빠른 검증 명령

```powershell
cd C:\projects\ai-serving-backend
.\.venv\Scripts\python.exe -m pytest -q
```

## 작업 흐름 (CLAUDE.md 기준)

1. 모든 코딩은 Codex 위임 (`mcp__codex__codex`, sandbox=danger-full-access, approval-policy=never).
2. Claude 계획 → Codex 구현 → Codex 결과 검토 → Claude 최종 검토.
3. 한 번에 너무 큰 스펙 던지면 한도 걸림. Phase 단위로 쪼개서 보내기.
4. 패치 후 항상 `pytest` 실행 + 다회 반복 실행으로 플레이키 확인.
