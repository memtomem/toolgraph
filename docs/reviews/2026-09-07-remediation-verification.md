# 구현 리뷰 개선 완료 및 검증 보고서

- 날짜: 2026-09-07
- 기준 커밋: `45b594872cf0d94635aa26db8a7f6fe6464159ae`
- 대상: 해당 커밋 이후의 로컬 작업 트리 변경
- 원본: [종합 구현 리뷰](2026-09-07-implementation-review.md). 원본 발견 사항은 수정하지 않고 이 문서에서 해결 상태를 추적한다.

## 결과

R1–R9를 구현하고 신규 회귀 테스트 82개를 추가했다. 기존 414개를 포함한 전체 **496개 테스트가 통과**했다. Neo4j와 Ladybug를 실제로 사용하는 공통 fixture에서 빈 목록, 정책 선택, 모호성·드리프트, 감사 결과, 예외 충돌과 재수집, 롤백을 검증했다. 로컬 검증 결과이며 원격 CI나 외부 소비자 통합 완료를 의미하지 않는다.

## 발견 사항별 조치와 증거

| 항목 | 상태 | 구현 | 회귀 검증 |
|---|---|---|---|
| R1 빈 목록 | 해결 | `graph/loader.py`, `manifest/ingest.py`에서 빈 struct 목록의 upsert만 생략. 오래된 관계 정리, 권한 회수와 generation 갱신은 유지 | 두 백엔드에서 tools/resources/policies의 8가지 조합, 비어 있지 않은 상태에서 빈 상태로 전환, 권한 회수 및 mutation 이후 실패 시 manifest 롤백 |
| R2 식별자 변형 | 해결 | `artifact_safety.py`의 필드별 처리. agent/principal/run_id/candidate/tool_key 및 모호한 후보 목록을 보존하고 credential 형태는 거부 | URI query/fragment가 다른 도구의 상반된 결정을 두 DB에서 유지. preflight 파일 readback, control principal 보존, 입력 credential의 DB 접근 전 거부 |
| R3 review-plan 정보 노출 | 해결 | `policy_review.py` 출력의 `current_paths`에 공통 evidence redaction 적용 | 실제 CLI가 기록한 JSON의 resource, provenance evidence, exception reason에 URI credential이 남지 않음 |
| R4 예외 키 충돌 | 해결 | `v2:` + canonical JSON tuple의 SHA-256. resource null을 별도 값으로 표현. logical exception 중복은 UNWIND 전에 마지막 항목으로 확정 | 구분자 충돌 tuple의 예외 두 개 유지, 마지막 reason 선택, null과 문자열 구분, legacy key를 가진 노드의 재수집 |
| R5 control-plan 계약 불일치 | 해결 | strict 모델 및 JSON 정수 정규화. `maximum_cycles` 필수. optional field의 명시적 null 거부. bool 숫자 취급 차단 | 누락·문자열 숫자·boolean 숫자·boolean schema version·null bound·문자열 boolean·null edge 변이 거부. `1.0`, `0.0`, `2.0` 허용 및 정규화. 기존 additive/major/topology 테스트 유지 |
| R6 catalog 상한 | 해결 | 정렬·중복 확인한 catalog를 최대 4,096개씩 평가. 모든 chunk는 하나의 strict bracket에 포함. evaluation coverage를 정확히 확인 | 0/4,095/4,096/4,097개. 마지막 chunk에서 state 변경 시 전체 재시도. 5회 모두 변경 시 실패. 누락·중복·상반된 결정 및 feature 누락·중복 거부. 실패 시 기존 output 보존 |
| R7 설정 복구 차단 | 해결 | `config.settings`를 첫 접근 시 만드는 lazy singleton으로 변경. 명시적 `--config` 우선 선택. 설정 오류 타입 및 CLI 오류 처리 추가 | 손상된 기본/환경 설정에서 별도 프로세스의 help/version 및 유효한 explicit config 성공. 손상된 explicit config는 traceback 없이 실패 |
| R8 reader 상태 원천 | 해결 | `with_graph_state(..., state_reader=...)`의 모든 읽기에 동일 reader 사용. compiler가 `reader.state` 전달. optional `EvaluatingGraphReader` protocol 분리 | global state에 접근하면 실패하는 주입 reader만으로 bundle 생성. evaluate 없는 기존 adapter fallback 성공 |
| R9 시각 검증 | 해결 | preflight/review-plan에서 기존 RFC 3339 offset helper를 I/O 전에 적용. `jsonschema[format-nongpl]` 개발 의존성 및 잠금 갱신 | naive, utcoffset=None, 30초 offset 거부. UTC 및 분 단위 offset은 실제 date-time checker로 출력 schema 검증. fixture가 checker 설치 여부를 명시적으로 확인 |

코드는 저장소의 `toolgraph/` 아래에 있다. 신규 사례는 [test_review_remediation.py](../../tests/test_review_remediation.py), 공통 fixture는 [conftest.py](../../tests/conftest.py)에 있다. 기존 backend 전용 테스트는 보존했다. 공통 fixture는 Ladybug가 없으면 skip하지 않고 실패한다.

## 검증 실행 결과

| 검사 | 결과 |
|---|---|
| `.venv/bin/python -m ruff check .` | 통과 |
| `git diff --check` | 통과 |
| `.venv/bin/python -m pytest tests/test_review_remediation.py -q --tb=short` | 82 passed, 20.21초 |
| `.venv/bin/python -m pytest -q --tb=short` | 496 passed, 3 warnings, 54.05초; skip 없음 |
| `uv lock --check` | 101개 패키지, 잠금 일치 |
| `./scripts/audit-dependencies.sh runtime` | 알려진 취약점 없음 |
| `./scripts/audit-dependencies.sh extras` | 알려진 취약점 없음 |
| `uv build --out-dir /private/tmp/toolgraph-remediation-20260907/dist` | wheel 및 sdist 생성 성공 |
| `python -m twine check <dist files>` | 두 배포 파일 PASSED |
| `bash scripts/verify-artifacts.sh /private/tmp/toolgraph-remediation-20260907/dist` | sdist 49 entries allowlist 통과, wheel 재빌드 바이트 일치 |
| 새 가상환경의 wheel 설치 및 quickstart | init → crawl → strict ingest → selection → policy compile 모두 exit 0 |

최종 wheel SHA-256:

```text
64e7cb2357fc7d70f3c092d675da55f6b4985ab04ff68b994cd911b02197d47b
```

테스트 환경은 Python 3.12.11, Neo4j `5.26-community`, Ladybug 0.18.3이다. 새 wheel 설치 환경은 저장소 editable install을 사용하지 않았으며, 허용된 의존성 범위에서 MCP 1.29.1, Neo4j driver 6.3.0, Pydantic 2.13.5를 설치했다. quickstart 결과는 `read_note=eligible`, `publish_note=rejected`이며 결과 bundle 권한은 `0600`이다.

임시 배포물·독립 설치 환경·quickstart 실행 증거:

```text
/private/tmp/toolgraph-remediation-20260907/dist/
/private/tmp/toolgraph-remediation-20260907/venv/
/private/tmp/toolgraph-remediation-20260907/quickstart-results.json
/private/tmp/toolgraph-remediation-20260907/quickstart/.toolgraph/policy-bundle.json
```

세 경고는 기존 testcontainers의 deprecated wait API 두 건과 pydantic-settings의 lifespan forward reference 한 건이다. 최초 sandbox 테스트는 Docker 소켓 접근 제한으로 중단되어 권한을 확장해 재실행했다. 개발 중 발생한 테스트 준비 오류 두 가지(존재하지 않는 `python -m toolgraph` 진입점, Ladybug primary key SET 사용)는 실제 CLI 진입점 및 legacy node 생성 방식으로 수정했다. 위 최종 결과에는 해당 실패가 남아 있지 않다.

## 호환성과 적용 절차

1. JSON Schema v1 파일, reason vocabulary, 기존 golden fixture 및 정상 입력의 digest 계약을 유지했다. credential 형태의 식별자, 누락된 필수 필드, 강제 형변환에 의존하던 잘못된 입력은 이제 실패한다.
2. GraphReader 구현에 `evaluate`를 추가할 필요는 없다. 기존 `state`, `exposed_tool_contracts`, `eligible_tools`, `rank_features` 구현을 그대로 사용할 수 있다.
3. 물리 DB schema version 변경이나 데이터베이스 reset은 필요하지 않다. 조회는 ExpectedException의 key 대신 agent/tool/resource/policy 속성을 사용하므로 이전 키의 노드도 읽는다.
4. 기존 설치에서는 원본 governance manifest를 재수집해 예외 키를 다시 만들고, 결과를 확인한 다음 policy bundle을 재컴파일한다. 예전 키 충돌로 사라진 행은 원본 manifest 없이 복구할 수 없다.

예시(설정·매니페스트·agent·출력 경로는 해당 설치의 값을 사용):

```bash
toolgraph --config <runtime-config.json> ingest-manifest \
  --governance <governance.yaml> --strict-drift
toolgraph --config <runtime-config.json> policy compile \
  --agent <agent-id> --profile review --output <policy-bundle.json>
```

사용자 운영 DB에는 재수집이나 migration을 실행하지 않았다. 변경된 입력 계약과 적용 안내는 [CHANGELOG.md](../../CHANGELOG.md)에도 기록했다.

## 범위의 한계

이번 작업은 리뷰의 R1–R9 및 관련 회귀 검증이다. ingest reference N+1 성능 최적화, 대규모 실제 DB 성능 측정, SyncMill/memtomem-stm 등 외부 소비자의 실환경 통합, 원격 CI, 배포는 포함하지 않는다. Toolgraph의 advisory 판정과 외부 실행·강제 적용의 역할 구분은 유지했다. 커밋·push·PR은 수행하지 않았다.
