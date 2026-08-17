# 공개 전환 체크리스트

**상태:** 준비 진행 중. 공개 여부는 아직 결정하지 않았다.
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
- **fork PR 위험 없음.** 워크플로우에 `pull_request_target` / `workflow_run`이
  없고, 릴리스는 태그 push에서만 돌며 PyPI는 하드코딩 토큰이 아니라 OIDC
  (`id-token: write`) + `pypi` environment를 쓴다. 공개 후 외부 fork PR이
  시크릿에 닿을 경로가 없다.

## 전환 직전에 할 일

1. **히스토리 이메일 재작성.** 기존 커밋에는 `ontofinance@gmail.com`(60건),
   `pandasdataanalysis@gmail.com`(12건)이 남아 있다. 지금 하지 않는 이유는
   두 가지다 — 모든 SHA가 바뀌어 **열린 PR이 깨지고**, 전환까지 쌓이는 커밋
   때문에 **어차피 다시 해야 한다**. 열린 PR을 모두 정리한 뒤 실행할 것:

   ```bash
   # mailmap 방식: 이름/이메일 매핑을 파일로 두고 한 번에 치환
   cat > /tmp/mailmap <<'EOF'
   memtomem <118508877+memtomem@users.noreply.github.com> <ontofinance@gmail.com>
   memtomem <118508877+memtomem@users.noreply.github.com> <pandasdataanalysis@gmail.com>
   EOF
   uvx git-filter-repo --mailmap /tmp/mailmap    # 백업 클론에서 먼저 검증할 것
   git push --force-with-lease --all && git push --force-with-lease --tags
   ```

   실행 전 반드시 별도 클론에서 리허설하고, 다른 작업 트리·클론이 없는지
   확인한다. `--force-with-lease`는 남의 push를 덮어쓰지 않기 위한 것이다.

2. **재검증.** 재작성 후 위 "완료된 항목"의 스캔을 한 번 더 돌린다. 특히
   전환까지 추가된 커밋이 새로운 노출을 들여왔는지 확인:

   ```bash
   git grep -I -n -E 'sk-ant-|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|BEGIN (RSA|OPENSSH|EC) PRIVATE KEY' $(git rev-list --all)
   git log --all --pretty=format: --name-only --diff-filter=A | sort -u | grep -iE '\.env$|secret|\.pem$|\.key$'
   ```

3. **저장소 설정.** 공개 직후 활성화: secret scanning + push protection,
   Dependabot alerts, `main` 브랜치 보호(필수 체크 = CI). 공개 저장소는
   기본값이 비공개와 다르므로 전환 후 한 번 훑어야 한다.

4. **CI 확인.** 공개 후에는 표준 러너가 무료이므로 결제와 무관하게 돌아야
   한다. 현재 로컬에서만 검증된 **Windows / Python 3.13·3.14 경로**를 이때
   처음으로 실제 확인하게 된다.

## 결정하지 않은 것

공개 여부 자체. 되돌리기 어렵고 히스토리 전체가 드러나는 결정이라 저장소
소유자가 직접 판단하고 직접 실행한다.
