#!/usr/bin/env python3
"""개인 위키 기계적 검사.

기계가 확실히 아는 것만 잡는다. 판단이 필요한 것은 /gardener --lint 에 넘긴다.
계약·이유 = engine/README.md
"""
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("❌ pyyaml 이 없습니다. 설치 방법은 README '0. 준비물' 을 보세요.\n"
             "   보통은:  pip install boto3 pyyaml")

WIKI_LINK = re.compile(r"\[\[([^\]|#]+)")
FM = re.compile(r"\A---\n(.*?)\n---\n", re.S)
FENCE = re.compile(r"^\s*(```|~~~)", re.M)
INLINE_CODE = re.compile(r"`[^`]*`")

# wiki/ 페이지에 요구되는 frontmatter (gardener 스킬 계약)
REQUIRED_FM = ("updated", "tags", "sources")

# 위키 자체를 설명하는 발판 문서. 지식 페이지가 아니다 —
# 링크 문법을 예시로 쓰므로([[링크]]로 잇는다) 링크 검사에서도 제외한다.
SCAFFOLD = {"README.md", "index.md", "log.md", "gaps.md", "AGENTS.md", "CLAUDE.md"}

# 규칙으로 흉내내면 오탐이 나는 것들 — 사람/LLM 이 읽고 판정한다
LLM_ONLY = [
    "페이지 간 모순",
    "새 소스가 뒤집은 낡은 주장",
    "언급만 되고 페이지가 없는 개념",
    "누락된 상호참조",
    "메울 수 있는 데이터 공백 (→ gaps.md)",
]


def wiki_root() -> Path:
    default = Path(__file__).resolve().parents[2] / "wiki-vault"
    p = Path(os.environ.get("WIKI_ROOT", default))
    if not (p / "index.md").exists():
        sys.exit(f"위키 vault 를 찾을 수 없습니다: {p}  (WIKI_ROOT 로 지정하세요)")
    return p


def md_files(root: Path) -> list[Path]:
    out = []
    for p in sorted(root.rglob("*.md")):
        rel = p.relative_to(root)
        if rel.parts[0] in (".git", ".claude"):
            continue
        out.append(p)
    return out


def frontmatter(text: str) -> dict | None:
    m = FM.match(text)
    if not m:
        return None
    try:
        return yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return {}


def strip_code(text: str) -> str:
    """코드블록과 인라인 코드를 뺀다 — 문법을 예시로 쓴 `[[링크]]`는 링크가 아니다."""
    out, inside = [], False
    for line in text.split("\n"):
        if FENCE.match(line):
            inside = not inside
        elif not inside:
            out.append(INLINE_CODE.sub(" ", line))
    return "\n".join(out)


def raw_modified(root: Path) -> list[str]:
    """raw/ 는 append-only — 추가(A) 아닌 수정(M) 이력을 잡는다."""
    def git(*args: str) -> str:
        # `core.quotePath=false` 가 없으면 한글 파일명이 8진수 이스케이프로 나온다
        # (`"\354\246\235..."`). 이 위키는 예시부터 전부 한글 파일명이라 그냥 못 읽는 출력이 된다.
        return subprocess.run(["git", "-C", str(root), "-c", "core.quotePath=false", *args],
                              capture_output=True, text=True, timeout=30, check=True).stdout

    try:
        out = git("log", "--diff-filter=M", "--format=", "--name-only", "--", "raw/")
        # git 이 주는 경로는 **레포 루트** 기준인데 나머지 findings 는 vault 루트 기준이다.
        # 섞어 내보내면 같은 리포트 안에서 경로 체계가 둘이 된다.
        top = git("rev-parse", "--show-toplevel").strip()
    except (subprocess.SubprocessError, OSError):
        return []

    prefix = ""
    try:
        rel = root.resolve().relative_to(Path(top).resolve())
        prefix = "" if rel == Path(".") else f"{rel.as_posix()}/"
    except ValueError:
        pass

    return sorted({ln[len(prefix):] if prefix and ln.startswith(prefix) else ln
                   for ln in out.split("\n")
                   if ln.strip() and not ln.endswith("README.md")})


def lint(root: Path) -> list[dict]:
    files = md_files(root)
    texts = {p: p.read_text(encoding="utf-8") for p in files}

    # 링크 대상 이름 사전: 스템과 파일명 둘 다 받는다 ([[나]] / [[나.md]])
    known: set[str] = set()
    for p in files:
        known.add(p.stem)
        known.add(p.name)

    inbound: dict[str, int] = {p.stem: 0 for p in files}
    findings: list[dict] = []

    for p in files:
        rel = str(p.relative_to(root))
        # raw/ 는 불변 증거다. 원문에 우연히 든 `[[…` (중첩 마크다운 링크 등)를
        # 깨진 위키링크로 잡아도 append-only 라 고칠 수가 없다 — 영구 오탐이 된다.
        # `[[링크]]` 는 wiki/ 의 규약이므로 링크 검사는 거기만 본다.
        if p.name in SCAFFOLD or rel.startswith("raw/"):
            continue
        for target in WIKI_LINK.findall(strip_code(texts[p])):
            target = target.strip()
            if target in known:
                inbound[Path(target).stem] = inbound.get(Path(target).stem, 0) + 1
            else:
                findings.append({
                    "check": "broken-link", "file": rel, "detail": f"[[{target}]] 대상 페이지 없음",
                })

    index_text = texts.get(root / "index.md", "")

    for p in files:
        rel = str(p.relative_to(root))
        if p.name in SCAFFOLD or p.parent == root:
            continue

        listed = p.name in index_text or rel in index_text
        if not listed:
            findings.append({"check": "index-missing", "file": rel,
                             "detail": "index.md 카탈로그에 없음 (→ build_index.py)"})

        if p.parent.name == "wiki":
            fm = frontmatter(texts[p])
            if fm is None:
                findings.append({"check": "frontmatter", "file": rel, "detail": "frontmatter 없음"})
            else:
                missing = [k for k in REQUIRED_FM if not fm.get(k)]
                if missing:
                    findings.append({"check": "frontmatter", "file": rel,
                                     "detail": f"누락: {', '.join(missing)}"})
            if inbound.get(p.stem, 0) == 0 and not listed:
                findings.append({"check": "orphan", "file": rel,
                                 "detail": "유입 [[링크]] 도 index 등재도 없음"})

    for f in raw_modified(root):
        findings.append({"check": "raw-modified", "file": f,
                         "detail": "raw/ 는 append-only인데 수정 이력이 있음"})

    order = {"raw-modified": 0, "broken-link": 1, "frontmatter": 2, "orphan": 3, "index-missing": 4}
    findings.sort(key=lambda f: (order.get(f["check"], 9), f["file"]))
    return findings


def main() -> int:
    ap = argparse.ArgumentParser(description="개인 위키 기계적 검사")
    ap.add_argument("--json", action="store_true", help="에이전트용 JSON")
    ap.add_argument("--quiet", action="store_true", help="문제 있을 때만 출력")
    args = ap.parse_args()

    root = wiki_root()
    findings = lint(root)

    if args.json:
        print(json.dumps({"root": str(root), "findings": findings,
                          "llm_only": LLM_ONLY}, ensure_ascii=False, indent=2))
    elif findings:
        print(f"위키 lint — {len(findings)}건  ({root})\n")
        last = None
        for f in findings:
            if f["check"] != last:
                print(f"[{f['check']}]")
                last = f["check"]
            print(f"  {f['file']}  —  {f['detail']}")
        print("\n판단이 필요해 여기서 안 잡는 것 (→ /gardener --lint):")
        for item in LLM_ONLY:
            print(f"  · {item}")
    elif not args.quiet:
        print(f"위키 lint — 기계적 문제 0건 ({root})")
        print("\n판단이 필요한 것은 /gardener --lint 가 본다:")
        for item in LLM_ONLY:
            print(f"  · {item}")

    return min(len(findings), 125)


if __name__ == "__main__":
    sys.exit(main())
