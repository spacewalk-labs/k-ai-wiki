# AGENTS.md — 개인 위키 운영 계약

이 파일이 유일한 레포 규칙 정본이다. `CLAUDE.md`는 이 파일을 가리키는 상대 symlink 또는
`@AGENTS.md` 한 줄이어야 한다. 사용법과 도구 계약은 `README.md`와 `engine/README.md`가 소유한다.

## 경계와 라우팅

- `wiki-vault/`만 지식이다. `engine/`은 도구이며 vault 안으로 옮기거나 지식 검색·승급 대상에
  섞지 않는다. vault 경로는 모두 `wiki-vault/` 기준으로 해석한다.
- 자료 투입은 `/wiki-upload`, 머릿속 판단과 `나.md` 시딩은 `/wiki-seed`, `raw/` 승급과 위키
  점검은 `/gardener`를 따른다. 세 스킬은 `.claude/skills/`의 제품 기능이다.

## 증거 보존

- `raw/`는 투입 후 수정·삭제하지 않고 새 스냅샷만 추가한다. 지식의 주장은 출처를 가져야 하며,
  문서를 갱신할 때 `sources`를 누적한다. 뒤집힌 주장은 지우지 말고 변경 사실을 기록한다.
- 무거운 원본은 버리거나 git에 넣지 말고 MinIO에 보존한다. git에는 검색용 정제본과 원본의
  `s3://` 키·해시·출처 포인터를 남긴다. 세부 적재 계약은 `/wiki-upload`와
  `engine/scripts/assets_upload.py`를 따른다.

## 안전한 실행과 완료

- 성공은 실제 검증 결과로만 판단한다. 첫 설정은 `engine/scripts/init.py`의 읽기·쓰기 검증까지 통과해야 한다.
  자산 업로더는 기본 dry-run, sha256 검증, 키 충돌 중단, 검증된 중복만 명시적 prune하는 안전장치를
  우회하지 않는다. stale 자료는 자동 삭제하지 않는다.
- `index.md`는 생성물이다. 마커 영역을 손으로 고치지 말고 `engine/scripts/build_index.py`로 재생성한다. 설명이
  손실되는 쓰기는 기본 거부하며, 확인 없이 `--force`하지 않는다.
- 변경을 마치기 전에 관련 실행 검증과 함께 `python3 engine/scripts/build_index.py --check`,
  `python3 engine/scripts/wiki_lint.py --json`을 통과시킨다.
