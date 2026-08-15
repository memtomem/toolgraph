# toolgraph, tracegraph, syncmill 연계 계획

**상태:** 첫 통합 마일스톤, P3.1 preview, T3, SyncMill board import 및 Toolgraph G3 완료 (2026-07-12)
**작성일:** 2026-07-11
**정본:** [전체 계획](https://github.com/memtomem/syncmill/blob/main/docs/ecosystem/integration-plan.md) · [구현 설계](https://github.com/memtomem/syncmill/blob/main/docs/ecosystem/implementation-design.md) · [smoke runbook](https://github.com/memtomem/syncmill/blob/main/docs/ecosystem/smoke-runbook.md)

> 실제 landed 순서와 설계 라벨은 정본에서 구분한다. P0/P1과 첫 마일스톤,
> P2 shared CLI/dashboard rendering, 실제 identity/qualified-tool advisory P3/Gate D는
> 완료됐다. strict P3.1 consumer enforcement는 SyncMill opt-in preview로 구현됐고
> Gate E 운영 evidence 승인은 열려 있다. Tracegraph T3 producer, SyncMill board import
> slice와 Toolgraph G3 artifact review도 완료됐지만 live qualified-tool telemetry와
> 자동 적용 없는 운영 검토 평가는 전체 P4 후속 범위로 남아 있다. 0단계와 1단계
> 체크리스트에서는 SyncMill 실행 보고서가 preflight `artifact_digest`를 기록하는
> cross-repo 항목만 열려 있다.

## 요약

세 프로젝트는 경쟁 관계가 아니라 에이전트 실행 수명주기의 서로 다른 지점을 담당한다.

| 프로젝트 | 역할 | 시점 |
| --- | --- | --- |
| toolgraph | MCP 도구 접근, 정책, 영향 분석 | 실행 전 |
| syncmill | 여러 코딩 에이전트의 격리 실행과 결과 조율 | 실행 중 |
| tracegraph | 실행 이력의 인과 분석과 회귀 탐지 | 실행 후 |

toolgraph의 권장 위치는 syncmill이 작업을 분배하기 전에 사용할 수 있는 도구 후보를
검사하는 advisory preflight이다. tracegraph에서 관측한 실패 신호는 이후 정책과 도구
선택을 개선하는 근거가 될 수 있지만 자동으로 ALLOW/DENY 정책을 바꾸어서는 안 된다.

## 현재 기준선

- MCP 서버를 크롤링해 `Agent -> Tool -> Resource -> Policy` 경로를 구성한다.
- `check-access`, `unsafe-tools`, `blast-radius`와 negative-truth 감사를 제공한다.
- 판정은 provenance가 포함된 설명 가능한 결과지만 런타임 enforcement는 아니다.
- selector surface는 deterministic filtering과 feature 산출에 한정한다.

## 목표

1. syncmill 실행 전에 사용할 도구 집합의 정책 위험과 drift를 검사한다.
2. 판정 근거를 run 단위 artifact로 남겨 실행 결과 및 trace와 연결한다.
3. tracegraph에서 발견된 실패 패턴을 정책 검토 후보로 환류한다.
4. toolgraph를 런타임 게이트웨이 또는 학습 시스템으로 확장하지 않는다.

## 비목표

- toolgraph가 agent subprocess 실행을 직접 중재하거나 차단하는 것
- tracegraph의 원시 span 또는 syncmill의 prompt 전체를 Neo4j에 저장하는 것
- 관측된 실패 한 건을 근거로 governance manifest를 자동 수정하는 것
- 세 저장소를 하나의 패키지나 데이터베이스로 합치는 것

## 제안 인터페이스

구현체는 기존 selector surface(`eligible_tools`/`rank_features`, ADR-0005)를 wrapping
하는 신규 CLI 명령이다. MCP의 `_with_generation` bracketing을 `graph/queries.py`로
추출해 CLI와 공유한다.

```
toolgraph preflight AGENT [CANDIDATES...] --profile strict|review|explore
                    --run-id RUN_ID [--features] [--out preflight.json]
```

### Preflight 요청

```json
{
  "schema_version": 1,
  "run_id": "stable-run-id",
  "agent": "codex",
  "candidate_tools": ["filesystem::read_file"],
  "policy_profile": "review"
}
```

### Preflight 결과 (`contracts/preflight.schema.json`)

```json
{
  "schema_version": 1,
  "kind": "toolgraph.preflight",
  "run_id": "stable-run-id",
  "created_at": "2026-07-11T00:00:00+00:00",
  "graph_generation": 42,
  "agent": "codex",
  "agent_found": true,
  "profile": "review",
  "mode": "advisory",
  "eligible": ["filesystem::read_file"],
  "rejected": [{"candidate": "…", "tool_key": "…", "reason": "DENY_GOVERNED", "paths": ["…"]}],
  "features": [],
  "decision": "advisory_allow | advisory_warn | unresolved_identity"
}
```

`schema_version`은 **정수**다(생태계 공통 — tracegraph artifact 계약과 일치).
`decision` 유도: `agent_found` false → `unresolved_identity`; rejected 행 존재 →
`advisory_warn`; 그 외 `advisory_allow`. exit code는 `advisory_warn`에도 0이다
(advisory는 enforcement가 아님 — ADR-0003의 opt-in gating 계약 유지).

계약에는 `graph_generation`, qualified tool key, verdict, classification 및 provenance를
포함한다. prompt, credential, 민감한 resource payload는 포함하지 않는다.
`paths`/provenance 문자열에는 **redaction pass**(resource URI의 userinfo/query 제거)를
적용한다 — provenance evidence에 credential이 실릴 수 있는 위험을 이 명령이 소유한다.
redaction fixture를 contract test에 포함한다.

## 단계별 계획

### 0단계: 계약 고정

- [x] qualified tool key와 agent identity 매핑을 문서화한다 — server-qualified key와
  `agent_found`/`unresolved_identity` 계약은 `contracts/preflight.schema.json`과
  가이드 문서에 정리됐다.
- [x] preflight JSON Schema와 버전 정책을 정의한다 — `contracts/preflight.schema.json`
  (additive-within-major 정책 포함), `tests/test_preflight_contract.py`가 고정한다.
- [x] `DENY`, `NOT_GRANTED`, drift, unmapped 상태별 소비자 정책을 정한다 — ADR-0008과
  `contracts/fixtures/`의 drift/unknown-tool/unresolved-identity fixture로 고정됐다.
- [x] syncmill이 MCP 도구를 직접 사용하지 않는 현재 구조에서 적용 범위를 확인한다 —
  ADR-0008이 advisory producer / SyncMill enforcement 경계로 기록했다.

### 1단계: 수동 artifact 연계

- [x] 기존 CLI로 preflight 결과를 JSON artifact로 내보내는 예제를 추가한다 —
  README의 `toolgraph preflight … --out preflight.json` 예제.
- [x] artifact에 `run_id`와 `graph_generation`을 기록한다 — 두 필드 모두 schema
  required이며 `toolgraph/preflight.py`가 기록한다.
- [x] provenance/path 문자열 redaction pass와 redaction fixture를 추가한다 —
  `toolgraph/redaction.py`와 `tests/test_preflight_contract.py`의 planted-violation
  scanner로 고정됐다.
- [ ] syncmill 실행 보고서가 해당 artifact 경로 또는 digest를 참조하게 한다 —
  producer 측은 완료됐다. `--out` 사용 시 `preflight`가 파일 바이트에 대한
  `artifact_digest` envelope를 stdout으로 출력한다. SyncMill 실행 보고서가 이
  digest를 기록하는 작업은 cross-repo로 남아 있다.
- [x] stale graph와 unknown agent가 성공으로 오인되지 않는 계약 테스트를 추가한다 —
  `tests/test_preflight.py`, `tests/test_preflight_graph.py`,
  `tests/test_preflight_contract.py`가 unresolved_identity와 generation bracketing을
  고정한다.

### 2단계: 선택적 syncmill adapter

- [x] syncmill 측의 opt-in preflight enforcement 요구사항을 공동 정의한다.
- [x] Toolgraph producer는 advisory로 유지하고 SyncMill mode가 선택 profile을 enforcement한다.
- [x] unknown/drift/unavailable 정책과 fail-open/fail-closed override를 명시한다.
- [x] strict block의 agent/worktree 이전 실행 보장은
  [SyncMill companion PR #56](https://github.com/memtomem/syncmill/pull/56)의 테스트로
  검증됐으며, 해당 PR merge로 완료됐다.

### 3단계: trace feedback

- [x] Tracegraph PR #10의 producer-derived v1 pattern 결과를 review candidate로 가져온다.
- [x] `run_id`, qualified tool key, pattern id/version, artifact digest만 투영한다.
- [x] 원본 report는 불변으로 두고 별도 sidecar에 operator disposition 이력을 기록한다.
- [x] SyncMill PR #57과 같은 exact-tuple UUIDv5로 candidate/board item을 상호 참조한다.
- [x] `accepted`도 manifest, policy, selector, Neo4j를 수정하지 않으며 blast radius와
  `graph_generation`이 보존됨을 regression test로 고정한다.
- [x] accepted 후보를 현재 agent/profile 판정과 graph state에 결합한
  `policy review-plan` artifact로 내보내되 `human_required` / `automatic_change: false`로
  고정한다. 실제 manifest 변경, ingest, bundle compile은 명시적으로 분리한다.

G3 annotation과 SyncMill board 상태는 독립적이다. 둘은 candidate id로 correlation만
가능하며 자동 동기화되지 않는다. 이후 실제 manifest 수정이 필요하면 별도 명시적
workflow에서 변경 전후 blast radius와 regression evidence를 남겨야 한다.

## 검증 기준

- 같은 graph generation과 입력은 같은 preflight 결과를 만든다.
- 결과의 모든 거부 사유는 graph path와 provenance로 설명 가능하다.
- trace 또는 syncmill이 없어도 toolgraph의 기존 CLI와 MCP API가 그대로 동작한다.
- 연계 기능이 꺼져 있으면 현재 성능과 exit-code 계약이 변하지 않는다.
- secret-bearing 입력이 artifact, 로그, provenance에 기록되지 않는다.

## 주요 위험과 대응

| 위험 | 대응 |
| --- | --- |
| advisory 결과가 enforcement로 오해됨 | artifact와 UI에 decision mode 명시 |
| graph가 실행 시점보다 오래됨 | `graph_generation`과 crawl timestamp 검증 |
| agent/tool 식별자 불일치 | server-qualified key와 명시적 mapping 사용 |
| trace 실패가 잘못된 정책으로 자동 승격됨 | review-only feedback와 운영자 승인 유지 |
| 분석 계층 간 강결합 | JSON artifact와 선택적 adapter만 공유 |

## 완료 정의

0단계와 1단계가 완료되고 실제 한 개 실행에서 preflight artifact, syncmill run 결과,
tracegraph artifact를 동일한 `run_id`로 추적할 수 있으면 첫 통합 마일스톤을 완료한 것으로
본다. 자동 정책 변경이나 런타임 gateway는 이 계획의 완료 조건이 아니다.
