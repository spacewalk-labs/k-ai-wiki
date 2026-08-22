#!/usr/bin/env python3
"""개인 위키 `index.md` 카탈로그 생성.

구조는 기계가, 설명은 LLM 이 — 각 줄의 설명은 기존 index 에서 그대로 옮겨온다.
스크립트가 사람이 쓴 설명을 덮어쓰는 일은 없다. 계약·이유 = engine/README.md
"""
import argparse
import os
import re
import sys
from pathlib import Path

BEGIN = "<!-- JW-MARKER:catalog -->"
END = "<!-- /JW-MARKER:catalog -->"
NO_DESC = "_(설명 없음 — /gardener 가 채운다)_"

# 카탈로그에 싣지 않는 파일 — 위키 자체의 발판
SKIP = {"index.md", "log.md", "gaps.md", "README.md", "AGENTS.md", "CLAUDE.md"}

# 기존 index 줄에서 설명 회수: `- [제목](경로) — 설명` / `` - `파일` — 설명 ``
DESC = re.compile(r"^\s*-\s+(?:\[[^\]]*\]\(([^)]+)\)|`([^`]+)`)\s*(?:—|-)\s*(.+?)\s*$")


def wiki_root() -> Path:
    default = Path(__file__).resolve().parents[2] / "wiki-vault"
    p = Path(os.environ.get("WIKI_ROOT", default))
    if not (p / "index.md").exists():
        sys.exit(f"위키 vault 를 찾을 수 없습니다: {p}  (WIKI_ROOT 로 지정하세요)")
    return p


def existing_descriptions(text: str) -> dict[str, str]:
    """파일명(basename) → 설명. 경로가 바뀌어도 설명이 따라가도록 basename 으로 잡는다."""
    out = {}
    for line in text.split("\n"):
        m = DESC.match(line)
        if not m:
            continue
        ref = (m.group(1) or m.group(2)).strip().strip("<>")
        desc = m.group(3).strip()
        if desc and desc != NO_DESC:
            out[Path(ref).name] = desc
    return out


def entry(path: Path, root: Path, descs: dict[str, str]) -> str:
    rel = path.relative_to(root).as_posix()
    # 공백이 든 경로는 <> 로 감싸야 링크가 끊기지 않는다 (파일명이 한글 + 공백)
    target = f"<{rel}>" if " " in rel else rel
    return f"- [{path.stem}]({target}) — {descs.get(path.name, NO_DESC)}"


def listing(root: Path, sub: str) -> list[Path]:
    d = root / sub if sub else root
    if not d.is_dir():
        return []
    # 확장자를 뺀 이름으로 정렬 — 안 그러면 `…-나.md` 가 `…-나-2차.md` 뒤로 밀린다
    return sorted((p for p in d.glob("*.md") if p.name not in SKIP), key=lambda p: p.stem)


def render(root: Path, descs: dict[str, str]) -> str:
    lines = [BEGIN, ""]

    lines.append("## 루트")
    lines.append("")
    roots = listing(root, "")
    lines += [entry(p, root, descs) for p in roots] or ["_(아직 없음)_"]

    lines += ["", "## wiki/", ""]
    pages = listing(root, "wiki")
    lines += [entry(p, root, descs) for p in pages] or ["_(아직 없음)_"]

    lines += ["", "## raw/"]
    # raw/ 아래는 깊이가 더 생긴다(raw/ingest/notion/ 등) — 폴더 트리를 그대로 따라간다
    raw = root / "raw"
    for d in sorted((p for p in raw.rglob("*") if p.is_dir()), key=lambda p: p.as_posix()):
        items = listing(root, str(d.relative_to(root)))
        if not items:
            continue
        rel = d.relative_to(raw).as_posix()
        lines += ["", f"### {rel}/", ""]
        lines += [entry(p, root, descs) for p in items]

    lines += ["", END]
    return "\n".join(lines)


def lost_descriptions(block: str, current: str) -> list[str]:
    """설명이 **사라지는** 항목만 센다.

    옛 계산은 `NO_DESC` 개수 차였는데, 그러면 설명이 아직 없는 **새 파일**까지 손실로 세어
    새 위키의 첫 실행이 항상 거부됐다. 손실은 "옛 index 에 이미 실려 있던 파일이 이번엔
    설명을 잃는 것"이다 — 처음 등장하는 파일은 손실이 아니다.
    """
    # 이미 `_(설명 없음)_` 으로 실려 있던 항목은 잃을 설명이 애초에 없다
    blank = set()
    for line in current.split("\n"):
        if NO_DESC in line:
            m = re.search(r"\]\(<?([^)>]+)>?\)", line)
            if m:
                blank.add(Path(m.group(1)).name)

    out = []
    for line in block.split("\n"):
        if NO_DESC not in line:
            continue
        m = re.search(r"\]\(<?([^)>]+)>?\)", line)
        if not m:
            continue
        name = Path(m.group(1)).name
        if name in current and name not in blank:
            out.append(name)
    return out


def compose(text: str, block: str) -> str:
    if BEGIN in text and END in text:
        head = text.split(BEGIN)[0]
        tail = text.split(END, 1)[1]
        return head + block + tail
    # 최초 실행 — 첫 `## ` 제목부터 끝까지를 마커 블록이 넘겨받는다
    m = re.search(r"^## ", text, re.M)
    if not m:
        return text.rstrip("\n") + "\n\n" + block + "\n"
    return text[: m.start()] + block + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="개인 위키 index.md 카탈로그 생성")
    ap.add_argument("--check", action="store_true", help="갱신이 필요하면 exit 1 (쓰지 않음)")
    ap.add_argument("--force", action="store_true",
                    help="설명이 줄어드는 갱신도 강행 (기본은 거부)")
    args = ap.parse_args()

    root = wiki_root()
    index = root / "index.md"
    current = index.read_text(encoding="utf-8")
    block = render(root, existing_descriptions(current))
    updated = compose(current, block)

    if updated == current:
        print(f"index.md 최신 ({root})")
        return 0
    if args.check:
        print(f"index.md 갱신 필요 — build_index.py 를 실행하세요 ({root})", file=sys.stderr)
        return 1

    # 설명 회수는 `- [제목](경로) — 설명` 한 줄 형태만 잡는다. 한 줄에 여러 파일을 묶은
    # 큐레이션(`- ✅ [A](..) · [B](..) — 설명`)이나 안내 blockquote 는 회수되지 않아
    # 재생성하면 사라진다 — 2026-08-08 실제로 26건이 '설명 없음' 이 됐다.
    # README 가 "사람이 쓴 설명을 덮어쓰지 않는다"고 약속하므로, 손실이면 쓰지 않는다.
    lost = lost_descriptions(block, current)
    if lost and not args.force:
        print(f"❌ 쓰지 않았습니다 — 설명이 {len(lost)}건 사라집니다 ({root})", file=sys.stderr)
        print(f"   대상: {', '.join(lost)}", file=sys.stderr)
        print("   손으로 큐레이션한 줄(묶음 항목·blockquote)은 생성기가 회수하지 못합니다.",
              file=sys.stderr)
        print("   누락 항목은 손으로 넣으시고, 정말 재생성하려면 --force.", file=sys.stderr)
        return 2

    index.write_text(updated, encoding="utf-8")
    filled = updated.count(NO_DESC)
    print(f"index.md 갱신 ({root})")
    if filled:
        print(f"⚠️  설명 없는 항목 {filled}건 — /gardener 로 채우세요")
    return 0


if __name__ == "__main__":
    sys.exit(main())
