# 공개 전환 체크리스트

**상태:** 전처리 진행 중(2026-08-23). 공개 여부는 아직 결정하지 않았다.
남은 블로커는 이슈 #63·#65·#67과 PR #60·#70·#71로 추적한다.
**목적:** private → public 전환 시점에 해야 할 일을 한 곳에 모아, 결정과 실행을
분리한다. 지금 해두면 좋은 것은 이미 했고, 전환 직전에 해야 의미가 있는 것만
아래 "전환 직전"에 남겨 두었다.

## 왜 이 문서가 있는가

toolgraph는 현재 비공개다. 부수 효과로 **GitHub Actions가 유료 분(minutes)을
소모**하므로, 계정 결제가 막히면 CI job이 시작조차 못 한다(2026-08-15~,
"The job was not started because recent account payments have failed").
같은 계정의 공개 저장소(memtomem, memtomem-stm)는 표준 러너가 무료라 정상
동작한다. 즉 **공개 전환은 CI 비용 문제를 자동으로 해소**하지만, 그것을 이유로
공개를 결정해서는 안 된다. 아래 노출 항목을 먼저 정리한 뒤 판단할 문제다.

## 완료된 항목 (2026-08-18)

- **실제 시크릿 없음.** 전체 히스토리 124커밋의 블롭을 훑어 `sk-ant-` / `ghp_` /
  `AKIA` / `AIza` / `xox*-` / `BEGIN * PRIVATE KEY` 패턴을 검사했고 해당 없음.
  커밋된 적 있는 민감 파일명은 `.env.example` 하나뿐이며 실제 `.env`는 없다.
  로컬 절대경로(`/Users/...`)와 사설 IP·내부 호스트도 문서에 없다.
- **자격증명 형태 문자열은 전부 테스트용 sentinel.** `alice:secret@example.test`,
  `hunter2` 등은 redaction 계약을 검증하는 값이므로 그대로 둔다. 지우면
  유출 방지 기능의 회귀 테스트가 사라진다.
- **비공개 저장소 하드링크 제거.** `docs/ecosystem-integration-plan.md`의 정본
  링크 3건과 companion PR 링크 1건을 텍스트로 바꿨다. 공개 독자에게 404가 되고
  비공개 저장소의 내부 문서 경로·PR 번호를 드러내던 유일한 경로였다.
  형제 프로젝트 **이름(syncmill/tracegraph)은 유지**한다 — 아키텍처 설명에
  필수이고, 이름 언급 자체는 노출로 보지 않기로 결정했다.
- **로컬 개발 비밀번호 정리.** `docker-compose.yml`이 `NEO4J_PASSWORD`를 따르도록
  바꾸고 "not a secret" 라벨을 달았다. 부수적으로 실제 버그도 고쳤다: 이전에는
  compose가 비밀번호를 하드코딩해서 `.env`에서 바꾸면 DB와 클라이언트가
  어긋나 인증이 실패했다.
- **신규 커밋의 이메일 노출 차단.** 저장소 로컬 `git config`를
  `memtomem <118508877+memtomem@users.noreply.github.com>`으로 설정했다.
  전역 설정은 건드리지 않았으므로 다른 저장소는 영향 없다.
- **fork PR 경로 점검.** 릴리스는 태그 push에서만 돌며 PyPI는 하드코딩 토큰이
  아니라 OIDC(`id-token: write`) + `pypi` environment를 쓴다.
  **2026-08-23 정정:** CLA 워크플로(PR #70)를 추가하면서 저장소 최초의
  `pull_request_target`이 생겼고 `PERSONAL_ACCESS_TOKEN`을 쓴다. fork PR의
  코드를 실행하지 않도록 `ref: default_branch` + `persist-credentials: false`로
  체크아웃하며, memtomem에서 운용 중인 것과 동일한 패턴이다. 이 워크플로를
  수정할 때 **PR 브랜치의 코드를 체크아웃하거나 실행하지 않는다**는 전제를
  깨면 안 된다.
- **공개 저장소 기준 정렬 (memtomem 동일 기준, PR #70/#71).** `LICENSE`는
  memtomem과 **바이트 단위로 동일**하고(`diff` 무출력) `pyproject.toml`의
  authors 형식도 같다. 여기에 맞춰 `CLA.md`(ASF ICLA + 미래 라이선싱 §4),
  `.github/workflows/cla.yml`, `.github/cla-check.py`(verbatim),
  `CONTRIBUTING.md`를 추가했다. 서명은 저장소별로 분리되어
  `cla-signatures` 브랜치의 `signatures/v1/cla.json`에 쌓인다.
  memtomem에 `NOTICE`가 없으므로 toolgraph에도 두지 않는다.
  vendoring한 `contracts/tracegraph/`는 sdist에 실려 나가므로 해당 위치에
  저작권·라이선스를 명시했다(같은 소유자 `memtomem/tracegraph`이므로 제3자
  자료가 아님).

## 전환 직전에 할 일

1. **히스토리 이메일 재작성 — 하지 않기로 결정(2026-08-23).**
   기존 커밋에는 개인 주소 두 개가 author/committer로 남아 있다. 재작성하지
   않는 이유는 **공개 형제 저장소 memtomem이 같은 기준이기 때문**이다:
   memtomem은 이미 공개 상태이며 동일한 기여자 신원(`Steve Oh`,
   `Pandas Studio`)이 히스토리에 그대로 있고 한 번도 재작성된 적이 없다.
   DAPADA는 같은 저작권 줄 아래 이미 같은 판단을 공개적으로 내렸다.

   기술적으로도 재작성은 약속을 지키지 못한다. force-push를 해도 옛 커밋은
   `refs/pull/*/head`와 직접 SHA URL로 계속 도달 가능하며, GitHub Support는
   자격증명급 민감 데이터가 아니면 정리를 거절할 수 있다. 즉 모든 SHA를
   바꾸고 닫힌 PR의 diff를 깨뜨리면서도 결과는 보장되지 않는다.

   **대신 한 것:** 신규 커밋은 이미 noreply 주소를 쓰고(저장소 로컬
   `git config`), 이 문서에서 평문 주소와 실행 가능한 mailmap을 제거했다.
   나중에 방침이 바뀌면 mailmap은 추적되지 않는 로컬 파일로 작성하되
   **이메일만 치환하고 이름은 보존**해야 한다 — 원래 안은 이름까지
   `memtomem`으로 덮어써 attribution을 지웠다.

2. **재검증.** "완료된 항목"의 스캔을 한 번 더 돌린다. 2026-08-18 이후 추가된
   커밋이 새 노출을 들여왔는지 확인:

   ```bash
   git grep -I -n -E 'sk-ant-|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|BEGIN (RSA|OPENSSH|EC) PRIVATE KEY' $(git rev-list --all)
   git log --all --pretty=format: --name-only --diff-filter=A | sort -u | grep -iE '\.env$|secret|\.pem$|\.key$'
   ```

   **이 스캔의 한계를 알고 쓸 것.** Git 객체 안의 좁은 토큰 패턴·파일명만
   본다. 따라서 "시크릿 없음"은 *추적된 파일 기준*의 주장이다.

3. **Git 밖의 GitHub 표면 감사.** 공개 시점에 **Actions 로그와 아티팩트가 함께
   공개**되고, 비공개 fork는 분리된다. `docs/releasing.md`도 이미 이 단계를
   요구한다. 대상: 워크플로 실행 로그, 아티팩트, PR·이슈 코멘트와 첨부,
   environments, 저장소 설정, 모든 ref.

   - 아티팩트 25건은 확인 완료(2026-08-23): `policy-bundle-gateway-evidence`
     하나뿐이고 내용은 다이제스트·SHA·불리언 요약이라 노출 위험 없음.
   - **실행 로그 감사 완료(2026-08-23): 깨끗함.** 전체 178건 중 31건은
     결제 실패로 시작조차 못 해(steps=0) 로그가 없고, 실제 실행된 147건 중
     42건(≈29%)을 내려받아 6.7MB를 전수 검사했다. 표본은 실행된 실패 런,
     가장 오래된 런(2026-06-11~12), `release/v0.1.0-hardening`,
     형제 저장소 연동 브랜치, 정책 번들 smoke 성공 런, Dependabot 런을
     모두 포함한다. 자격증명 패턴·개인 이메일·로컬 절대경로·사설
     호스트/IP **전부 0건**이며, 로그에 이메일 형식 문자열 자체가 없다.
     `***` 마스킹(런당 3~36회)은 전부 GitHub이 토큰을 정상 가린 것이다.
     `syncmill`/`tracegraph`는 ruff/mypy가 되울린 **이 저장소 자신의
     소스 줄** 안에서 이름으로만 등장하며(경로·SHA·URL·설정값 없음),
     그 파일들은 어차피 공개된다. smoke가 체크아웃하는 `memtomem-stm`은
     이미 공개 저장소다. `release.yml`은 **실행 이력이 0건**이다.
   - 미포함: 실행된 런 147건 중 105건(같은 커밋의 근사 중복 CI·일일 cron),
     런 제목·job summary, PR/이슈 코멘트와 첨부.
   - fork 0건, watcher 0건.

4. **저장소 설정.** 공개 직후 활성화: secret scanning + push protection,
   Dependabot alerts, code scanning, **private vulnerability reporting**
   (`SECURITY.md`가 이미 이 채널을 약속하지만 아직 꺼져 있는 별도 설정이다),
   `main` 브랜치 보호. 필수 체크는 **matrix에서 파생된 이름을 쓰지 말 것** —
   `artifact-smoke (ubuntu-latest, 3.14)` 같은 이름은 matrix를 바꾸는 순간
   고아가 되어 PR이 영구 대기한다. 안정적인 집계 job 하나를 쓴다(이슈 #67).
   브랜치 보호는 **비공개 상태에서는 설정 자체가 불가**하다(API 403,
   Pro 또는 공개 필요).

   CLA 워크플로가 동작하려면 추가로 필요한 것: `PERSONAL_ACCESS_TOKEN`
   시크릿, `cla-signatures` 브랜치, `needs-cla` 라벨.

5. **CI 확인.** 공개 후에는 표준 러너가 무료이므로 결제와 무관하게 돌아야
   한다. 다만 이때 실제로 검증되는 **Windows / Python 3.13·3.14 경로는
   artifact 퀵스타트뿐**이다(`ci.yml`의 `artifact-smoke` matrix). Neo4j·
   testcontainers 전체 스위트는 여전히 Ubuntu 단일 job에서만 돈다.

6. **릴리스는 `docs/releasing.md`를 따른다.** 여기에 별도 절차를 쓰지 않는다.
   요지: TestPyPI와 PyPI **양쪽에** pending Trusted Publisher 생성(pending
   publisher는 이름을 선점하지 않는다) → 로컬 릴리스 게이트 → `test-v<VERSION>`
   → 깨끗한 환경에서 설치 검증 → 같은 커밋에 `v<VERSION>`. `pyproject.toml`이
   README를 패키지 메타데이터로 넣으므로 **README와 CHANGELOG는 태그 커밋
   시점에 이미 최종본**이어야 한다(이슈 #63).

## 결정하지 않은 것

공개 여부 자체. 되돌리기 어렵고 히스토리 전체가 드러나는 결정이라 저장소
소유자가 직접 판단하고 직접 실행한다.
