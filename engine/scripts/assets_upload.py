#!/usr/bin/env python3
"""
assets_upload.py — 임의의 자료 폴더를 MinIO 에 규약대로 적재하는 범용 업로더.

`AGENTS.md §2-1` 이 정한 자산 규약(`{namespace}/{YYYY-MM}/{slug}/`)의 **실행 도구**다.
소스·명명 규칙을 모른 채 폴더 하나만 받는다.

핵심 계약 (전부 실측으로 값이 증명된 것들 — README 가 근거를 적는다):
  · 신원 = **sha256**. ETag·크기로 가르지 않는다 (multipart ETag 는 md5 가 아니다).
  · **멱등** — 같은 내용이 이미 있으면 skip. 재실행은 upload 0 · skip N 이어야 한다.
  · **올렸다 ≠ 올라갔다** — 업로드 뒤 되받아 재해시(round-trip)해 전수 대조한다(기본 켜짐 — `--no-verify` 로만 끈다).
  · **음성 대조** — 없는 키가 404 를 내는지 매 실행 확인한다. 안 그러면 "전부 있음"이 도구 고장일 수 있다.
  · **자동 삭제 금지** — stale 은 기본 보고만. `--prune` 은 sha256 동일이 증명된 것만 지운다.
  · 한글·공백 파일명은 **NFC** 로 저장하고 올린 뒤 되읽어 왕복을 확인한다.

env (둘 중 하나로 주입 — 값을 인자로 넘기지 않는다):
  S3_ENDPOINT_URL · S3_BUCKET · S3_ACCESS_KEY_ID · S3_SECRET_ACCESS_KEY
      set -a; . ./.env; set +a; python3 engine/scripts/assets_upload.py …
  값은 인자로 넘기지 않는다 — 셸 히스토리·프로세스 목록·로그에 남는다.
"""
import argparse
import csv
import datetime
import hashlib
import io
import json
import mimetypes
import os
import sys
import unicodedata
import uuid
import zipfile

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from boto3.s3.transfer import TransferConfig

CHUNK = 1 << 22                       # 4MiB — 해시 스트리밍
MULTIPART_THRESHOLD = 64 * 1024 * 1024
XFER = TransferConfig(multipart_threshold=MULTIPART_THRESHOLD,
                      multipart_chunksize=MULTIPART_THRESHOLD,
                      max_concurrency=4)
MANIFEST_JSON = "_manifest.json"
MANIFEST_CSV = "_manifest.csv"
MANIFEST_KEYS = {MANIFEST_JSON, MANIFEST_CSV}
# 스크립트가 스스로 만드는 필드. 그 밖의 필드(예: 도메인 라벨 `project`)는
# 옛 manifest 에서 그대로 이월한다 — 재생성이 사람이 붙인 뜻을 지우면 안 된다.
OWNED_FIELDS = {"key", "rel_path", "size", "sha256", "etag"}


def log(msg):
    print(f"{datetime.datetime.now():%H:%M:%S} {msg}", flush=True)


def nfc(s):
    return unicodedata.normalize("NFC", s)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for c in iter(lambda: f.read(CHUNK), b""):
            h.update(c)
    return h.hexdigest()


def sha256_stream(body):
    h = hashlib.sha256()
    for c in iter(lambda: body.read(CHUNK), b""):
        h.update(c)
    return h.hexdigest()


# ---------- 접속 ----------
def make_client():
    missing = [v for v in ("S3_ENDPOINT_URL", "S3_BUCKET",
                           "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY")
               if not os.environ.get(v)]
    if missing:
        sys.exit(
            "자격증명이 주입되지 않았습니다: " + ", ".join(missing) + "\n"
            "  레포 루트에서:  set -a; . ./.env; set +a\n"
            "  그다음:        python3 engine/scripts/assets_upload.py …\n"
            "  .env 가 없으면: python3 engine/scripts/init.py 를 먼저 돌리세요.")
    return boto3.client(
        "s3",
        endpoint_url=os.environ["S3_ENDPOINT_URL"],
        aws_access_key_id=os.environ["S3_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["S3_SECRET_ACCESS_KEY"],
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
        config=Config(s3={"addressing_style": "path"},
                      retries={"max_attempts": 5}, max_pool_connections=8),
    ), os.environ["S3_BUCKET"]


def negative_control(s3, bucket, prefix):
    """없는 키가 정말 404 를 내는지. 안 그러면 '전부 있음'이 도구 고장일 수 있다."""
    key = f"{prefix}__negative-control-{uuid.uuid4().hex}__.probe"
    try:
        s3.head_object(Bucket=bucket, Key=key)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        if code in ("404", "NoSuchKey", "NotFound"):
            log(f"[음성대조] OK — 없는 키가 {code} 를 냈습니다")
            return True
        log(f"[음성대조] ⚠️ 404 가 아닌 오류: {code}")
        return False
    log("[음성대조] ❌ 없는 키에 head_object 가 성공했습니다 — 결과를 믿지 마십시오")
    return False


# ---------- 원격 상태 ----------
def list_prefix(s3, bucket, prefix):
    """prefix 아래 객체 → {NFC key: {size, etag, raw_key}}

    🔴 NFC 로 접었을 때 서로 다른 원본 키가 같은 자리로 오면 **중단한다.** 접힌 채로 진행하면
    한쪽이 목록·stale·대조군에서 통째로 사라지고, 삭제 도구가 그 상태로 판정하게 된다.
    """
    out, collide = {}, {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for o in page.get("Contents", []):
            k = nfc(o["Key"])
            if k in out and out[k]["raw_key"] != o["Key"]:
                collide.setdefault(k, [out[k]["raw_key"]]).append(o["Key"])
                continue
            out[k] = {"size": o["Size"], "etag": o["ETag"].strip('"'), "raw_key": o["Key"]}
    if collide:
        for k, raws in collide.items():
            log(f"[키충돌] {k} ← {[r.encode('utf-8') for r in raws]}")
        sys.exit("❌ 원격에 정규화만 다른 키가 공존합니다 — 접어서 진행하면 한쪽이 사라집니다. "
                 "사람이 정리한 뒤 다시 도십시오.")
    return out


def load_manifest(s3, bucket, prefix):
    """기존 manifest. **없을 때만** None 이다.

    🔴 모든 ClientError 를 "없음"으로 번역하면 503·AccessDenied 한 번에 삭제 원장과 사람이 붙인
    라벨이 통째로 덮여 사라진다. 404 만 없음으로 보고 나머지는 그대로 올린다.
    """
    try:
        raw = s3.get_object(Bucket=bucket, Key=prefix + MANIFEST_JSON)["Body"].read()
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        if code in ("404", "NoSuchKey", "NotFound"):
            return None
        raise
    try:
        return json.loads(raw)
    except ValueError:
        sys.exit(f"❌ 기존 {MANIFEST_JSON} 를 파싱하지 못했습니다. 덮어쓰면 삭제 원장·사람이 붙인 "
                 "필드가 사라지므로 중단합니다 — 사람이 내려받아 고치거나 치운 뒤 다시 도십시오.")


def remote_sha(s3, bucket, key, meta_cache, remote, manifest_index, stats, deep=False):
    """원격 객체의 sha256. ① 객체 메타 ② manifest(크기+ETag 일치할 때만) ③ 내려받아 해시.

    `deep=True` 는 ①②를 건너뛰고 **실물 바이트를 다시 읽는다.** 되돌릴 수 없는 삭제의 근거는
    "메타에 그렇게 적혀 있다"가 아니라 "지금 그 바이트가 그렇다"여야 한다.
    """
    if key in meta_cache and not deep:
        return meta_cache[key]
    if deep:
        stats["sha_from_download"] += 1
        got = sha256_stream(s3.get_object(Bucket=bucket, Key=key)["Body"])
        meta_cache[key] = got
        return got
    head = s3.head_object(Bucket=bucket, Key=key)
    got = head.get("Metadata", {}).get("sha256")
    # 메타는 업로드 **전**에 계산해 박은 값이라 "주장"이지 "측정"이 아니다. 최소한 길이가
    # 지금 목록과 맞을 때만 믿는다(맞지 않으면 그 뒤에 무언가 덮어쓴 것이다).
    if got and len(got) == 64 and head.get("ContentLength") == remote.get(key, {}).get("size"):
        stats["sha_from_meta"] += 1
        meta_cache[key] = got
        return got
    m = manifest_index.get(key)
    if m and m.get("sha256") and m.get("etag") and \
       m.get("size") == remote[key]["size"] and m.get("etag") == remote[key]["etag"]:
        stats["sha_from_manifest"] += 1
        meta_cache[key] = m["sha256"]
        return m["sha256"]
    stats["sha_from_download"] += 1
    body = s3.get_object(Bucket=bucket, Key=key)["Body"]
    got = sha256_stream(body)
    meta_cache[key] = got
    return got


# ---------- 로컬 상태 ----------
def scan_src(src, prefix):
    """--src 폴더 → [{rel_path, key, path, size, sha256}] (NFC, 정렬)"""
    files, seen, skipped = [], {}, []
    for root, dirs, names in os.walk(src):
        dirs[:] = sorted(d for d in dirs if d not in (".git", "__pycache__"))
        for n in sorted(names):
            if n == ".DS_Store":
                continue
            p = os.path.join(root, n)
            if os.path.islink(p) or not os.path.isfile(p):
                skipped.append(p)
                continue
            rel = nfc(os.path.relpath(p, src))
            # 🔴 ext4 에는 NFD 이름과 NFC 이름이 **서로 다른 두 파일**로 공존할 수 있다(맥에서
            #    받은 폴더·한글 zip 해제분에서 흔하다). 접으면 뒤엣것이 앞엣것을 덮고,
            #    검증은 남은 하나만 보며, prune 은 사라진 쪽 sha 까지 "살아 있다"고 센다.
            if rel in seen:
                sys.exit(f"❌ 정규화하면 같은 이름이 되는 파일이 둘입니다 — 하나가 조용히 사라집니다:\n"
                         f"   {seen[rel]}\n   {p}\n사람이 이름을 정리한 뒤 다시 도십시오.")
            seen[rel] = p
            files.append({"rel_path": rel, "key": nfc(prefix + rel), "path": p,
                          "size": os.path.getsize(p), "sha256": sha256_file(p)})
    if skipped:
        log(f"[로컬] 심링크·비정규 파일 {len(skipped)}건은 건너뜁니다")
        for p in skipped[:10]:
            log(f"  · {p}")
    return sorted(files, key=lambda f: f["rel_path"])


# ---------- 업로드 ----------
def upload_one(s3, bucket, f):
    ctype = mimetypes.guess_type(f["rel_path"])[0] or "application/octet-stream"
    s3.upload_file(f["path"], bucket, f["key"], Config=XFER,
                   ExtraArgs={"ContentType": ctype, "Metadata": {"sha256": f["sha256"]}})


def roundtrip_name_check(s3, bucket, prefix, keys):
    """올린 키가 **그 바이트 그대로** 되읽히는가 (NFC/NFD·공백·괄호 왕복).

    검출력은 `head_object` 쪽에 있다 — list 대조는 양쪽을 NFC 로 접으므로 정규화 드리프트를
    정의상 못 잡는다. 그래서 list 는 **원본 키 바이트**와 대조한다(접지 않는다).
    """
    if not keys:
        return []
    listed = set()
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for o in page.get("Contents", []):
            listed.add(o["Key"])
    bad = []
    for k in keys:
        if k not in listed:
            bad.append((k, "list 가 올린 키와 다른 바이트로 돌려줍니다(정규화 드리프트)"))
            continue
        try:
            s3.head_object(Bucket=bucket, Key=k)
        except ClientError as e:
            bad.append((k, f"head 실패 {e.response.get('Error', {}).get('Code')}"))
    return bad


# ---------- 검증 ----------
def verify_roundtrip(s3, bucket, expected):
    """expected = {key: sha256}. 전부 되받아 재해시해 대조한다. '올렸다'와 '올라갔다'는 다르다."""
    bad, ok = [], 0
    for i, (key, want) in enumerate(sorted(expected.items()), 1):
        try:
            got = sha256_stream(s3.get_object(Bucket=bucket, Key=key)["Body"])
        except ClientError as e:
            bad.append((key, "GET 실패 " + str(e.response.get("Error", {}).get("Code"))))
            log(f"  [{i}/{len(expected)}] ❌ GET 실패 {key}")
            continue
        if got == want:
            ok += 1
        else:
            bad.append((key, f"sha 불일치 want={want[:12]} got={got[:12]}"))
            log(f"  [{i}/{len(expected)}] ❌ sha 불일치 {key}")
    # 회계가 안 맞으면 어딘가에서 키가 접혔다는 뜻이다 — 초록으로 넘기지 않는다.
    assert ok + len(bad) == len(expected), \
        f"검증 회계 불일치: ok {ok} + bad {len(bad)} ≠ 대상 {len(expected)}"
    return ok, bad


# ---------- manifest ----------
def build_manifest(prefix, bucket, rows, source, prior):
    prior_files = {r.get("key"): r for r in (prior or {}).get("files", [])}
    files = []
    for r in rows:
        entry = {k: r[k] for k in ("key", "rel_path", "size", "sha256", "etag") if k in r}
        carry = prior_files.get(r["key"], {})
        for k, v in carry.items():                # 사람이 붙인 도메인 필드 이월
            if k not in OWNED_FIELDS:
                entry.setdefault(k, v)
        files.append(entry)
    m = {
        "prefix": prefix,
        "bucket": bucket,
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "source": source or (prior or {}).get("source", ""),
        "file_count": len(files),
        "total_bytes": sum(f["size"] for f in files),
        "files": files,
    }
    for k in ("deletions", "notes"):              # 삭제 원장은 재생성해도 살아남는다
        if (prior or {}).get(k):
            m[k] = prior[k]
    return m


def manifest_csv(m):
    cols, seen = [], set()
    for f in m["files"]:
        for k in f:
            if k not in seen:
                seen.add(k)
                cols.append(k)
    order = [c for c in ("project", "rel_path", "size", "sha256", "etag", "key") if c in seen]
    cols = order + [c for c in cols if c not in order]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for f in m["files"]:
        w.writerow(f)
    return buf.getvalue()


def put_manifest(s3, bucket, prefix, m):
    body = json.dumps(m, ensure_ascii=False, indent=1).encode()
    s3.put_object(Bucket=bucket, Key=prefix + MANIFEST_JSON, Body=body,
                  ContentType="application/json; charset=utf-8",
                  Metadata={"sha256": hashlib.sha256(body).hexdigest()})
    cbody = manifest_csv(m).encode()
    s3.put_object(Bucket=bucket, Key=prefix + MANIFEST_CSV, Body=cbody,
                  ContentType="text/csv; charset=utf-8",
                  Metadata={"sha256": hashlib.sha256(cbody).hexdigest()})
    log(f"[manifest] _manifest.json({len(body)}B) + _manifest.csv({len(cbody)}B) 갱신")


# ---------- 아카이브 중복 판정 ----------
def zip_entry_name(info):
    """zip 항목 이름을 사람이 읽을 수 있게. UTF-8 플래그가 없으면 CP949 → CP437 순으로 시도."""
    if info.flag_bits & 0x800:
        return info.filename
    raw = info.filename.encode("cp437", "replace")
    for enc in ("utf-8", "cp949"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return info.filename


def unaccounted_bytes(zf, path):
    """zip 파일 안에서 **어떤 항목에도 속하지 않는 바이트**가 있는가.

    zipfile 은 끝에서 central directory 를 찾으므로 앞에 스텁이 붙은 자기추출(SFX) 아카이브도
    정상으로 연다. 그 스텁은 항목이 아니라서 「전부 중복」 판정 뒤 **말없이 사라진다.**
    """
    reasons = []
    infos = zf.infolist()
    if infos and min(i.header_offset for i in infos) != 0:
        reasons.append(f"앞에 항목 아닌 바이트 {min(i.header_offset for i in infos)}B (SFX 스텁 등)")
    if zf.comment:
        reasons.append(f"아카이브 코멘트 {len(zf.comment)}B")
    return reasons


def check_archive(s3, bucket, prefix, arch_key, remote, meta_cache, manifest_index, stats):
    """zip 객체의 모든 항목이 prefix 안 다른 객체와 byte 동일한가.

    같으면 그 zip 은 '용기'일 뿐이라 지워도 내용이 남는다. 하나라도 어긋나면 유일본이 있다는 뜻.
    대조군은 **실물 바이트로 재해시**한다 — 되돌릴 수 없는 조작의 근거는 메타가 아니라 측정이다.
    """
    arch_key = nfc(arch_key)
    if arch_key not in remote:
        log(f"[archive] ❌ prefix 에 없는 키입니다: {arch_key}")
        return None
    tmp = f"/tmp/wiki_assets_archive_{uuid.uuid4().hex}.zip"
    log(f"[archive] 내려받는 중 {arch_key} ({remote[arch_key]['size']}B)")
    s3.download_file(bucket, arch_key, tmp)
    try:
        got = sha256_file(tmp)
        log(f"[archive] 내려받은 zip sha256={got}")
        with zipfile.ZipFile(tmp) as zf:
            entries = [i for i in zf.infolist() if not i.is_dir()]
            leftover = unaccounted_bytes(zf, tmp)
            rows, missing = [], []
            pool = {}
            cands = [k for k in sorted(remote)
                     if k != arch_key and k.rsplit("/", 1)[-1] not in MANIFEST_KEYS]
            log(f"[archive] 대조군 {len(cands)}건을 **실물 바이트로 재해시**합니다 (메타 신뢰 안 함)")
            for k in cands:
                pool.setdefault(remote_sha(s3, bucket, k, meta_cache, remote,
                                           manifest_index, stats, deep=True), []).append(k)
            for i, info in enumerate(entries, 1):
                name = nfc(zip_entry_name(info))
                zsha = hashlib.sha256(zf.read(info)).hexdigest()
                hit = pool.get(zsha)
                if hit:
                    rows.append({"entry": name, "size": info.file_size,
                                 "sha256": zsha, "objects": hit})
                    log(f"  [{i}/{len(entries)}] OK   {zsha[:12]} {name}")
                else:
                    missing.append({"entry": name, "size": info.file_size, "sha256": zsha})
                    log(f"  [{i}/{len(entries)}] ❌ 대응 객체 없음 {name}")
        for r in leftover:
            log(f"[archive] ❌ 회계되지 않는 바이트 — {r}")
        redundant = bool(entries) and not missing and not leftover
        log(f"[archive] 항목 {len(entries)} · 대응 확인 {len(rows)} · 미대응 {len(missing)} "
            f"· 회계 밖 {len(leftover)} "
            f"→ {'전부 중복(삭제 가능)' if redundant else '삭제 금지'}")
        if not entries:
            log("[archive] 항목이 없는 zip 입니다 — 지울 근거가 없습니다")
        return {"key": arch_key, "size": remote[arch_key]["size"], "sha256": got,
                "entries": len(entries), "covered": len(rows), "uncovered": missing,
                "leftover": leftover, "redundant": redundant, "rows": rows,
                "objects": sorted({o for r in rows for o in r["objects"]})}
    finally:
        os.unlink(tmp)


def ledger_add(prior, entry):
    """삭제 원장에 한 줄. 재생성해도 살아남는다(build_manifest 가 이월)."""
    prior.setdefault("deletions", []).append(entry)


# ---------- manifest 행 만들기 ----------
def rows_from_remote(s3, bucket, prefix, remote, local_by_key, meta_cache, manifest_index, stats):
    """manifest 행은 **원격 실물 목록 전체**에서 만든다.

    로컬만으로 만들면 stale 로 남겨 둔 객체(삭제를 거부한 유일본 포함)와 앞선 다른 배치의 행이
    사람이 붙인 라벨과 함께 조용히 사라진다.
    """
    rows = []
    for k, v in sorted(remote.items()):
        if k.rsplit("/", 1)[-1] in MANIFEST_KEYS:
            continue
        f = local_by_key.get(k)
        sha = f["sha256"] if f else remote_sha(s3, bucket, k, meta_cache, remote,
                                               manifest_index, stats)
        rows.append({"key": k, "rel_path": k[len(prefix):], "size": v["size"],
                     "etag": v["etag"], "sha256": sha})
    return rows


# ---------- main ----------
def main():
    ap = argparse.ArgumentParser(description="MinIO 범용 자산 업로더 (AGENTS.md §2-1)")
    ap.add_argument("--namespace", required=True, help="자료가 속한 주제 영역 (예: 홈서버, 학습, 재무). 기존 값을 먼저 볼 것")
    ap.add_argument("--slug", required=True, help="자료 묶음 slug (kebab-case)")
    ap.add_argument("--month", default=datetime.date.today().strftime("%Y-%m"),
                    help="YYYY-MM (기본: 이번 달)")
    ap.add_argument("--src", help="올릴 폴더. 없으면 업로드 없이 점검만 한다")
    ap.add_argument("--source", default="", help="manifest 에 남길 출처 한 줄")
    ap.add_argument("--apply", action="store_true", help="실제로 쓴다 (기본은 dry-run)")
    ap.add_argument("--no-verify", action="store_true",
                    help="업로드 뒤 되받아 재해시하는 전수 검증을 생략 (--prune 과 함께 쓸 수 없다)")
    ap.add_argument("--prune", action="store_true",
                    help="로컬에 없는 stale 객체를 지운다 — sha256 이 남는 객체와 같음이 증명된 것만")
    arch = ap.add_mutually_exclusive_group()
    arch.add_argument("--check-archive", metavar="KEY",
                      help="zip 객체가 prefix 안 개별 객체들과 전부 byte 동일한지 판정(보고만)")
    arch.add_argument("--prune-archive", metavar="KEY",
                      help="위 판정이 '전부 중복'일 때만 그 zip 을 지운다 (--apply 필요)")
    a = ap.parse_args()

    # 🔴 --no-verify 는 "올라갔다"를 확인하지 않는다. 그 상태에서 지우면 근거 없이 지우는 것이다.
    if a.prune and a.no_verify:
        ap.error("--prune 은 --no-verify 와 함께 쓸 수 없습니다 — 검증하지 않고 지울 수 없습니다")

    prefix = nfc(f"{a.namespace}/{a.month}/{a.slug}/")
    s3, bucket = make_client()
    log(f"[대상] s3://{bucket}/{prefix}  (endpoint={os.environ['S3_ENDPOINT_URL']})")
    if not negative_control(s3, bucket, prefix):
        sys.exit("음성 대조 실패 — 도구가 고장 났을 수 있어 중단합니다")

    remote = list_prefix(s3, bucket, prefix)
    log(f"[원격] 객체 {len(remote)} · {sum(v['size'] for v in remote.values())} B")
    prior = load_manifest(s3, bucket, prefix) or {}
    manifest_index = {f.get("key"): f for f in prior.get("files", [])}
    meta_cache, stats = {}, {"sha_from_meta": 0, "sha_from_manifest": 0, "sha_from_download": 0}
    exit_code = 0

    def hash_source_line():
        log(f"[해시 출처] 메타 {stats['sha_from_meta']} · manifest {stats['sha_from_manifest']} "
            f"· 내려받음 {stats['sha_from_download']}")

    # --- 아카이브 중복 판정/삭제 ---
    arch_target = a.check_archive or a.prune_archive
    if arch_target:
        r = check_archive(s3, bucket, prefix, arch_target, remote, meta_cache, manifest_index, stats)
        if r is None:
            sys.exit(2)
        if not r["redundant"]:
            exit_code = 3                      # 자동화가 판정 결과를 종료코드로 구분할 수 있게
        if a.prune_archive:
            if not r["redundant"]:
                sys.exit("❌ 전부 중복임이 증명되지 않았습니다 — 지우지 않습니다")
            if not a.apply:
                log("[dry-run] --apply 를 주면 이 zip 을 지웁니다")
            else:
                s3.delete_object(Bucket=bucket, Key=remote[r["key"]]["raw_key"])
                log(f"[삭제] {r['key']}")
                remote = list_prefix(s3, bucket, prefix)
                if r["key"] in remote:
                    sys.exit("❌ 삭제 뒤에도 키가 목록에 남아 있습니다")
                log(f"[원격] 삭제 후 객체 {len(remote)} "
                    f"· {sum(v['size'] for v in remote.values())} B")
                ledger_add(prior, {
                    "key": r["key"], "size": r["size"], "sha256": r["sha256"],
                    "deleted_at": datetime.datetime.now().isoformat(timespec="seconds"),
                    "deleted_by": os.environ.get("USER", "?"),
                    "reason": f"zip 항목 {r['entries']}건이 전부 prefix 안 개별 객체 "
                              f"{len(r['objects'])}건과 byte 동일 — 용기만 제거",
                    "covered_by": r["rows"],   # 어느 항목이 어느 객체로 살아남았나 (사후 감사용)
                })
                prior["files"] = [f for f in prior.get("files", []) if f.get("key") != r["key"]]
                manifest_index = {f.get("key"): f for f in prior.get("files", [])}
                put_manifest(s3, bucket, prefix, build_manifest(
                    prefix, bucket,
                    rows_from_remote(s3, bucket, prefix, remote, {}, meta_cache,
                                     manifest_index, stats),
                    a.source, prior))
        if not a.src:
            hash_source_line()
            sys.exit(exit_code)

    if not a.src:
        log("--src 가 없어 업로드는 건너뜁니다")
        sys.exit(exit_code)

    # --- 계획 ---
    local = scan_src(a.src, prefix)
    log(f"[로컬] 파일 {len(local)} · {sum(f['size'] for f in local)} B ({a.src})")
    to_upload, skipped = [], []
    for f in local:
        if f["key"] in remote:
            got = remote_sha(s3, bucket, f["key"], meta_cache, remote, manifest_index, stats)
            if got == f["sha256"]:
                skipped.append(f)
                continue
            log(f"  [변경] {f['rel_path']}  원격 {got[:12]} ≠ 로컬 {f['sha256'][:12]}")
        to_upload.append(f)
    log(f"[계획] upload {len(to_upload)} · skip {len(skipped)}")
    hash_source_line()

    local_keys = {f["key"] for f in local}
    stale = [k for k in remote if k not in local_keys
             and k.rsplit("/", 1)[-1] not in MANIFEST_KEYS]
    if stale:
        log(f"[stale] 로컬에 없는 객체 {len(stale)}건 — 기본은 보고만 합니다")
        for k in sorted(stale):
            log(f"  · {k}")

    if not a.apply:
        log("[dry-run] 아무것도 쓰지 않았습니다. 실제 적재는 --apply.")
        sys.exit(exit_code)

    # --- 업로드 ---
    failed = []
    for i, f in enumerate(to_upload, 1):
        log(f"  [{i}/{len(to_upload)}] ↑ {f['rel_path']} ({f['size']}B)")
        try:
            upload_one(s3, bucket, f)
        except Exception as e:            # 한 건이 죽어도 나머지를 돌리고 manifest 까지 맞춘다
            failed.append((f["rel_path"], repr(e)))
            log(f"       ❌ 업로드 실패 — {e!r}")
    log(f"[적재] upload {len(to_upload) - len(failed)} · skip {len(skipped)} · 실패 {len(failed)}")
    if failed:
        exit_code = 1

    # --- 이름 왕복 확인 ---
    ok_keys = [f["key"] for f in local if f["rel_path"] not in {r for r, _ in failed}]
    bad_names = roundtrip_name_check(s3, bucket, prefix, ok_keys)
    if bad_names:
        exit_code = 1
        log(f"[왕복] ❌ 이름이 되읽히지 않는 키 {len(bad_names)}건")
        for k, why in bad_names:
            log(f"  · {k} — {why}")
    else:
        log(f"[왕복] OK — {len(ok_keys)}건 전부 올린 키 바이트 그대로 list·head 로 되읽힙니다")

    # --- 전수 재해시 검증 ---
    if a.no_verify:
        log("[검증] --no-verify 로 생략했습니다 — '올라갔다'는 아직 미확인입니다")
    else:
        log(f"[검증] {len(local)}건을 되받아 재해시합니다")
        ok, bad = verify_roundtrip(s3, bucket, {f["key"]: f["sha256"] for f in local})
        log(f"[검증] 일치 {ok}/{len(local)} · 불일치 {len(bad)}")
        if bad:
            # 🔴 그냥 실패로 두면 **다음 실행이 고치지 못한다** — 메타 sha 는 업로드 전에 박은
            #    값이라 깨진 객체가 계속 skip 된다. 그러니 그 자리에서 다시 올리고 다시 잰다.
            keys = {k for k, _ in bad}
            retry = [f for f in local if f["key"] in keys]
            log(f"[복구] 불일치 {len(retry)}건을 다시 올리고 다시 잽니다")
            for f in retry:
                try:
                    upload_one(s3, bucket, f)
                except Exception as e:
                    log(f"  ❌ 재업로드 실패 {f['rel_path']} — {e!r}")
            ok2, bad2 = verify_roundtrip(s3, bucket, {f["key"]: f["sha256"] for f in retry})
            log(f"[복구] 재검증 일치 {ok2}/{len(retry)} · 불일치 {len(bad2)}")
            if bad2:
                exit_code = 1
                for k, why in bad2:
                    log(f"  · {k} — {why}")

    # --- stale prune (증명된 것만, 그리고 검증이 초록일 때만) ---
    if a.prune and stale:
        if exit_code != 0:
            log("[prune] ❌ 업로드·왕복·검증 중 하나가 실패했습니다 — 아무것도 지우지 않습니다. "
                "'같은 내용이 다른 키로 남는다'를 지금은 증명할 수 없습니다")
        else:
            # 여기 도달했다는 것은 로컬 66건이 원격에 그 sha 로 실재함을 방금 전수 재해시로
            # 확인했다는 뜻이다. 그래서 keep_sha 는 "로컬의 주장"이 아니라 "측정된 잔존분"이다.
            keep_sha = {f["sha256"] for f in local}
            log("[prune] 삭제 후보를 **실물 바이트로 재해시**합니다 (메타 신뢰 안 함)")
            for k in sorted(stale):
                got = remote_sha(s3, bucket, k, meta_cache, remote,
                                 manifest_index, stats, deep=True)
                survivors = sorted(f["key"] for f in local if f["sha256"] == got)
                if survivors:
                    s3.delete_object(Bucket=bucket, Key=remote[k]["raw_key"])
                    log(f"[prune] 삭제 {k} — 같은 내용이 {survivors[0]} 로 남습니다")
                    ledger_add(prior, {
                        "key": k, "size": remote[k]["size"], "sha256": got,
                        "deleted_at": datetime.datetime.now().isoformat(timespec="seconds"),
                        "deleted_by": os.environ.get("USER", "?"),
                        "reason": "stale — 같은 내용이 다른 키로 남는다(검증 통과 후 전수 재해시로 확인)",
                        "covered_by": [{"entry": k, "sha256": got, "objects": survivors}],
                    })
                else:
                    log(f"[prune] 보존 {k} — 이 내용의 유일본입니다(삭제 금지)")

    # --- manifest ---
    remote = list_prefix(s3, bucket, prefix)
    local_by_key = {f["key"]: f for f in local}
    put_manifest(s3, bucket, prefix, build_manifest(
        prefix, bucket,
        rows_from_remote(s3, bucket, prefix, remote, local_by_key, meta_cache,
                         manifest_index, stats),
        a.source, prior))
    remote = list_prefix(s3, bucket, prefix)
    log(f"[최종] 객체 {len(remote)} · {sum(v['size'] for v in remote.values())} B")
    hash_source_line()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
