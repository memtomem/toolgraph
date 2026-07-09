# toolgraph 운영 및 Context Engineering 활용 종합 보고서

## 요약

toolgraph는 현재 런타임 차단 게이트웨이가 아니라 MCP 도구 생태계를 그래프로
분석하는 advisory analyzer입니다. 이 특성은 tool selection 알고리즘 앞단의
context engineering 계층으로 활용하기 좋습니다. 선택 모델이 자연어 설명이나
embedding 유사도만 보고 도구를 고르는 대신, toolgraph가 제공하는 권한, 리소스,
정책, provenance, drift 정보를 함께 사용하면 후보 도구를 더 안전하게 줄이고
선택 이유를 설명할 수 있습니다.

권장 방향은 처음부터 강화학습을 붙이는 것이 아닙니다. 먼저 deterministic
filtering과 score 기반 ranking을 만들고, 선택/실행 telemetry를 모은 뒤 offline
evaluation과 learning-to-rank를 거쳐 contextual bandit으로 확장하는 순서가
현실적입니다. full RL은 장기 계획, multi-step tool chain, 명확한 보상 신호와
안전한 실험 환경이 준비된 뒤에 검토하는 것이 맞습니다.

## 현재 위치

toolgraph가 이미 제공하는 핵심 신호는 다음과 같습니다.

- `Agent -> Tool -> Resource -> Policy` 경로를 통한 권한 및 정책 도달성
- `CAN_CALL`, `READS`, `WRITES`, `GOVERNED_BY` 엣지의 provenance
- `check-access`의 `ALLOW`, `DENY`, `NOT_GRANTED`, `AGENT_NOT_FOUND`,
  `TOOL_NOT_FOUND`, `AMBIGUOUS_TOOL` verdict
- `unsafe-tools`의 `violation` / `authorized_but_governed` classification
- `unmapped-tools`, `unbacked-edges`, `drift` 같은 negative-truth 감사 쿼리
- `blast-radius`를 통한 리소스나 정책 변경 영향 범위

이 정보는 "이 도구가 task와 의미상 맞는가"를 직접 해결하지는 않습니다. 대신
"이 도구를 이 agent가 지금 선택해도 되는가", "선택했을 때 어떤 정책/리소스에
닿는가", "이 판단의 근거가 얼마나 신뢰 가능한가"를 구조화된 context로 제공합니다.

## 운영 관점 리뷰

### 보안

현재 Cypher 쿼리는 대부분 파라미터 바인딩을 사용하고, manifest ingest도 unresolved
reference가 있으면 전체 적용을 거부합니다. 이 점은 운영 안전성에 긍정적입니다.
다만 production-like 배치에서는 다음 보강이 필요합니다.

- 기본 Neo4j credential과 docker-compose 포트 공개는 local dev 전제로만 사용해야 합니다.
- `toolgraph serve --http`는 별도 인증/인가 레이어 없이 MCP query surface를 노출합니다.
- `servers.yaml`의 stdio `command`는 operator가 신뢰한 설정 파일이라는 전제가 필요합니다.
- HTTP/SSE crawl target과 headers는 민감 정보를 포함할 수 있으므로 로그와 provenance에
  secret이 섞이지 않도록 redaction 정책이 필요합니다.

운영 배포 전 최소 기준은 secret rotation, network binding 제한, reverse proxy 인증,
read-only DB 계정 분리, crawl 설정 리뷰 절차입니다.

### 성능

현재 MVP는 explainability와 정확성을 우선합니다. fleet 규모가 커지면 다음 비용이
커질 수 있습니다.

- `unmapped-tools`, `unbacked-edges`, `drift`, `orphan-policies`는 전체 그래프를
  훑는 감사 쿼리입니다.
- `blast-radius`는 특정 리소스나 정책에 연결된 tool/agent fan-out이 크면 결과가
  급격히 커집니다.
- crawl은 서버 단위 timeout과 concurrency를 갖지만, 각 서버의 tool/resource page 수에는
  별도 상한이 없습니다.
- MCP server wrapper는 쿼리 결과를 한 번에 반환하므로 대형 graph에서 response size가
  선택 latency에 직접 영향을 줍니다.

운영 최적화는 pagination, `LIMIT`/cursor, query profile 기반 index 점검, selector용
precomputed feature cache, crawl/ingest 후 cache invalidation 순서로 진행하는 것이 좋습니다.

### 안정성

manifest ingest는 clean manifest에 대해서만 authored edge를 지우고 다시 적용하므로
부분 적용으로 false ALLOW가 생기는 위험을 줄입니다. crawler도 서버별 실패를 분리해
partial success를 허용합니다.

보강할 부분은 운영 절차입니다.

- crawl 실패가 있는 상태에서 ingest를 진행할지에 대한 policy가 필요합니다.
- `drift` 결과가 남아 있을 때 selector가 해당 도구를 자동 후보에서 제외해야 합니다.
- 대형 변경 전후 graph snapshot이나 export가 있으면 rollback 판단이 쉬워집니다.
- HTTP serving은 health/readiness, timeout, request size limit, structured logging이
  필요합니다.

## Context Engineering 활용 모델

tool selection을 네 단계로 나누면 toolgraph의 역할이 명확합니다.

```text
user/task context
  -> candidate generation
  -> graph-aware filtering
  -> graph-aware ranking
  -> execution + telemetry
```

### 1. Candidate generation

초기 후보는 기존 방식대로 만들 수 있습니다.

- tool name / description search
- embedding similarity
- task classifier
- 최근 성공한 tool memory
- explicit user preference

이 단계는 recall을 높이는 데 집중합니다. 아직 policy 판단을 하지 않습니다.

### 2. Graph-aware filtering

toolgraph는 hard filter로 사용합니다.

- `AGENT_NOT_FOUND`: selection 자체를 중단하고 context 구성 오류로 보고합니다.
- `TOOL_NOT_FOUND`: 후보에서 제거합니다.
- `AMBIGUOUS_TOOL`: bare name을 자동 선택하지 않고 server-qualified key를 요구합니다.
- `NOT_GRANTED`: 후보에서 제거합니다.
- `DENY` + `violation`: 기본적으로 후보에서 제거합니다.
- `DENY` + `authorized_but_governed`: override policy가 있을 때만 낮은 우선순위로 유지합니다.
- `drifted_tools`: 후보에서 제거하거나 operator 확인을 요구합니다.
- `unmapped_tools`: 안전성이 중요한 task에서는 후보에서 제거하고, 일반 task에서는 패널티를 줍니다.

이 단계의 목적은 모델이 고르면 안 되는 도구를 ranking 이전에 제거하는 것입니다.

### 3. Graph-aware ranking

남은 후보에는 score를 부여합니다.

```text
score(tool) =
  task_match_score
  + permission_score
  + provenance_score
  + reliability_score
  - policy_risk_penalty
  - missing_mapping_penalty
  - drift_penalty
  - blast_radius_penalty
  - latency_cost_penalty
```

권장 feature는 다음과 같습니다.

| Feature | Source | Selection 영향 |
| --- | --- | --- |
| `permitted` | `check-access` | false면 hard reject |
| `verdict` | `check-access` | `ALLOW` 우선, `DENY`는 policy에 따라 reject |
| `classification` | `unsafe-tools` | `violation`은 reject, expected exception은 penalty |
| `provenance.source` | query result | `crawled`와 evidence 있는 authored edge를 우대 |
| `provenance.confidence` | query result | low confidence는 penalty |
| `has_evidence` | `unbacked-edges` inverse | evidence 없는 load-bearing edge는 penalty |
| `is_mapped` | `unmapped-tools` inverse | data-flow 미작성 도구는 penalty |
| `is_drifted` | `drift` inverse | stale 도구는 reject |
| `blast_radius_size` | `blast-radius` | 영향 범위가 큰 도구는 신중하게 penalty |
| `resource_policy_count` | graph query | 민감 리소스 접점이 많을수록 penalty |

### 4. Execution telemetry

선택 결과를 지속 개선하려면 selector가 다음 로그를 남겨야 합니다.

```json
{
  "event": "tool_selection",
  "agent": "planner",
  "task_hash": "stable-task-or-trace-id",
  "candidate_tools": ["filesystem::read_file", "git::git_show"],
  "selected_tool": "git::git_show",
  "graph_features": {
    "verdict": "ALLOW",
    "is_drifted": false,
    "has_unbacked_edges": false,
    "blast_radius_size": 3
  },
  "ranker_version": "heuristic-2026-06",
  "execution": {
    "success": true,
    "latency_ms": 420,
    "cost_units": 1,
    "retry_count": 0,
    "policy_violation": false
  },
  "feedback": {
    "user_corrected": false,
    "operator_override": false
  }
}
```

telemetry는 raw prompt나 secret-bearing resource URI를 그대로 저장하지 않는 정책이
필요합니다. task text는 hash나 redacted summary로 저장하는 편이 안전합니다.

## 성능 최적화 전략

### Online path

tool selection은 사용자 요청 경로에 있으므로 query budget을 작게 잡아야 합니다.

- 후보 수를 먼저 제한합니다. 예: semantic candidate top 50 이하.
- 후보별 `check-access`를 N번 호출하지 말고 batch query를 추가합니다.
- `drift`, `unmapped`, `unbacked` 결과는 selector cache로 유지합니다.
- crawl/ingest 이후 cache generation을 증가시켜 stale feature를 무효화합니다.
- `blast-radius`는 모든 후보에 매번 계산하지 말고 high-risk 후보에만 lazy 계산합니다.

### Offline path

감사와 학습 데이터 준비는 offline job으로 분리합니다.

- 전체 `unbacked-edges`와 `drift` 스캔
- policy별 blast-radius report
- feature distribution drift 감지
- failed selection replay
- ranker weight 튜닝

### Neo4j query hygiene

현재 uniqueness constraint는 주요 노드 key에 잡혀 있습니다. 운영 규모에서는 다음을 추가로
점검합니다.

- 자주 쓰는 relationship traversal의 cardinality
- high-degree resource/policy node
- `PROFILE` 기준 db hits가 큰 query
- audit query의 pagination 필요 여부
- response payload 상한

## 지속 개선과 강화학습 검토

### 1단계: Heuristic ranker

초기에는 사람이 이해할 수 있는 점수표가 낫습니다. 정책 위반, drift, missing evidence는
명확한 penalty로 두고, task matching은 기존 retrieval score를 사용합니다.

장점은 디버깅과 운영 승인입니다. 단점은 weight 조정이 수동이라는 점입니다.

### 2단계: Offline evaluation

과거 selection log를 replay해서 ranker가 어떤 선택을 했을지 비교합니다.

주요 metric은 다음과 같습니다.

- task success rate
- unsafe candidate reject rate
- unnecessary rejection rate
- average latency
- retry count
- user correction rate
- policy violation count

이 단계 없이는 RL이나 bandit을 붙여도 성능 개선인지 운인지 구분하기 어렵습니다.

### 3단계: Learning-to-rank

충분한 labeled outcome이 생기면 feature 기반 ranker를 학습할 수 있습니다. 이때도
hard safety constraint는 모델 밖에 둡니다.

```text
eligible candidates = graph hard filter(context, candidates)
ranked candidates = learned_ranker(features(eligible candidates))
```

모델이 아무리 높은 점수를 줘도 `NOT_GRANTED`, `violation`, `drifted` 같은 hard reject를
넘을 수 없게 해야 합니다.

### 4단계: Contextual bandit

tool selection은 대부분 "현재 context에서 어떤 tool을 고를까" 문제입니다. 장기 episode
reward를 다루는 full RL보다 contextual bandit이 먼저 맞습니다.

가능한 reward는 다음과 같습니다.

```text
reward =
  + task_success
  - retry_penalty
  - latency_penalty
  - cost_penalty
  - user_correction_penalty
  - policy_violation_penalty
```

운영에서는 exploration을 제한해야 합니다. 예를 들어 high-risk policy가 연결된 후보는
exploration 대상에서 제외하고, low-risk 동등 후보 사이에서만 탐색합니다.

### 5단계: Full RL

full RL은 다음 조건이 충족될 때만 검토합니다.

- multi-step tool chain의 상태와 action이 명확히 정의되어 있음
- delayed reward가 실제로 중요함
- simulation 또는 replay 환경이 있음
- safety constraint를 policy 밖에서 강제할 수 있음
- 실패 비용이 낮은 sandbox에서 학습 가능함

현재 toolgraph의 가장 가까운 활용처는 full RL이 아니라 graph-constrained contextual
ranking입니다.

## 권장 API 확장

selector 연동을 쉽게 하려면 다음 API가 유용합니다.

> **Erratum (2026-07-09)**: 이 섹션의 API는 2026-06-11에 구현·출하되었습니다.
> 규범적 형태는 `toolgraph/graph/selector.py`이며
> `tests/test_selector_contract.py`가 소비자(memtomem-stm) 계약으로 고정합니다.
> 초안 대비 변경: `path`(단수) → `paths`(리스트), `provenance_score` 필드는
> `rank_features` 출력에서 제외(unbacked 신호는 `has_unbacked_edges` +
> risk_score 0.4로 흡수), `risk_score`는 고정 테이블 값. 아래 표본은 구현
> 형태로 갱신되었습니다.

### `rank_features(agent, candidates)`

여러 candidate tool에 대해 selection feature를 batch로 반환합니다.

```json
{
  "agent": "planner",
  "agent_found": true,
  "features": [
    {
      "candidate": "filesystem::read_file",
      "tool_key": "filesystem::read_file",
      "found": true,
      "permitted": true,
      "verdict": "DENY",
      "classification": "violation",
      "is_drifted": false,
      "is_unmapped": false,
      "has_unbacked_edges": true,
      "risk_score": 1.0
    }
  ]
}
```

### `eligible_tools(agent, candidates, profile)`

hard filter 결과와 reject reason을 반환합니다.

```json
{
  "agent": "planner",
  "agent_found": true,
  "profile": "strict",
  "eligible": ["git::git_status"],
  "rejected": [
    {
      "candidate": "filesystem::read_file",
      "tool_key": "filesystem::read_file",
      "reason": "DENY_VIOLATION",
      "paths": ["(read_file) -READS-> (file:///private/secrets.env) -GOVERNED_BY-> (secrets-deny:DENY)"]
    }
  ]
}
```

### `selection_explain(agent, tool)`

최종 선택 이유를 사용자나 operator에게 설명하기 위한 compact result입니다.

```json
{
  "tool_key": "git::git_status",
  "decision": "selected",
  "reasons": [
    "agent has CAN_CALL grant",
    "no DENY policy path found",
    "all load-bearing edges have evidence",
    "tool is currently exposed by crawled server"
  ]
}
```

## Repo 기준 구현 메모

현재 코드 구조에서는 다음 위치에 작게 나누어 붙이는 것이 가장 자연스럽습니다.

| 영역 | 현재 위치 | 확장 방향 |
| --- | --- | --- |
| Graph query | `toolgraph/graph/queries.py` | batch candidate feature query 추가 |
| MCP wrapper | `toolgraph/server/app.py` | `rank_features`, `eligible_tools`, `selection_explain` 노출 |
| CLI | `toolgraph/cli.py` | operator가 수동 검증할 수 있는 command 추가 |
| Models | `toolgraph/models.py` | selection feature/result pydantic model 추가 |
| Tests | `tests/test_queries.py`, `tests/test_server.py` | direct query와 MCP shape 동시 검증 |

### `rank_features` query 방향

후보 도구별로 `check-access`를 반복 호출하면 latency가 후보 수에 선형으로 늘어납니다.
selector용 API는 candidate list를 한 번에 받아 다음 정보를 batch로 반환하는 것이 좋습니다.

- tool resolution 결과: not found, ambiguous, resolved key
- agent grant 여부
- DENY path 존재 여부와 classification
- drift 여부
- unmapped 여부
- load-bearing edge의 missing evidence 여부

MVP에서는 `risk_score`를 학습 모델이 아니라 규칙 기반으로 계산해도 충분합니다.

```text
risk_score =
  1.0 if DENY violation
  0.8 if drifted
  0.6 if unmapped
  0.4 if unbacked load-bearing edge
  0.2 if authorized_but_governed
  0.0 otherwise
```

### `eligible_tools` policy 방향

hard filter policy는 코드에 흩뿌리지 말고 이름 있는 policy profile로 두는 편이 낫습니다.

| Profile | 용도 | 기본 동작 |
| --- | --- | --- |
| `strict` | production agent | `ALLOW`만 통과 |
| `review` | human-in-the-loop review | expected exception은 통과, violation은 reject |
| `explore` | offline eval/sandbox | not granted와 drift만 reject, 나머지는 penalty |

이 profile은 selector 실험을 가능하게 하지만, production default는 `strict`로 두어야 합니다.

### Telemetry 저장 방향

초기에는 DB schema를 크게 늘리기보다 append-only JSONL이나 별도 event sink로 시작하는 것이
좋습니다. 운영적으로 중요한 것은 학습보다 재현성입니다.

필수 필드는 다음 네 가지입니다.

- `ranker_version`: 어떤 selector logic이 선택했는지
- `graph_generation`: 어떤 crawl/ingest 상태의 feature였는지
- `reject_reasons`: 후보가 왜 제외되었는지
- `outcome`: 실행 성공, latency, retry, user correction

`graph_generation`은 아직 코드에 없으므로 crawl/ingest가 끝날 때 증가하는 run id나 timestamp를
도입하는 것이 후속 작업의 시작점입니다.

## 검증 기준

selector 연동 PR은 기능 테스트뿐 아니라 운영 회귀 테스트를 포함해야 합니다.

- ambiguous bare tool name은 자동 선택되지 않아야 합니다.
- `DENY` + `violation` 후보는 `strict` profile에서 제외되어야 합니다.
- `authorized_but_governed` 후보는 `strict`에서는 제외, `review`에서는 통과해야 합니다.
- drifted tool은 모든 online profile에서 제외되어야 합니다.
- unmapped tool은 `strict`에서 제외되거나 명시 penalty를 받아야 합니다.
- batch API 결과는 candidate 입력 순서와 stable id를 유지해야 합니다.
- MCP wrapper는 direct query와 같은 structured shape를 반환해야 합니다.
- telemetry에는 raw secret value나 authorization header가 저장되지 않아야 합니다.

## Roadmap

1. **Selector adapter**
   기존 selector 앞단에서 `check-access`, `drift`, `unmapped-tools` 결과를 hard filter로 사용합니다.

2. **Batch feature query**
   후보 N개를 한 번에 평가하는 `rank_features` 계열 쿼리를 추가합니다.

3. **Telemetry schema**
   selection event, execution outcome, user correction, operator override를 저장합니다.

4. **Offline eval**
   fixed test set과 replay log로 heuristic weight를 조정합니다.

5. **Feature cache**
   crawl/ingest generation 단위로 graph-derived feature를 캐시합니다.

6. **Learning-to-rank**
   hard filter 이후의 eligible candidate ranking만 학습합니다.

7. **Contextual bandit**
   low-risk 후보 사이에서 제한된 exploration을 도입합니다.

8. **Runtime gateway 연계**
   toolgraph는 analyzer로 유지하고, 실제 차단은 gateway나 execution layer에서 수행합니다.

## 결론

toolgraph는 tool selection 알고리즘의 "생각하는 모델"을 대체하지 않습니다. 대신 선택에
필요한 운영 context를 구조화합니다. 권한, 정책, 리소스, provenance, drift를 selection
context에 넣으면 모델은 더 적은 후보에서, 더 낮은 위험으로, 더 설명 가능한 선택을 할 수
있습니다.

지금 가장 가치 있는 다음 단계는 강화학습이 아니라 다음 세 가지입니다.

1. graph-aware hard filter
2. batch rank feature API
3. selection telemetry와 offline evaluation

이 세 가지가 안정화되면 learning-to-rank와 contextual bandit이 자연스럽게 붙습니다.
