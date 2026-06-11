# toolgraph 입문 가이드

이 문서는 toolgraph를 처음 보는 사용자가 로컬에서 데모를 실행하고,
결과를 읽고, 예제 거버넌스 파일을 조금씩 바꿔 보는 데 필요한 최소
흐름을 설명합니다.

toolgraph는 MCP 도구를 실행 중에 막는 게이트웨이가 아닙니다. 대신
MCP 서버가 어떤 도구를 노출하는지 그래프로 적재하고, 운영자가
작성한 권한과 데이터 접근 규칙을 합쳐서 "어떤 에이전트가 어떤
민감 자원에 닿을 수 있는지"를 설명 가능한 경로로 보여주는 분석
도구입니다.

## 먼저 알아둘 개념

- `MCPServer`: 도구와 리소스를 제공하는 MCP 서버입니다.
- `Tool`: MCP 서버가 노출하는 호출 가능한 기능입니다.
- `Resource`: 파일, 메모리 네임스페이스, API 데이터처럼 도구가 읽거나
  쓰는 대상입니다.
- `Agent`: 도구를 호출할 수 있는 주체입니다. 예제에서는 `public-bot`,
  `ops-agent` 같은 이름을 씁니다.
- `Policy`: 리소스나 도구에 붙는 정책입니다. 예제에서는
  `secrets-deny`가 비밀 파일 접근을 DENY로 표시합니다.
- `CAN_CALL`: 에이전트가 도구를 호출할 수 있다는 운영자 작성 권한입니다.
- `READS` / `WRITES`: 도구가 어떤 리소스를 읽거나 쓰는지 나타내는
  운영자 작성 데이터 흐름입니다.
- `GOVERNED_BY`: 리소스나 도구가 어떤 정책의 적용을 받는지 나타냅니다.

핵심 질문은 다음처럼 읽으면 됩니다.

```text
Agent -> Tool -> Resource -> Policy
```

예를 들어 `public-bot -> read_file -> file:///private/secrets.env -> secrets-deny`
경로가 있으면, `public-bot`이 호출 가능한 도구를 통해 DENY 정책이 붙은
비밀 파일에 닿는다는 뜻입니다.

## 준비물

- Python 3.12 이상
- Docker 또는 Docker Desktop
- `uv`
- Node.js와 `npx`

기본 데모는 `examples/servers.yaml`에 있는 filesystem MCP 서버를
`npx`로 실행합니다. 첫 실행 때 npm 패키지를 내려받을 수 있어 네트워크가
필요할 수 있습니다.

## 1. 설치와 Neo4j 실행

```bash
uv sync --extra dev
cp .env.example .env
docker compose up -d --wait
```

`.env.example`의 기본 Neo4j 접속값은 `docker-compose.yml`과 맞춰져
있습니다. 별도 Neo4j를 쓰는 경우에만 `.env`를 수정하면 됩니다.

연결이 되는지 확인합니다.

```bash
uv run toolgraph check
uv run toolgraph init-schema
```

정상이라면 `OK: connected to ...`와 제약 조건 목록이 출력됩니다.

## 2. 데모 한 번에 실행하기

가장 빠른 확인 방법은 데모 스크립트입니다.

```bash
bash scripts/demo.sh
```

스크립트는 다음 순서로 동작합니다.

1. 기존 그래프 데이터를 지웁니다.
2. `examples/servers.yaml`의 MCP 서버를 크롤링합니다.
3. `examples/governance.yaml`의 운영자 작성 규칙을 적재합니다.
4. 세 가지 질문을 실행합니다.

## 3. 같은 흐름을 직접 실행하기

각 단계를 직접 실행하면 toolgraph가 어디에서 어떤 정보를 얻는지
보기 쉽습니다.

```bash
uv run toolgraph reset --yes
uv run toolgraph crawl --servers examples/servers.yaml
uv run toolgraph ingest-manifest --governance examples/governance.yaml
```

여기서 `crawl`은 MCP 서버가 실제로 노출하는 도구와 리소스를 그래프에
넣습니다. `ingest-manifest`는 운영자가 작성한 에이전트, 정책, 권한,
데이터 접근 규칙을 그래프에 넣습니다.

## 4. 결과 읽어 보기

`public-bot`이 호출할 수 있으면서 DENY 정책이 붙은 리소스에 닿는
도구를 찾습니다.

```bash
uv run toolgraph unsafe-tools public-bot
```

예제에서는 `public-bot`에게 `filesystem::read_file` 권한이 있고,
`read_file`은 `file:///private/secrets.env`를 읽는다고 선언되어 있으며,
그 파일은 `secrets-deny` 정책의 적용을 받습니다. 그래서 결과에는
대략 다음 정보가 들어갑니다.

- `tool`: 문제가 되는 도구 이름
- `resource`: 도구가 닿는 리소스
- `policy`: 적용된 DENY 정책
- `classification`: 운영자가 예외로 선언하지 않은 경우 `violation`
- `path`: 그래프 경로
- `provenance`: 이 판단에 사용된 엣지의 출처 정보

특정 에이전트가 특정 도구를 호출할 수 있는지도 확인할 수 있습니다.

```bash
uv run toolgraph check-access public-bot read_file
```

중요한 필드는 `verdict`입니다.

- `ALLOW`: 호출 권한이 있고 DENY 정책 경로가 없습니다.
- `DENY`: 호출 권한은 있지만 DENY 정책 경로가 있습니다.
- `NOT_GRANTED`: 에이전트가 그 도구를 호출할 권한이 없습니다.
- `AGENT_NOT_FOUND`: 에이전트 이름이 그래프에 없습니다.
- `TOOL_NOT_FOUND`: 도구 이름이 그래프에 없습니다.
- `AMBIGUOUS_TOOL`: 같은 bare tool 이름이 여러 서버에 있어 하나로
  고를 수 없습니다.

리소스가 바뀌면 어떤 에이전트와 도구가 영향을 받는지도 볼 수 있습니다.

```bash
uv run toolgraph blast-radius "file:///private/secrets.env"
```

`found: false`가 나오면 해당 리소스나 정책 노드를 그래프에서 찾지
못했다는 뜻입니다. 실제로 영향이 없는 경우와 오타를 구분하기 위해
이 값을 먼저 확인하는 습관이 좋습니다.

## 5. 예제 YAML 바꿔 보기

기본 규칙은 `examples/governance.yaml`에 있습니다.

```yaml
agents:
  - ops-agent
  - public-bot

policies:
  - id: secrets-deny
    effect: DENY
    scope: secrets

grants:
  - {agent: public-bot, tool: "filesystem::read_file", granted_by: platform-team}

data_access:
  - {tool: "filesystem::read_file", resource: "file:///private/secrets.env", mode: READS}

governed_by:
  "file:///private/secrets.env": [secrets-deny]
```

처음에는 다음처럼 작은 변경부터 해 보는 것이 좋습니다.

- `public-bot`의 `filesystem::read_file` grant를 지우고 다시
  `unsafe-tools public-bot`을 실행합니다.
- `data_access`의 리소스를 다른 URI로 바꾼 뒤 `check-access` 결과가
  어떻게 달라지는지 봅니다.
- 새 `Policy`를 추가하고 `governed_by`에 연결해 봅니다.

변경 후에는 항상 다시 적재해야 합니다.

```bash
uv run toolgraph ingest-manifest --governance examples/governance.yaml
```

manifest에 오타가 있으면 ingest는 전체 적용을 거부하고 그래프를
이전 상태로 둡니다. 예를 들어 없는 도구나 없는 정책을 참조하면
경고가 나오고 `REJECTED`로 끝납니다.

## 6. 비어 있는 부분 찾기

toolgraph는 "위험한 경로"만 보여주는 것이 아니라, 아직 작성되지 않았거나
근거가 부족한 부분도 찾을 수 있습니다.

```bash
uv run toolgraph unmapped-tools
uv run toolgraph orphan-policies
uv run toolgraph unbacked-edges
uv run toolgraph drift
```

- `unmapped-tools`: 호출 권한은 있지만 `READS` / `WRITES`가 아직
  작성되지 않은 도구입니다.
- `orphan-policies`: 어떤 에이전트도 현재 닿지 않는 정책입니다.
- `unbacked-edges`: 증거 문자열이 없는 운영자 작성 엣지입니다.
- `drift`: 거버넌스에는 남아 있지만 현재 크롤링된 MCP 서버가 더는
  노출하지 않는 도구입니다.

입문 단계에서는 `unsafe-tools`, `check-access`, `blast-radius`를 먼저
익힌 뒤 이 네 가지 감사 명령으로 범위를 넓히면 됩니다.

## 자주 막히는 지점

`docker compose up -d --wait`가 실패하면 Docker가 실행 중인지 먼저
확인합니다.

`crawl` 단계에서 `npx` 관련 오류가 나면 Node.js 설치와 네트워크 접근을
확인합니다.

`AGENT_NOT_FOUND`가 나오면 `examples/governance.yaml`의 `agents:`나
`grants:`에 같은 이름이 있는지 확인합니다.

`TOOL_NOT_FOUND`가 나오면 `crawl`이 성공했는지, 도구 이름이
`filesystem::read_file`처럼 서버 prefix를 포함해야 하는 상황인지
확인합니다.

`DENY`가 나오는 것은 런타임 차단이 아닙니다. toolgraph는 분석 결과를
설명해 주는 도구이며, 실제 호출 차단은 별도 gateway나 MCP proxy에서
처리해야 합니다.
