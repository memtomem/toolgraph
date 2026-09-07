# Toolgraph 현재 구현 종합 리뷰

작성일: 2026-09-07 · 검토 방식: 코드·계약·문서 검토, 기존 테스트 실행, 격리된 두 백엔드 재현, 로컬 패키지 검증

## 1. 총평

Toolgraph는 **실행을 강제하지 않는 governance 분석·정책 산출물 생산 도구**라는 제품 경계를 비교적 일관되게 지킨다. 정책 판정, provenance, drift, 사람의 검토와 정책 변경의 분리, 원자적 파일 저장, 배포 권한 분리에는 구체적인 구현과 회귀 테스트가 있다. 이번 기준 커밋에서 **기존 테스트 414개와 Ruff가 통과**했고, 빌드한 wheel을 독립 설치한 quickstart도 완료했다.

다만 테스트 성공만으로 기본 로컬 경로와 계약의 완전성을 판단할 수는 없다. 추가 재현에서 **P1 2건, P2 6건, P3 1건**을 확인했다. 특히 기본 권장 백엔드인 Ladybug는 tools/resources/policies의 정상적인 빈 목록을 일부 처리하지 못한다. 또한 산출물 redaction이 에이전트 식별자를 바꾸어 서로 다른 판정을 같은 principal에 붙이는 문제가 있다. 이 두 항목을 우선 수정하고, 나머지 계약·식별자·정보 노출 문제를 회귀 테스트로 고정하는 것이 좋다.

이 평가는 로컬 구현에 대한 판단이다. 운영 배포 적합성, 원격 CI 성공, 외부 gateway의 실제 enforcement 성공을 뜻하지 않는다. 아래 발견 사항의 수정은 이번 리뷰에 포함하지 않았다.

## 2. 기준과 검토 범위

| 항목 | 기준 |
| --- | --- |
| 저장소 | `toolgraph` |
| 브랜치 | `perf/p1-p2-backlog` |
| 기준 HEAD | `45b594872cf0d94635aa26db8a7f6fe6464159ae` |
| 시작 상태 | 작업 트리 clean |
| 주요 구현 규모 | 핵심 패키지 Python 약 6,339줄(quickstart 제외); 테스트·스크립트·계약·문서 별도 |
| 호스트 | macOS 26.6.2, arm64, Python 3.12.11 |
| 기존 테스트 환경 | Ladybug 0.18.3, Neo4j Python driver 6.2.0, MCP 1.28.1, Pydantic 2.13.4 |
| 검증 도구 | pytest 9.1.1, Ruff 0.15.22, jsonschema 4.26.0 |
| Neo4j 서버 | testcontainers로 만든 `neo4j:5.26-community`; 사용자 DB 사용 안 함 |
| Ladybug 저장소 | 임시 DB만 사용; 추가 probe의 시나리오 사이에는 reset |
| 수정 범위 | 이 보고서만 저장; 제품 소스·테스트·계약·lockfile 변경 없음 |

전체 구현을 기준으로 검토했고, 최근 배치 조회·ingest·잠금·파일 처리 변경은 추가로 확인했다. 중점 대상은 crawler → loader → manifest ingest → query/selector → CLI/MCP → preflight/bundle/review-plan의 흐름과 백엔드 adapter, JSON 계약, CI·패키징이다.

심각도는 P1=기본 사용 또는 핵심 결과의 신뢰성을 훼손하므로 우선 수정, P2=특정 입력·규모·연동에서 재현되는 기능/계약 결함, P3=영향 범위가 좁은 일관성 결함으로 사용한다. 심각도와 증거 수준은 구분하며, 아래 R1–R9는 모두 실행 재현을 갖는다.

## 3. 구현 구조와 강점

```mermaid
flowchart LR
    S[MCP servers] --> C[Crawler]
    C --> L[Crawl loader]
    Y[Authored governance YAML] --> I[Manifest ingest]
    L --> G[Neo4j / Ladybug]
    I --> G
    G --> Q[Query / Selector]
    Q --> U[CLI / MCP responses]
    Q --> A[Preflight / Policy bundle]
    T[Tracegraph report] --> H[Human review sidecar]
    H --> P[Policy review plan]
    Q --> P
    A --> E[External consumer]
```

- **책임 분리:** Toolgraph 안에 scheduler/executor를 추가하지 않고 판정과 근거를 외부에 제공한다. accepted review도 manifest나 graph를 자동 변경하지 않는다. ADR-0008·0010·0012·0013과 부합한다.
- **정책 의미:** unknown agent/tool, ambiguous bare name, 미부여 권한, DENY violation, operator exception, drift, unmapped를 구별한다. strict/review/explore의 차이가 상수와 테스트에 드러난다.
- **변경 원자성:** manifest는 참조 검증 후 동일 트랜잭션에서 authored 상태를 교체한다. 거부된 ingest가 기존 상태를 보존하는 것을 두 백엔드에서 재확인했다. 성공한 reset은 새로운 instance ID와 generation 0을 만들었다.
- **증거의 출처:** crawled 사실, authored assertion, annotation self-claim을 구분한다. annotation을 자동 정책 권한으로 사용하지 않는다.
- **성능 개선:** selector는 후보 수와 무관한 배치 쿼리 구조를 사용한다. audit의 다중 agent 배치에서 예외가 다른 agent로 새지 않는 기존 회귀 시나리오를 Ladybug에서도 실행했다.
- **파일·배포 처리:** private atomic replace, 파일 fsync, 부모 디렉터리 fsync의 불확실성을 별도 경고로 전달하는 구조가 있다. 릴리스는 build와 publish를 분리하고 publish job에 checkout/의존성 설치를 두지 않는다.

## 4. 발견 사항 요약

| ID | 우선순위 | 발견 사항 | 재현 범위 |
| --- | --- | --- | --- |
| R1 | P1 | Ladybug가 빈 tools/resources/policies 목록을 처리하지 못함 | Ladybug 실패, Neo4j 성공 |
| R2 | P1 | 산출물 redaction이 agent/principal/run 식별자를 변경·병합함 | 두 백엔드 |
| R3 | P2 | policy review-plan의 경로에 URI 자격정보가 그대로 남음 | 두 백엔드 |
| R4 | P2 | ExpectedException 복합 키 충돌로 선언한 예외가 사라짐 | 두 백엔드, 충돌 후 남는 예외도 다름 |
| R5 | P2 | control-plan loader가 JSON Schema에서 거부하는 입력을 승인함 | 백엔드와 무관 |
| R6 | P2 | 노출 도구가 4,097개이면 전체 policy bundle 컴파일 불가 | 두 백엔드 |
| R7 | P2 | 손상된 기본 설정이 명시적 `--config`, 도움말, 버전 조회까지 차단함 | 새 CLI 프로세스 |
| R8 | P2 | 주입한 GraphReader의 데이터에 다른 런타임 DB의 state를 붙임 | 실제 Ladybug + 별도 reader |
| R9 | P3 | 일부 producer가 timezone 없는 시각을 출력하며 계약 테스트도 이를 놓침 | 두 백엔드의 Python API |

### R1. [P1] Ladybug의 빈 목록이 정상적인 crawl·ingest를 실패시킨다

**위치:** [loader.py:57](../../toolgraph/graph/loader.py#L57), [loader.py:85](../../toolgraph/graph/loader.py#L85), [ingest.py:320](../../toolgraph/manifest/ingest.py#L320), [driver.py:159](../../toolgraph/graph/driver.py#L159).

`UNWIND $tools AS t`, `$resources AS r`, `$policies AS p` 뒤에서 원소의 속성을 참조한다. Ladybug 0.18.3은 해당 빈 파라미터 목록의 원소를 필요한 struct로 추론하지 못하고 binder error를 발생시킨다. 목록이 비어 있다는 이유로 이 쿼리가 실행되지 않는 것은 아니다.

| 입력 | Ladybug | Neo4j |
| --- | --- | --- |
| tools 1개, resources 0개 | `r has data type STRING ...` | 성공 |
| tools 0개, resources 1개 | `t has data type STRING ...` | 성공 |
| tools 0개, resources 0개 | `t has data type STRING ...` | 성공 |
| tools 1개, resources 1개 | 성공 | 성공 |
| `Governance(agents=['a'])`, policies 없음 | `p has data type STRING ...` | 성공 |

리소스 기능이 없는 서버는 crawler에서도 자연스럽게 `resources=[]`를 만든다. 리소스를 지원할 때 capability를 선언하도록 하는 [MCP resources 규격](https://modelcontextprotocol.io/specification/2025-11-25/server/resources)과도 모순되지 않는 정상 입력이다. 기존 도구를 모두 제거한 재수집 역시 빈 목록이 되므로 이 문제는 초기 등록에만 국한되지 않는다. 재수집 저장 트랜잭션이 실패하면 그 pass의 reconciliation도 반영되지 않는다.

**최소 재현:** 저장소의 가상환경에서 다음 코드를 실행한다. 임시 DB만 사용한다.

```python
from pathlib import Path
from tempfile import TemporaryDirectory
from toolgraph import config
from toolgraph.graph import driver, loader, schema
from toolgraph.models import CrawlResult, ToolRecord

with TemporaryDirectory() as tmp:
    config.settings = config.Settings(
        backend="ladybug", db_path=Path(tmp) / "graph.lbug"
    )
    try:
        schema.init_schema()
        loader.load_crawl_result(
            CrawlResult(server_name="s", tools=[ToolRecord(name="t")])
        )
    finally:
        driver.close_driver()
```

실제 오류: `RuntimeError: Binder exception: r has data type STRING but (NODE,REL,STRUCT,ANY) was expected.`

**수정 방향:** 빈 목록의 upsert 쿼리는 생략하거나 명시적으로 타입을 부여한다. 단, 목록이 비어도 stale EXPOSES/PROVIDES 제거와 manifest의 기존 authored 상태 삭제는 계속 실행해야 한다. 단순 조기 return으로 reconciliation을 생략하면 안 된다.

**회귀 기준:** 위 5개 입력, nonempty→empty 재수집, empty manifest에 의한 revoke, 실패 시 rollback을 양쪽 백엔드의 동일 시나리오로 검사한다. 현재 Ladybug smoke fixture는 tools/resources/policies가 있는 경로에 편중되어 있다.

### R2. [P1] Redaction이 서로 다른 principal을 같은 ID로 만든다

**위치:** [control_plan.py:470](../../toolgraph/control_plan.py#L470), [preflight.py:105](../../toolgraph/preflight.py#L105), [policy_bundle.py:169](../../toolgraph/policy_bundle.py#L169).

control-plan은 공백 없는 URI 형태의 principal ID를 허용한다. 그러나 `redact(compiled['evaluations'])`는 evidence뿐 아니라 `principal`까지 재귀적으로 수정한다. 주석은 식별자를 보존한다고 설명하지만 실제 범위는 더 넓다. 일반 preflight와 policy bundle도 전체 객체를 redaction에 넣는다.

**입력:** 다음 두 agent를 선언하고 A에만 `s::t`를 부여한다. 두 principal 모두 `candidate_tools=['s::t']`로 control-preflight를 review profile에서 실행한다.

```text
https://agents.example/worker?tenant=A
https://agents.example/worker?tenant=B
```

**실제 evaluations:** 두 백엔드에서 동일하다.

```json
[
  {"principal":"https://agents.example/worker","agent_found":true,
   "eligible":["s::t"],"rejected":[]},
  {"principal":"https://agents.example/worker","agent_found":true,
   "eligible":[],"rejected":[
     {"candidate":"s::t","tool_key":"s::t","reason":"NOT_GRANTED"}
   ]}
]
```

또한 일반 preflight의 `run_id=https://runs.example/r?attempt=1`은 `https://runs.example/r`로 바뀌고, preflight/bundle의 agent도 query를 잃는다. 원래 identity에 판정을 결합하거나 완전한 평가 집합을 확인할 수 없게 된다. 이것이 실제 외부 consumer에서 권한 우회로 이어지는지는 이번 범위에서 확인하지 않았다.

**수정 방향:** 타입/필드에 따라 identity와 evidence를 구분한다. 자격정보가 없는 identity는 정확히 보존하고, 허용할 수 없는 identity는 입력 단계에서 명시적으로 거부한다. 성공 산출물에서 ID를 조용히 변환하지 않는다. 기존 일반 preflight/bundle도 함께 점검해야 한다.

**회귀 기준:** query/fragment만 다른 두 principal의 상반된 판정, URI 형태 run ID의 exact round-trip, credential-bearing identity의 거부, evidence URI의 redaction을 동시에 검사한다.

### R3. [P2] Policy review-plan은 URI 자격정보를 export한다

**위치:** [policy_review.py:75](../../toolgraph/policy_review.py#L75), [policy_review.py:82](../../toolgraph/policy_review.py#L82).

`current_paths`에 selector의 문자열을 그대로 넣고 artifact를 반환한다. preflight/bundle과 달리 경로 redaction이 없다. 다음 **합성** URI를 READS 대상으로 하고 DENY 정책을 연결한 뒤, `s::t`의 Tracegraph candidate를 accepted로 기록하여 `build_policy_review_plan(report=..., agent='a')`을 호출했다.

```text
입력 URI:
https://user:SYNTHETIC_PASS@resource.example/data?token=SYNTHETIC_TOKEN

review-plan current_paths:
(t) -READS-> (https://user:SYNTHETIC_PASS@resource.example/data?token=SYNTHETIC_TOKEN) -GOVERNED_BY-> (p:DENY)

같은 그래프의 policy-bundle paths:
(t) -READS-> (https://resource.example/data) -GOVERNED_BY-> (p:DENY)
```

이 결과는 두 백엔드에서 같다. [SECURITY.md](../../SECURITY.md)는 resource identity를 위해 graph 내부에는 원문을 저장하고 exported artifacts가 자체 redaction을 수행한다고 설명한다. 같은 문서는 operator에게 resource URI에 자격정보를 넣지 말라고도 안내한다. 따라서 이 발견은 **그런 URI가 이미 들어온 경우 export 방어가 누락되는 문제**이며, 임의의 비밀 파일을 읽는 취약점으로 해석하면 안 된다. 파일 권한 0600은 후속 검토·공유 시 내용 노출을 막지 못한다.

**수정 방향:** review-plan의 evidence 경로만 공통 redaction으로 처리한다. R2를 피하려면 artifact 전체를 redaction에 넣어서는 안 된다.

**회귀 기준:** 실제 accepted sidecar와 그래프에서 작업 계획을 생성하고, 저장된 JSON에 userinfo/query/fragment가 없는지 확인한다. candidate ID, run ID, artifact digest, source report digest는 보존되어야 한다.

### R4. [P2] ExpectedException 키가 충돌하여 선언한 예외를 덮어쓴다

**위치:** [schema.py:83](../../toolgraph/graph/schema.py#L83), [ingest.py:402](../../toolgraph/manifest/ingest.py#L402).

예외 키는 `agent|tool_key|resource|policy` 단순 연결이다. 각 필드의 `|`를 금지하거나 escape하지 않으므로 다른 유효 입력이 같은 키를 만든다.

| agent | tool | resource | policy | 생성 키 |
| --- | --- | --- | --- | --- |
| `a` | `s::t` | `urn:x\|y` | `p` | `a\|s::t\|urn:x\|y\|p` |
| `a` | `s::t` | `urn:x` | `y\|p` | `a\|s::t\|urn:x\|y\|p` |

재현 manifest는 두 DENY 정책을 선언하고 `a → s::t` grant, 두 resource에 대한 READS, 각 resource의 해당 policy binding, 표의 두 expected exception을 포함한다. ingest는 경고 없이 적용되지만 **ExpectedException 노드는 2개가 아닌 1개**다. `check_access('a', 's::t')`의 한 경로는 `violation`, `all_authorized`는 false가 된다. 이번 실행에서는 Neo4j와 Ladybug가 서로 다른 예외를 남겼다. 키 충돌에 더해 UNWIND의 중복 MERGE 처리 순서에 의존한다.

**영향:** 선언한 예외 일부가 사라져 review profile과 audit에서 잘못된 위반을 보고한다. 이번 재현은 false deny이며, false allow를 증명한 것은 아니다.

**수정 방향:** 타입을 보존한 canonical tuple 직렬화 또는 그 digest로 키를 만든다. 동일 logical exception 중복의 reason 처리도 명시한다. 기존 DB는 manifest 재-ingest에서 예외를 전체 재생성하는 경로를 이용할 수 있으나, 키 표현 변경의 호환성을 확인해야 한다.

**회귀 기준:** delimiter 포함 각 필드의 충돌 쌍, wildcard와 specific exception, 중복 선언을 양쪽 백엔드에서 검사한다. 예외 수와 경로별 classification까지 대조한다.

### R5. [P2] Control-plan loader와 JSON Schema의 승인 집합이 다르다

**위치:** [control_plan.py:85](../../toolgraph/control_plan.py#L85), [control_plan.py:193](../../toolgraph/control_plan.py#L193), [control_plan.schema.json](../../contracts/control-plan.schema.json).

`_AdditiveModel`은 `extra='ignore'`만 설정하고 타입 coercion을 허용한다. 필수인 `maximum_cycles`는 모델에서 기본값을 갖고, optional 필드의 명시적 null과 필드 부재도 일부 같은 것으로 처리한다.

기존 `contracts/fixtures/control-plan-v1.json`에서 아래 항목을 **각각 하나씩** 변경했다. `load_control_plan(json.dumps(plan).encode())`은 모두 승인하지만 `Draft202012Validator(schema).validate(plan)`은 모두 거부한다.

| 변경 | Schema 거부 근거 |
| --- | --- |
| `limits.maximum_cycles` 삭제 | 필수 속성 없음 |
| `limits.maximum_agent_calls = "2"` | integer가 아님 |
| `schema_version = true` | const 1과 다름 |
| start node의 `agent_call = "false"` | boolean이 아님 |
| start node에 `timeout_seconds = null` 추가 | integer가 아니며 non-agent에 금지된 필드 |

이는 additive field 허용과 별개의 문제다. producer가 승인한 원본 plan을 계약에 따라 검증하는 consumer는 거부할 수 있다. digest는 원본 bytes를 묶지만 producer의 평가는 coercion/default 적용 후 모델에 대해 수행된다.

**수정 방향:** 기존 명시적 Schema 계약에 loader를 맞춘다. strict validation, 필수 필드 선언, field presence 검사를 함께 적용하고 Literal의 bool/int 구별도 확인한다. Pydantic의 coercion과 strict mode 차이는 [공식 문서](https://docs.pydantic.dev/latest/concepts/strict_mode/)에 설명되어 있다. strict 설정 하나만으로 모든 차이가 사라진다고 가정하지 않는다.

**회귀 기준:** 위 5개 변형과 정상 fixture, 같은 major의 additive field, 미지원 major를 양쪽 validator에 넣는 differential test를 추가한다. 토폴로지 lint의 advisory finding과 입력 자체의 invalid를 구분한다.

### R6. [P2] 전체 catalog 컴파일이 selector의 4,096개 요청 한도에 막힌다

**위치:** [policy_bundle.py:98](../../toolgraph/policy_bundle.py#L98), [graph/store.py:47](../../toolgraph/graph/store.py#L47), [selector.py:195](../../toolgraph/graph/selector.py#L195).

compiler는 현재 노출된 **모든** 도구를 하나의 candidate 목록으로 만들어 selector에 넘긴다. selector는 외부 요청의 크기를 제한하기 위해 `MAX_CANDIDATES=4096`을 검사한다. 따라서 catalog가 4,097개이면 특정 agent에 grant가 거의 없어도 컴파일 전체가 실패한다.

**재현:** 한 서버에 `t0`부터 `t4096`까지 4,097개 도구와 리소스 1개를 저장한다. agent `a`와 ALLOW policy 1개만 선언하고 grant는 두지 않는다. `build_policy_bundle(agent='a')`은 두 백엔드에서 다음 오류를 낸다.

```text
ValueError: 4097 candidates exceed the 4096 limit
```

예상 동작은 전체 노출 도구에 대해 eligible/rejected 결정을 담는 bundle이다. 현재는 관련 없는 서버의 catalog 증가로도 기존 agent용 bundle 갱신이 막힌다. catalog 크기 자체를 제한하는 문서화된 별도 계약과도 연결되어 있지 않다.

**수정 방향:** 공개 selector 요청 한도는 유지하고 compiler 내부에서 bounded chunk로 평가한다. 모든 chunk는 하나의 strict graph-state bracket에 포함시키고, 도중 state 변경이면 전체를 재시도/실패 처리한다. catalog 정렬·중복 검증·digest 의미는 유지한다.

**회귀 기준:** 4,095/4,096/4,097개, 복수 chunk, chunk 중간 graph 변경, agent의 grant가 0개인 큰 catalog를 검사한다.

### R7. [P2] 기본 설정 손상이 명시적 설정과 CLI 복구 경로를 차단한다

**위치:** [config.py:82](../../toolgraph/config.py#L82), [cli.py:19](../../toolgraph/cli.py#L19), [cli.py:116](../../toolgraph/cli.py#L116).

`settings = Settings()`가 import 시점에 현재 디렉터리의 `.toolgraph/config.json`을 읽는다. CLI가 `--config`나 eager `--version`을 처리하기 전에 잘못된 기본 설정으로 import 자체가 실패한다.

**재현:** 임시 cwd에 `.toolgraph/config.json`의 내용을 `{broken`으로 저장하고 별도로 schema_version 1의 올바른 `valid.json`을 만든다. 새 프로세스로 아래 세 명령을 실행했다.

```text
toolgraph --version
toolgraph --help
toolgraph --config /absolute/path/to/valid.json --version
```

모두 exit 1, 빈 stdout, 기본 config의 JSON decode 오류 traceback을 반환했다. 명시적 CLI 설정이 환경/cwd보다 우선한다는 `configure()` 계약을 활용할 수 없다.

**수정 방향:** 설정 I/O와 검증을 명시적 CLI 초기화 단계로 늦춘다. 최종 선택된 설정 경로만 읽고, help/version은 backend 설정에 의존하지 않게 한다. import 가능한 모듈이라는 Python API 사용성도 함께 보존한다.

**회귀 기준:** 새 subprocess에서 손상된 기본 설정 + 유효한 explicit config, 손상된 `TOOLGRAPH_CONFIG` + explicit config, help/version, 명시적 설정 자체가 잘못된 경우의 간결한 오류를 검사한다.

### R8. [P2] 주입한 GraphReader와 graph-state bracket의 데이터 원천이 다르다

**위치:** [policy_bundle.py:79](../../toolgraph/policy_bundle.py#L79), [policy_bundle.py:124](../../toolgraph/policy_bundle.py#L124), [graph/store.py:15](../../toolgraph/graph/store.py#L15).

`build_policy_bundle(store=reader)`는 `reader.state()`, catalog, selector 결과를 사용하면서, 외부 bracket은 전역 `queries.with_graph_state()`에 맡긴다. 이 bracket은 주입된 reader가 아니라 현재 설정의 runtime DB를 조회한다.

**재현:** 실제 임시 Ladybug A를 초기화하고, 별도 reader B가 state `('injected-B', 99, 'b' * 64)`와 `s::t`의 일관된 catalog/eligible 결과를 제공하도록 했다. compiler의 출력은 다음처럼 섞였다.

```text
reader B state:       instance_id=injected-B, generation=99
runtime A state:      instance_id=f49dba48-5622-477e-8c89-01ed08e8ec27, generation=0
bundle graph_state:   instance_id=f49dba48-5622-477e-8c89-01ed08e8ec27, generation=0
bundle governance:    bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
```

runtime A에는 이 reader가 제공한 governance가 없어도 artifact가 생성된다. 현재 기본 CLI의 `RuntimeGraphStore` 경로에서는 데이터 원천이 같으므로 이 재현이 기본 CLI의 모든 bundle이 잘못되었다는 뜻은 아니다. 그러나 `store` 주입 API와 향후 backend adapter가 제공해야 하는 일관성은 깨져 있다.

**수정 방향:** before/after state 조회까지 동일한 reader에 위임한다. bracket 함수에 state reader를 주입하거나 store가 일관된 read operation을 제공하게 한다. 전역 runtime DB에 불필요하게 연결하지 않아야 한다.

**회귀 기준:** reader만으로 실행되는 독립 컴파일, 서로 다른 A/B state, B가 중간에 변하는 경우의 재시도/실패, runtime 설정이 없는 경우를 검사한다.

### R9. [P3] Preflight와 review-plan의 시각 검증이 다른 producer보다 약하다

**위치:** [preflight.py:87](../../toolgraph/preflight.py#L87), [policy_review.py:81](../../toolgraph/policy_review.py#L81), [tests/test_policy_review.py:86](../../tests/test_policy_review.py#L86), [pyproject.toml:61](../../pyproject.toml#L61).

두 API에 `created_at=datetime(2026, 9, 7)`을 넘기면 timezone 없는 `2026-09-07T00:00:00`을 그대로 출력한다. bundle/control-preflight는 이미 `rfc3339_offset_error()`로 같은 입력을 거부한다. 기본 CLI는 UTC clock을 쓰므로 이번 문제는 custom clock을 전달하는 Python caller에 한정된다.

부가적으로 현재 테스트 환경에는 `FormatChecker().checkers`의 `date-time` 항목이 없다. 따라서 임시 probe에서 FormatChecker를 넘겼어도 이 값의 format 오류가 보고되지 않았다. 기존 관련 계약 테스트도 format checker를 생략하거나 `None`으로 둔다. **이 경우 schema validation 성공은 timestamp format 검증의 증거가 아니다.** 필요한 extra와 checker가 있어야 한다는 점은 [jsonschema 공식 문서](https://python-jsonschema.readthedocs.io/en/stable/validate/#validating-formats)에 명시되어 있다.

**수정 방향:** 모든 artifact producer에 기존 공통 시각 검사를 적용한다. dev 환경에 format 검증 의존성을 명시하고 checker가 실제 활성화되었는지 assertion을 둔다.

**회귀 기준:** naive datetime, utcoffset가 None인 tzinfo, 초 단위 offset, 정상 UTC/분 단위 offset을 실제 producer와 format validator 모두에 넣는다.

## 5. 관점별 평가와 개선 제안

다음 항목은 R1–R9와 별도로 분류한 설계·확장성 평가다. 근거 없이 추가 결함으로 세지 않았다.

| 관점 | 평가 | 다음 개선 |
| --- | --- | --- |
| 도메인·아키텍처 | advisory와 enforcement 경계는 명확하다. GraphReader는 일부 정책 서비스의 port이며 query 계층 전체를 대체하지는 않는다. | R8 해결 후 state/read 경계를 같은 adapter로 수렴시킨다. |
| 정책 정확성 | unknown/ambiguous/drift/exception 의미가 명시적이다. 배치 audit의 cross-agent exception 격리는 추가 Ladybug 검사도 통과했다. | R4의 키 충돌과 R2 identity 보존을 같은 수준의 회귀 기준으로 만든다. |
| 데이터 일관성 | 성공 mutation과 generation 변경의 트랜잭션 결합은 강점이다. | backend 공통 시나리오에 빈 집합과 마지막 항목 삭제를 포함한다. |
| 보안·개인정보 | YAML duplicate key·unknown field 거부, 읽기 크기 제한, endpoint/진단 redaction, private artifact 저장이 있다. | R2/R3를 함께 해결해 identity 보존과 evidence redaction을 필드별 계약으로 만든다. |
| 성능 | selector 쿼리 수는 bounded batch지만 ingest 검증의 참조 해석은 여전히 참조별 query다. | 동일 tool ref를 캐시·배치 resolve하고, 한 트랜잭션의 의미는 유지한다. |
| 유지보수성 | 실패 원인·의도된 tradeoff를 설명하는 주석과 ADR이 풍부하다. CLI 약 948줄, queries 약 964줄에 책임이 모인다. | 무조건 파일을 나누기보다 artifact 검증/redaction과 backend read 경계를 먼저 통합한다. |
| 테스트 | 실제 Neo4j 통합 테스트와 계약 fixture, 배포 보안 회귀 검사가 있다. | 핵심 policy 시나리오를 두 backend로 parameterize하고 schema/runtime differential tests를 추가한다. |
| 설치·사용성 | 배포 wheel에서 quickstart가 독립 실행된다. source checkout 없이 example 생성도 성공했다. | R1 때문에 일반 MCP catalog로 확장할 때 깨질 수 있고, R7 때문에 설정 복구가 어렵다. |
| CI·릴리스 | SHA-pinned actions, required job 집계, runtime/extras audit, wheel 재빌드 검사가 있다. | 설정 파일의 존재와 원격 설정의 실제 enforcement는 별도 증거로 유지한다. |

### 5.1 작은 합성 데이터의 성능 관측

도구 N개, agent 1개, 도구당 grant 1개, ALLOW policy 1개를 사용했다. 실제 DB transaction/session에 계수 wrapper를 두어 `.run()` 호출을 세었다. query 결과를 mock하지 않았다. 각 크기에서 한 번 측정했으며, ingest 시간에는 해당 write transaction을, rank 시간에는 `rank_features()`를 포함한다. crawl 및 schema init은 제외한다.

| Backend | 도구 수 | ingest 쿼리 수 | ingest ms | rank 쿼리 수 | rank ms |
| --- | ---: | ---: | ---: | ---: | ---: |
| Ladybug | 10 | 22 | 14.76 | 5 | 8.46 |
| Ladybug | 100 | 112 | 32.83 | 5 | 8.56 |
| Ladybug | 500 | 512 | 111.92 | 5 | 12.26 |
| Neo4j | 10 | 22 | 68.42 | 5 | 194.73 |
| Neo4j | 100 | 112 | 143.23 | 5 | 158.73 |
| Neo4j | 500 | 512 | 409.30 | 5 | 164.42 |

이 fixture에서 ingest는 **N+12**, qualified 후보의 rank는 **5회**였다. ingest Phase 2의 배치화가 Phase 1의 grant별 `_resolve()`를 제거하지 않았기 때문이다. 같은 도구가 grants/data_access/governed_by/exceptions에 반복되어도 별도 resolve가 발생할 수 있다. 최적화의 다음 대상은 transaction 분할보다 **동일 transaction 내 참조 일괄 해석**이다.

수치는 성능 보증이나 두 엔진의 일반적인 우열을 뜻하지 않는다. 한 번의 cold/warm 혼합 실행이고, Neo4j는 Docker·Bolt·초기 query planning과 notification 비용을 포함한다. sustained load, 다중 writer, 원격 DB 지연, peak RSS는 측정하지 않았다. 모든 크기에서 반환된 rank row 수는 입력 N과 같았다.

### 5.2 의도된 제약으로 유지할 항목

- **전체 manifest 교체 transaction:** 부분 적용으로 DENY를 지우는 위험을 피하기 위한 선택이다. 크기 부담만을 이유로 commit을 여러 번 나누는 변경은 권하지 않는다.
- **Ladybug single-owner:** 공유 서비스·다중 프로세스 요구는 Neo4j와 분리되어 있다. single-owner 자체를 결함으로 세지 않았다.
- **reset의 유지보수 창:** batched deletion은 완전한 원자적 reset이 아니며 중간 장애 후 재실행이 필요하다고 CLI docstring이 명시한다. 이번 검증은 정상 완료 후 state 변경을 확인한 것이며 동시 실행 안전성을 증명하지 않는다.
- **MCP live read의 retry exhaustion fallback:** ADR-0004는 가용성을 위해 마지막 결과에 최신 state를 붙이는 동작을 명시한다. persisted artifact의 strict bracket과 같은 일관성 보증으로 해석하면 안 된다. 소비자 문서에 이 차이를 계속 드러내는 것이 좋다.
- **실행 강제·자동 정책 변경 부재:** 제품 경계다. 이를 메우기 위해 Toolgraph에 별도 runtime을 넣는 개선은 이번 발견 사항의 해결책이 아니다.
- **빈 manifest/fleet의 삭제 의미:** ADR-0007에 명시된 declarative semantics다. R1은 그 의미를 Ladybug가 수행하지 못하는 구현 문제다.
- **review sidecar lock 파일 유지:** unlink race 방지를 위한 명시적 tradeoff다. 빈 `.lock` 파일을 자동 삭제하는 cleanup을 권하지 않는다.

## 6. 검증 결과

| 검사 | 결과 | 증거·한계 |
| --- | --- | --- |
| `.venv/bin/ruff check .` | PASS | All checks passed |
| `.venv/bin/python -m pytest -q --tb=short` | PASS | **414 passed, 3 warnings, 36.22s**, skip 없음 |
| 두 백엔드 추가 probe | 결함 재현 완료 | R1은 차이, R2/R3/R6는 동일 결과; R4는 같은 키 충돌과 다른 잔존 예외 |
| 거부된 ingest 상태 보존 | PASS | 두 백엔드에서 GraphState 변화 없음 |
| 완료된 reset state | PASS | 두 백엔드에서 instance ID 변경, generation 0 |
| Ladybug 배치 audit 추가 실행 | PASS | 기존 `test_audit_report_matches_the_per_agent_composition_exactly`, `test_batched_audit_never_leaks_one_agents_exception_to_another`를 임시 Ladybug에서 직접 실행 |
| control-plan 변형 비교 | 불일치 5개 확인 | loader 승인 / JSON Schema 거부 |
| `uv lock --check` | PASS | 88 packages resolved, lockfile 변경 없음 |
| `uv build --out-dir <tmp>/dist` | PASS | wheel + sdist 생성 |
| `.venv/bin/python -m twine check <tmp>/dist/*` | PASS | wheel/sdist metadata 모두 통과 |
| `bash scripts/verify-artifacts.sh <tmp>/dist 0.0.1` | PASS | sdist 48개 허용 항목, 같은 환경 wheel 재빌드 digest 일치 |
| `scripts/audit-dependencies.sh runtime` | PASS | 조회 시점에 알려진 취약점 발견 없음 |
| `scripts/audit-dependencies.sh extras` | PASS | Ladybug 포함; 조회 시점에 알려진 취약점 발견 없음 |
| 독립 wheel 설치·quickstart | PASS | example init → init → crawl → strict-drift ingest → review bundle |
| 생성 bundle 파일 권한 | PASS | `0600` |
| 외부 연동·원격 CI | NOT_RUN | 합의된 범위 밖 |

첫 전체 테스트 시도는 sandbox의 Docker socket 접근 제한으로 중단했다. 권한을 확보한 재실행 결과가 위의 414 passed이며, 초기 setup error들을 제품 테스트 실패로 집계하지 않았다. uv cache 접근 제한으로 중단된 검사도 권한을 확보해 재실행했다.

기존 테스트의 경고 3개는 testcontainers의 deprecated wait API 2종과 Pydantic settings의 `lifespan` forward reference 경고다. 실제 실패로 관측되지는 않았지만 향후 의존성 변경 시 확인할 유지보수 항목이다.

독립 설치는 lockfile 환경과 다르다. built wheel의 허용 dependency 범위로 설치했으며 **MCP 1.29.1, Neo4j driver 6.3.0, Pydantic 2.13.5, Ladybug 0.18.3**이 선택되었다. 그 환경의 quickstart는 서버 1개·도구 2개·리소스 1개를 저장했고 bundle에 eligible 1개/rejected 1개, generation 2가 생성되었다. 전체 테스트를 이 새 dependency 조합으로 다시 실행한 것은 아니다.

동일 환경에서 재빌드가 일치한 wheel의 SHA-256:

```text
31bb4ec52e68801049ba8998f5e05c3245c65e4a0b2accd26e0b4be524e6324a
```

이는 같은 환경의 sdist→wheel 재현성 근거이며 다른 OS·Python·시간에서 같은 bytes를 보증하지 않는다. 의존성 audit 통과 역시 애플리케이션의 모든 보안 결함 부재를 뜻하지 않는다.

## 7. 수정·검증 권장 순서

1. **기본 로컬 경로 복구 — R1:** 빈 batch의 upsert를 처리하고 두 backend 공통 nonempty→empty lifecycle 테스트를 먼저 추가한다.
2. **산출물 identity와 redaction 정리 — R2/R3:** identity를 보존하면서 evidence만 scrub하는 공통 경계를 만들고 실제 저장 JSON을 검사한다.
3. **판정·원본 계약 일치 — R4/R5:** 예외 키를 충돌 없게 만들고 원본 control-plan의 승인 집합을 Schema와 맞춘다.
4. **큰 catalog와 adapter 일관성 — R6/R8:** bounded evaluation을 하나의 reader/state bracket 아래 실행한다. 기본 runtime과 injected reader 경로를 모두 검증한다.
5. **복구·시각 일관성 — R7/R9:** CLI 설정 로딩 시점을 정리하고 공통 timestamp 검사 및 실제 format 검증을 적용한다.
6. **성능 후속:** manifest 전체 transaction을 유지하면서 Phase 1의 중복 참조 해석을 줄인다. 대표 데이터에서 query count와 지연을 다시 측정한다.

각 수정은 해당 최소 재현을 먼저 회귀 테스트로 고정한 다음 전체 기존 테스트를 통과시켜야 한다. 새 결함이 수정되었다는 근거 없이 이 보고서의 검증 결과를 릴리스 승인으로 사용하면 안 된다.

## 8. 증거 위치와 검증하지 않은 영역

재현용 임시 파일은 `/private/tmp/toolgraph-review-20260907/`에 두었다. `probe.py`, `ladybug-probes.json`, `neo4j-probes.json`은 R1–R6/R9와 작은 성능 관측의 입력·출력을 담고, `dist/`, `artifact-venv/`, `quickstart/`는 패키지 검증에 사용했다. 이 디렉터리는 휘발성 보조 증거다. 판단에 필요한 입력·오류·수치와 코드 위치는 본문에도 기록했다. R7은 별도 subprocess, R8은 별도 임시 Ladybug/reader probe로 검증했다.

다음 영역은 미검증이므로 성공을 주장하지 않는다.

- 실제 STM·SyncMill·Tracegraph 프로세스와의 end-to-end 연동 및 runtime enforcement.
- 원격 GitHub CI 실행 결과, branch protection, 환경 승인자, PyPI publisher의 실제 설정과 publication.
- macOS 외 플랫폼 및 Python 3.13/3.14에서의 이번 HEAD 실행. 저장소 CI matrix의 존재는 검토했지만 원격 성공 증거는 조회하지 않았다.
- 실제 고객 catalog 규모, 장시간 부하, 다중 writer 스트레스, crash/power-loss fault injection, peak memory 측정.
- 전체 Git history의 secret scan, 외부 MCP 서버의 신뢰성·동작 의미, 운영 인증/인가 배포 구성.

관련 이전 설계 맥락은 로컬 메모리에서 참고했으며, advisory 경계와 dependency group 사용 방식은 현재 README·CONTRIBUTING·ADR에서 다시 확인했다. 이전 PR/CI 상태를 현재 상태로 전용하지 않았다.
