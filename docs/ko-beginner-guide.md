# toolgraph 입문 가이드

처음 사용하는 사람이 설치부터 실제 정책 번들 하나를 확인하는 과정까지
안내합니다. 패키지에 Ladybug 그래프와 MCP fixture가 포함되어 있으므로
저장소 clone, Docker, Node.js가 필요하지 않습니다.

영어판: [`docs/beginner-guide.md`](beginner-guide.md)

같은 과정을 한 단계씩 실행하며 출력을 바로 보려면 영어 노트북
[`docs/notebooks/01-first-policy-bundle.ipynb`](notebooks/01-first-policy-bundle.ipynb)를
여세요.

Toolgraph는 런타임 프록시가 아니라 정책 분석기이자 컴파일러입니다. MCP
도구 계약을 크롤링하고 운영자가 작성한 권한·데이터 흐름을 결합해
`Agent -> Tool -> Resource -> Policy` 경로를 설명합니다. 실제 호출 차단은
생성된 JSON 번들을 읽는 memtomem-stm 같은 gateway가 담당합니다.

## 준비물

- Python 3.12, 3.13 또는 3.14
- [`uv`](https://docs.astral.sh/uv/)

## 1. 설치하고 quickstart 만들기

```bash
uv tool install "toolgraph[ladybug]"
toolgraph example init toolgraph-quickstart
cd toolgraph-quickstart
toolgraph init
```

`.toolgraph/config.json`과 로컬 Ladybug DB가 생성됩니다. Toolgraph 명령이
이 DB를 순차적으로 소유하며 gateway는 DB를 직접 열지 않습니다.

다음 단계로 가기 전에 설정을 점검합니다.

```bash
toolgraph doctor
```

`doctor`는 설정 파일, DB 연결, 스키마, 그래프 상태 네 가지를 확인합니다.
`init` 직후에는 네 항목이 모두 `PASS`이고 마지막 줄이
`Overall status: HEALTHY`입니다. `Configuration` 항목의 `backend_source`는
백엔드 선택이 어디서 왔는지 알려 주며, 여기서는 `config-file`입니다.
`FAIL`이 하나라도 있으면 `doctor`는 1로 종료합니다.
[자주 막히는 지점](#자주-막히는-지점)을 보세요. `toolgraph doctor --json`은
같은 보고서를 JSON으로 출력합니다.

## 2. 오프라인 MCP fixture 크롤링

```bash
toolgraph crawl --servers servers.yaml
```

`servers.yaml`은 fleet 전체를 선언합니다. `servers` 키는 필수이며,
`servers: []`는 "서버 없음"을 뜻해 기본값으로는 크롤링된 서버를 모두
퇴역시킵니다(`--no-prune`이면 유지). 운영자가 작성한 거버넌스는 크롤링으로
삭제되지 않습니다. 빈 파일이나 키가 없는 파일은 서버에 접속하기 전에
거부됩니다.

fixture는 두 도구를 노출합니다.

- `policy-gateway::read_note`: 읽기 전용이며 idempotent인 도구
- `policy-gateway::publish_note`: 정책이 적용된 draft 영역에 쓰는 도구

## 3. 운영자 정책 적재

먼저 미리 봅니다. `--dry-run`은 manifest를 그래프와 대조해 무엇이 바뀔지
출력할 뿐 아무것도 쓰지 않습니다.

```bash
toolgraph ingest-manifest \
  --governance governance.yaml \
  --strict-drift \
  --dry-run
```

`DRY-RUN: manifest at governance.yaml would apply cleanly`와 함께 추가될
변경 요약(agent 1, 정책 1, grant 2, 데이터 접근 edge 2, 정책 binding 1)이
출력됩니다. 거부될 manifest라면 `DRY-RUN REJECTED`와 이유를 출력하고 1로
종료하며, 도구 이름 오타가 흔한 원인입니다. 확인했으면 실제로 적재합니다.

```bash
toolgraph ingest-manifest \
  --governance governance.yaml \
  --strict-drift
```

예제는 `vibe-coder`에게 두 도구의 호출 권한을 주지만 draft 리소스에는
DENY 정책을 적용합니다. 작성한 모든 edge에 `provenance` 근거 포인터가 있어서
`toolgraph unbacked-edges` 결과가 비어 있습니다. `data_access` 항목 하나에서
`provenance` 블록을 지우고 `--dry-run`으로 다시 실행하면 변경 요약에
`Provenance modified: 1`이 보입니다. `--dry-run` 없이 다시 적재한 뒤
`unbacked-edges`를 실행하면 그 edge가 목록에 나타납니다. 계속하기 전에 블록을
되돌리고 다시 적재하세요. 이 가이드의 나머지는 원래 manifest를 전제로 합니다.
grant는 `unbacked-edges --include-grants`로 따로 감사합니다.

결과가 비어 있다는 것은 모든 edge에 포인터가 있다는 뜻이지 그 주장이 사실이라는
뜻이 아닙니다. demo 도구는 문자열만 반환하고 실제 파일을 건드리지 않으므로
READS/WRITES 항목은 예제용이며 evidence에 그렇게 적혀 있습니다. 포인터를 따라가
확인하는 것은 검토자의 몫이고, 포인터를 요구하는 이유가 바로 그것입니다.

## 4. 판정 확인

```bash
toolgraph eligible-tools vibe-coder \
  policy-gateway::read_note policy-gateway::publish_note \
  --profile review
toolgraph selection-explain vibe-coder policy-gateway::publish_note \
  --profile review
```

`read_note`는 eligible이고 `publish_note`는 정책 경로와 함께 rejected로
표시됩니다. 이 단계에서 Toolgraph는 판정을 설명할 뿐 호출을 가로채지
않습니다.

## 5. gateway 번들 생성

```bash
toolgraph policy compile \
  --agent vibe-coder \
  --profile review \
  --output .toolgraph/policy-bundle.json
```

성공하면 번들 자체가 아니라 번들을 설명하는 JSON(출력 경로, 정확한 byte
digest, 그래프 instance/generation, eligible/rejected 개수)이 표시됩니다. 번들은 canonical UTF-8
JSON이며 private 권한과 atomic replacement로 저장됩니다.

첫 적용은 `review`가 안전합니다. reference gateway는 rejected 도구를 계속
보이게 두고 would-block 호출을 기록합니다. 판정을 확인한 뒤 같은 정책을
`strict` profile로 다시 컴파일하면 rejected 도구가 숨겨지고 차단됩니다.

여기까지가 단독으로 완결되는 quickstart입니다. 아래는 모두 선택 사항입니다.

## 6. (선택) memtomem-stm에서 적용

Toolgraph는 호출을 직접 막지 않으므로 DENY를 실제 차단으로 만들려면
gateway가 필요합니다. reference gateway는 별도 패키지이며 Toolgraph를
쓰는 데 필수는 아닙니다.
[memtomem-stm Toolgraph 정책 gateway 가이드](https://github.com/memtomem/memtomem-stm/blob/main/docs/guides/toolgraph-policy-gateway.md)를
따라 이 번들을 STM에 연결하고 `mms gateway status`, `explain`으로 확인한
뒤 Codex 또는 Claude Code에 등록합니다.

STM upstream 이름은 Toolgraph가 크롤링한 이름과 같게 두는 것이 가장
간단합니다. 이름이 다르면 STM의 `server_name_map`을 명시해야 합니다.

## 다음 실험과 감사 명령

```bash
toolgraph unsafe-tools vibe-coder
toolgraph unmapped-tools
toolgraph unbacked-edges
toolgraph drift
toolgraph blast-radius draft-publish-deny
```

이 중 둘은 결과가 나오는 것이 정상입니다. `unsafe-tools`는 `publish_note`가
`draft-publish-deny`에 닿는 것을 violation으로 보고하고, `blast-radius`는 그
정책이 덮는 경로를 보고합니다. 그 도달 범위를 짚어 주는 것이 두 명령의 목적이므로
이 예제에서는 결과가 비어 있지 않은 쪽이 맞습니다. 나머지 셋은 비어 있습니다.

`read_note` grant를 제거하거나 draft 정책 binding을 바꾼 뒤 다시 적재·컴파일해
보세요. 컴파일 전에는 항상 `ingest-manifest`를 먼저 다시 실행하세요. 잘못된 manifest는 이전 그래프 상태를 유지한 채 거부되고, 컴파일
실패도 마지막 정상 번들을 덮어쓰지 않습니다.

## 팀·공유 환경

Ladybug는 로컬 단일 프로세스 기본값입니다. 여러 프로세스나 운영자가
그래프를 공유해야 할 때 Neo4j 5.7 이상을 사용합니다. 이 부분은 저장소가 필요합니다.
`docker-compose.yml`과 `.env.example`은 패키지나 생성된 quickstart가 아니라
clone에만 들어 있습니다. Neo4j driver는 `neo4j` extra이므로 첫 명령으로
Toolgraph를 그 extra와 함께 다시 설치합니다.

```bash
uv tool install "toolgraph[neo4j]"
git clone https://github.com/memtomem/toolgraph
cd toolgraph
docker compose up -d --wait
cp .env.example .env
toolgraph init --backend neo4j
```

compose 설정은 알려진 개발용 비밀번호와 loopback 포트를 사용하므로 운영용
인증 구성이 아닙니다. 저장소의 demo script는 임시 Ladybug DB만 사용하며,
공개 서버 demo는 Node.js, `npx`, 네트워크가 필요할 수 있습니다.

fleet 설정에서는 모든 서버에 고유한 `name`을 지정하세요. 이름 없는 stdio
진단에는 command 인자와 credential이 로그에 남지 않도록 실행 파일명만
표시됩니다(예: `stdio:npx`).

## 자주 막히는 지점

먼저 `toolgraph doctor`를 실행하고 `Configuration` 아래의 `backend_source`
줄을 확인하세요.

- `backend_source: default`이고 백엔드 `neo4j`에 대한 `Connectivity`가 FAIL:
  설정 파일도 환경 변수도 백엔드를 고르지 않아 Neo4j로 넘어간 것입니다.
  `Configuration` 항목에 `exists: False`도 보이면 quickstart 디렉터리 밖에
  있거나 `toolgraph init`을 건너뛴 것입니다. 디렉터리로 `cd`하거나 `init`을
  실행하세요.
- `Configuration` 항목이 `conflicting backend configuration`으로
  FAIL: 셸이나 이 디렉터리의 `.env`에 설정된 `TOOLGRAPH_BACKEND` 또는
  `TOOLGRAPH_DB_PATH`가 `.toolgraph/config.json`과 다릅니다. 메시지에 변수
  이름이 나옵니다. (`backend_source`는 `TOOLGRAPH_BACKEND`가 설정된 경우에만
  `env`로 표시됩니다.) 셸에서 해제하거나 `.env`에서 지우세요. 다음 명령이
  `.env`를 다시 읽기 때문입니다. 또는 명령 앞에
  `--config .toolgraph/config.json`을 붙이세요
  (`toolgraph --config .toolgraph/config.json doctor`). 그때까지 `crawl`,
  `eligible-tools` 같은 명령은 같은 메시지로 멈춥니다.
- `Schema Integrity`가 FAIL: `toolgraph init-schema`를 실행하세요. 누락된
  테이블이나 constraint를 만들고, 스키마 버전이 없는 DB에는 버전을 기록합니다.
  `backend schema version N does not match this build's M`으로 거부하면 다른
  Toolgraph 버전이 만든 DB입니다. DB는 바뀌지 않았으니 그 버전을 쓰거나 새
  DB를 사용하세요.
- `DRY-RUN REJECTED` 또는 `REJECTED`: 경고가 문제를 알려 주며 그래프는 바뀌지
  않았습니다.
- `AGENT_NOT_FOUND`: 같은 agent가 `agents`와 관련 grant에 있어야 합니다.
- `TOOL_NOT_FOUND`: crawl을 다시 실행하고 `server::tool` 이름을 사용합니다.
- `DRIFTED`: governance가 최신 crawl에 없는 도구를 참조하고 있습니다.
- governance 상태나 graph identity가 없으면 번들을 생성하지 않습니다.
- `DENY`가 실제 호출 차단이 되는 시점은 gateway가 번들을 적용한 뒤입니다.

## 정책 변경 영향 미리보기

별도 파일로 수정한 정책이 어떤 도구의 판정을 바꾸는지 적용 전에 비교할 수 있습니다.

```sh
toolgraph policy impact proposed.yaml policy-gateway::read_note policy-gateway::publish_note \
  --agent vibe-coder --profile review --format json --output impact.json
toolgraph policy check impact.json
```

`impact`는 실제 DB를 바꾸지 않습니다. `check`는 JSON의 **제안 정책 판정**을
현재 DB와 비교하므로, 제안 정책을 아직 적용하지 않았다면 실패할 수 있습니다.
필요한 manifest 적용은 별도 `ingest-manifest` 작업입니다. 선택한 범위만 비교하며
미해결 참조가 있는 보고서는 회귀 기대값으로 사용할 수 없습니다. 그래프 reset 또는
선택 도구의 catalog 변경 뒤에는 보고서를 다시 생성·검토해야 합니다.
[전체 계약과 사용 예제](policy-impact.md)를 참고하세요.
