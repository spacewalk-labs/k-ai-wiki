# k-ai-wiki — 내 두 번째 뇌, LLM 이 대신 써 주는 개인 위키

> **[K-AI PRO] 대한민국 AI 전사 육성과정** 부속 키트입니다.
> 홈서버 위에 **내 장기기억**을 세웁니다. 내용은 비어 있습니다 — 채우는 것은 여러분과 에이전트입니다.

## 이게 뭔가요

노션도 옵시디언도 결국 **내가 써야** 유지됩니다. 그래서 대부분 3개월 안에 죽습니다.

이 위키는 반대로 만들어졌습니다. **여러분은 자료를 고르고 질문만 하고, 정리·요약·연결·색인은
전부 AI 에이전트가 합니다.** Andrej Karpathy 가 제안한 *LLM Wiki* 패턴을 개인용으로 구현한 것입니다.

> *"You never (or rarely) write the wiki yourself — the LLM writes and maintains all of it."*

여러분이 하는 일은 셋뿐입니다.

| 이렇게 말하면 | 에이전트가 |
|---|---|
| **`/wiki-seed`** | 인터뷰로 **머릿속에만 있는 판단**을 끌어내 위키에 심습니다 |
| **`/wiki-upload`** | 손에 든 자료를 3문 인터뷰를 거쳐 증거 폴더에 넣습니다 |
| **`/gardener`** | 쌓인 증거를 **지식 페이지로 승급**하고, 위키를 건강검진합니다 |

**스킬 3개가 이 레포 안에 들어 있습니다.** 클론하면 따라옵니다 — 따로 설치할 게 없습니다.

---

## 시작하기 — 4단계

### 0. 준비물

- **1강에서 만든 홈서버** (Docker 가 도는 리눅스 박스)
- **Python 3.10 이상** 과 패키지 두 개 (`boto3`·`pyyaml`)
- Claude Code (또는 같은 방식으로 스킬을 읽는 에이전트)

```bash
python3 --version          # 3.10 이상이어야 합니다
pip install boto3 pyyaml
```

> ⚠️ **`error: externally-managed-environment` 가 뜨면** 잘못한 게 아닙니다.
> 요즘 우분투·데비안은 시스템 파이썬에 직접 설치하는 걸 막습니다. 셋 중 하나로 가세요.
>
> ```bash
> # ① 배포판 패키지로 (가장 간단)
> sudo apt install -y python3-boto3 python3-yaml
>
> # ② 이 폴더 전용 가상환경으로 (가장 깔끔 — 다음부터 python3 대신 .venv/bin/python 을 씁니다)
> python3 -m venv .venv && .venv/bin/pip install boto3 pyyaml
>
> # ③ 그냥 밀어붙이기 (빠르지만 시스템 파이썬을 건드립니다)
> pip install --break-system-packages boto3 pyyaml
> ```

### 1. 클론하고 **내 레포로** 만들기

```bash
git clone <이 레포 주소> my-wiki
cd my-wiki

# 여기가 중요합니다 — 남의 히스토리를 끊고 내 위키로 다시 시작합니다
rm -rf .git
git init -b main
git add . && git commit -m "내 위키 시작"
```

> 🔴 **GitHub 에 올릴 때는 반드시 Private 으로 만드세요.**
> 개인 위키에는 판단·금액·관계가 쌓입니다. 나중에 지워도 **git 히스토리에는 남습니다.**
> 처음부터 private 이 유일하게 안전한 선택입니다.

### 2. 무거운 파일 저장소(MinIO) 올리기

PDF·엑셀·사진 같은 무거운 원본은 git 에 넣으면 안 됩니다(레포가 부풀고, 100MB 제한에 걸리고,
diff 가 의미 없습니다). **텍스트는 git 에, 무거운 원본은 오브젝트 스토리지에** 둡니다.

홈서버에서 아래 한 장이면 끝납니다.

```yaml
# ~/minio/docker-compose.yml
services:
  minio:
    image: quay.io/minio/minio:latest
    command: server /data --console-address ":9001"
    environment:
      MINIO_ROOT_USER: <내가-정한-아이디>
      MINIO_ROOT_PASSWORD: <내가-정한-비밀번호-8자이상>
    volumes:
      - ./data:/data
    ports:
      # 앞의 127.0.0.1 이 핵심입니다 — 이 기기 안에서만 열립니다
      - "127.0.0.1:9000:9000"    # API — 이 위키가 쓰는 포트
      - "127.0.0.1:9001:9001"    # 웹 콘솔 — 브라우저로 파일을 눈으로 볼 때
    restart: unless-stopped
```

```bash
cd ~/minio && docker compose up -d
```

> 🔒 **`127.0.0.1:` 을 빼지 마세요.** 그냥 `"9000:9000"` 으로 두면 **같은 공유기에 붙은 아무 기기나**
> 여러분의 저장소에 닿습니다. 다른 기기에서 쓰고 싶으면 포트를 여는 게 아니라
> **Tailscale 로 그 홈서버에 들어와서** 씁니다. 공유기 포트포워딩은 하지 않습니다.

> 🔑 **위 `MINIO_ROOT_*` 는 관리자 계정입니다 — 위키에 그대로 쓰지 마세요.**
> 콘솔(`http://localhost:9001`)에 로그인해 **Access Keys → Create** 로 키를 하나 더 만들고,
> 다음 단계에는 **그 키**를 넣으세요. 그래야 `.env` 가 새더라도 저장소 전체의 관리자 권한까지
> 넘어가지 않습니다.

### 3. 첫 실행

```bash
python3 engine/scripts/init.py
```

MinIO 주소와 키를 묻고, `.env` 를 만들고(git 에 안 올라갑니다), 버킷을 만들고,
**실제로 파일을 넣었다 빼 보고** 연결을 확인합니다. 여기서 초록이 나오면 준비 끝입니다.

### 4. 에이전트에게 첫 마디

이 폴더에서 Claude Code 를 열고:

```
/wiki-seed
```

인터뷰가 시작되고, 그 답으로 **`wiki-vault/나.md`** 가 채워집니다.
**이게 위키에서 제일 중요한 문서입니다** — 나머지 모든 페이지가 "이걸 어디에 붙일지"를
여기 적힌 맥락으로 판단하기 때문입니다.

> 한 번에 다 채우려 하지 마세요. 첫 세션은 **한 절**만 제대로 채워도 성공입니다.

---

## 폴더 구조

```
AGENTS.md            운영 계약 — 에이전트가 지키는 규칙의 정본 (= CLAUDE.md)
README.md            이 파일

wiki-vault/          ★ 여기 안이 지식입니다. 옵시디언은 이 폴더를 여세요
  나.md                내 맥락 매뉴얼 — 판단의 기준
  index.md             카탈로그 (자동 생성)
  log.md               타임라인
  gaps.md              "증거가 아직 없는 주장" 목록 = 다음에 모을 것
  raw/                 불변 증거 — 넣은 뒤에는 고치지 않습니다
    ingest/              투입한 자료   ·   interviews/  인터뷰 전문
    assets/              작은 스크린샷 (무거운 원본은 여기가 아니라 MinIO)
  wiki/                에이전트가 쓰고 유지하는 지식 페이지

engine/scripts/      도구 (vault 밖이라 옵시디언에 안 보입니다)
  init.py              첫 실행
  build_index.py       색인 생성
  wiki_lint.py         기계 검사
  assets_upload.py     무거운 원본을 MinIO 로

.claude/skills/      스킬 3개 — 클론하면 따라옵니다
```

**옵시디언은 레포 루트가 아니라 `wiki-vault/` 를 vault 로 여세요.** 그래야 도구와 설정 파일이
검색·그래프에 섞이지 않습니다.

---

## 자주 막히는 곳

| 증상 | 원인·해결 |
|---|---|
| `error: externally-managed-environment` | 0단계의 ①②③ 중 하나로 (README 맨 위) |
| `ModuleNotFoundError: No module named 'yaml'` 또는 `'boto3'` | 같은 원인 — 0단계의 설치가 안 끝났습니다 |
| `자격증명이 주입되지 않았습니다` | `set -a; . ./.env; set +a` 를 먼저. `.env` 가 없으면 `init.py` |
| `❌ MinIO 에 닿지 않습니다` | 주소·포트 오타이거나 MinIO 가 안 떠 있습니다 (`docker compose ps`). 다른 기기면 Tailscale 확인 |
| `❌ 자격증명이 거부됐습니다` | `.env` 의 키가 틀렸습니다. **`.env` 를 지우고 `init.py` 를 다시** 돌리세요 |
| `❌ 쓰지 않았습니다 — 설명이 N건 사라집니다` | 손으로 쓴 설명이 지워질 상황이라 **일부러 멈춘 것**입니다. 함께 출력된 항목명을 확인하고, 정말 재생성이 맞으면 `--force` |
| 위키가 안 쌓임 | 정상입니다. **`/wiki-upload` 를 부르지 않으면 아무것도 안 들어옵니다** — 자동 수집은 없습니다 |

> 🔧 **`.env` 를 손으로 고칠 때는 값을 작은따옴표로 감싸세요** (`S3_SECRET_ACCESS_KEY='se$cret'`).
> 감싸지 않으면 비밀번호 속 `$`·공백을 셸이 먹어 버려서, **점검은 통과하는데 업로드만 실패하는**
> 헷갈리는 상태가 됩니다. `init.py` 가 만든 `.env` 는 이미 감싸져 있습니다.

---

## 🔴 백업 — 이것만은 오늘 해 두세요

이 위키는 시간이 지날수록 **다시 만들 수 없는 것**이 됩니다. 그런데 기본 상태로는
**홈서버 디스크 한 장**에만 존재합니다.

- **MinIO 의 versioning 은 백업이 아닙니다.** 같은 디스크 안의 이전 버전일 뿐이라,
  디스크가 죽으면 원본과 함께 죽습니다. 실수로 덮어썼을 때 되살리는 용도입니다.
- **위키 본문(git)** — GitHub 에 **Private** 레포를 만들어 `git push` 해 두세요.
  이것 하나로 텍스트 전부가 다른 곳에 복제됩니다.
  ```bash
  git remote add origin git@github.com:<내계정>/<내위키>.git
  git push -u origin main
  ```
- **무거운 원본(MinIO)** — `~/minio/data` 폴더를 주기적으로 외장 디스크나 다른 기기로
  복사해 두세요. 여기까지 하면 디스크가 죽어도 잃는 게 없습니다.

## 왜 이렇게 만들었나 (읽으면 더 잘 쓰게 되는 것들)

- **넣기 전에 3가지를 묻습니다** — 왜 캡처했나 · 내 관점은 · 어떻게 써먹을 건가.
  이게 이 위키가 "지식의 무덤"이 되지 않는 유일한 장치입니다. 답이 안 나오는 자료는 넣지 않습니다.
- **증거는 고치지 않습니다.** `raw/` 는 추가만 합니다. 판단이 바뀌면 지우는 게 아니라
  *"~였으나 언제 뒤집힘"* 으로 남깁니다.
- **출처 없는 문장은 쓰지 않습니다.** 모든 지식 페이지에 `sources` 가 붙습니다.
- **폴더를 미리 나누지 않습니다.** 내용이 쌓이기 전의 분류는 추측입니다. 150 페이지쯤에서
  실제로 뭉친 대로 나눕니다.

자세한 규칙은 **[AGENTS.md](AGENTS.md)** 에 있습니다 — 에이전트가 읽는 문서지만,
사람이 읽어도 그대로 말이 됩니다.
