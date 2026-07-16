# toolgraph 입문 가이드

처음 사용하는 사람이 설치부터 실제 정책 번들 하나를 확인하는 과정까지
안내합니다. 패키지에 Ladybug 그래프와 MCP fixture가 포함되어 있으므로
저장소 clone, Docker, Node.js가 필요하지 않습니다.

영어판: [`docs/beginner-guide.md`](beginner-guide.md)

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

## 2. 오프라인 MCP fixture 크롤링

```bash
toolgraph crawl --servers servers.yaml
```

fixture는 두 도구를 노출합니다.

- `policy-gateway::read_note`: 읽기 전용이며 idempotent인 도구
- `policy-gateway::publish_note`: 정책이 적용된 draft 영역에 쓰는 도구

## 3. 운영자 정책 적재

```bash
toolgraph ingest-manifest \
  --governance governance.yaml \
  --strict-drift
```

예제는 `vibe-coder`에게 두 도구의 호출 권한을 주지만 draft 리소스에는
DENY 정책을 적용합니다. 작성한 모든 edge에는 근거 포인터가 있습니다.

## 4. 판정 확인

```bash
toolgraph eligible-tools vibe-coder \
  policy-gateway::read_note policy-gateway::publish_note \
  --profile review
toolgraph selection-explain vibe-coder policy-gateway::publish_note
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

성공하면 출력 경로, 정확한 byte digest, 그래프 instance/generation,
eligible/rejected 개수가 JSON으로 표시됩니다. 번들은 canonical UTF-8
JSON이며 private 권한과 atomic replacement로 저장됩니다.

첫 적용은 `review`가 안전합니다. reference gateway는 rejected 도구를 계속
보이게 두고 would-block 호출을 기록합니다. 판정을 확인한 뒤 같은 정책을
`strict` profile로 다시 컴파일하면 rejected 도구가 숨겨지고 차단됩니다.

## 6. memtomem-stm에서 적용

Toolgraph와 gateway는 서로 독립된 패키지입니다.
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

`read_note` grant를 제거하거나 draft 정책 binding을 바꾼 뒤 다시 적재·컴파일해
보세요. 잘못된 manifest는 이전 그래프 상태를 유지한 채 거부되고, 컴파일
실패도 마지막 정상 번들을 덮어쓰지 않습니다.

## 팀·공유 환경

Ladybug는 로컬 단일 프로세스 기본값입니다. 여러 프로세스나 운영자가
그래프를 공유해야 할 때 Neo4j를 사용합니다.

```bash
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

- `AGENT_NOT_FOUND`: 같은 agent가 `agents`와 관련 grant에 있어야 합니다.
- `TOOL_NOT_FOUND`: crawl을 다시 실행하고 `server::tool` 이름을 사용합니다.
- `DRIFTED`: governance가 최신 crawl에 없는 도구를 참조하고 있습니다.
- governance 상태나 graph identity가 없으면 번들을 생성하지 않습니다.
- `DENY`가 실제 호출 차단이 되는 시점은 gateway가 번들을 적용한 뒤입니다.
