#!/usr/bin/env python3
"""init.py — 내 위키로 만드는 첫 실행.

묻는 것은 **MinIO 자격 넷**뿐이다. 위키 내용은 묻지 않는다 —
그건 사람이 폼을 채우는 게 아니라 에이전트가 `/wiki-seed` 인터뷰로 만든다.

하는 일:
  1. `.env` 를 만든다 (권한 600. 이미 있으면 건드리지 않는다)
  2. 버킷이 없으면 만들고 **versioning 을 켠다**
  3. 실제로 넣고·되읽고·지워 본다 (= 양성 대조)
  4. 없는 키가 404 를 내는지 본다 (= 음성 대조. 이게 없으면 "성공"이 도구 고장일 수 있다)

3·4 를 둘 다 하는 이유: 되읽기만 성공하면 "권한이 있다"까지만 알 뿐,
**조회가 아무 키에나 200 을 주는 고장**을 못 가린다. 부재를 확인하는 쪽이 더 자주 틀린다.
"""
import getpass
import os
import shlex
import sys
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ENV = REPO / ".env"
KEYS = ("S3_ENDPOINT_URL", "S3_BUCKET", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY")


def ask(prompt: str, default: str = "", secret: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        val = (getpass.getpass(f"{prompt}{suffix}: ") if secret
               else input(f"{prompt}{suffix}: ")).strip()
        val = val or default
        if val:
            return val
        print("  값이 필요합니다.")


def write_env() -> dict:
    if ENV.exists():
        print(f"✅ .env 가 이미 있습니다 ({ENV}) — 건드리지 않습니다.")
        return load_env()

    print("\n── MinIO 접속 정보 ──")
    print("홈서버에 MinIO 를 아직 안 올렸다면 Ctrl-C 로 멈추고 먼저 올리세요.")
    print("(README '2. 무거운 파일 저장소(MinIO) 올리기' 참고)\n")
    vals = {
        "S3_ENDPOINT_URL": ask("MinIO 주소", "http://localhost:9000"),
        "S3_BUCKET": ask("버킷 이름", "my-wiki"),
        "S3_ACCESS_KEY_ID": ask("Access Key"),
        "S3_SECRET_ACCESS_KEY": ask("Secret Key (입력해도 화면에 안 보입니다)", secret=True),
    }
    # 🔴 반드시 인용한다. `.env` 는 다른 도구들이 `set -a; . ./.env` 로 **셸에 먹여** 읽으므로,
    # 인용 없이 쓰면 비밀번호의 `$`·공백·백틱을 셸이 해석해 값이 조용히 달라진다.
    # 그러면 init.py 는 "성공"을 찍는데 정작 업로더는 403 으로 죽는다(원인 표시도 엉뚱해진다).
    ENV.write_text("".join(f"{k}={shlex.quote(v)}\n" for k, v in vals.items()), encoding="utf-8")
    ENV.chmod(0o600)
    print(f"\n✅ {ENV} 를 만들었습니다 (권한 600, git 추적 안 됨)")
    return vals


def load_env() -> dict:
    vals = {}
    for line in ENV.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        # 셸과 **같은 규칙**으로 읽는다 — 따옴표를 벗기고 인라인 주석(`# …`)을 뗀다.
        # 두 파서가 갈리면 `.env.example` 을 그대로 복사한 사람이 주석까지 버킷 이름으로 쓰게 된다.
        try:
            parts = shlex.split(v, comments=True)
        except ValueError:
            parts = [v.strip()]
        vals[k.strip()] = parts[0] if parts else ""
    missing = [k for k in KEYS if not vals.get(k)]
    if missing:
        sys.exit(f"❌ .env 에 값이 비었습니다: {', '.join(missing)}")
    return vals


def check_store(vals: dict) -> None:
    try:
        import boto3
        from botocore.config import Config
        from botocore.exceptions import ClientError
    except ImportError:
        sys.exit("❌ boto3 가 없습니다:  pip install boto3 pyyaml")

    s3 = boto3.client(
        "s3",
        endpoint_url=vals["S3_ENDPOINT_URL"],
        aws_access_key_id=vals["S3_ACCESS_KEY_ID"],
        aws_secret_access_key=vals["S3_SECRET_ACCESS_KEY"],
        region_name=vals.get("AWS_REGION", "us-east-1"),
        config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 3}),
    )
    bucket = vals["S3_BUCKET"]

    print(f"\n── 저장소 점검 ({vals['S3_ENDPOINT_URL']} / {bucket}) ──")

    def bail(e) -> None:
        """입문자가 첫 실행에서 가장 자주 밟는 두 실패를 한글 한 줄로 돌려준다."""
        name = type(e).__name__
        code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
        if "Endpoint" in name or "Connect" in name:
            sys.exit(f"❌ MinIO 에 닿지 않습니다: {vals['S3_ENDPOINT_URL']}\n"
                     "   · 주소·포트가 맞는지, MinIO 가 떠 있는지 (docker compose ps)\n"
                     "   · 다른 기기라면 Tailscale 이 연결돼 있는지\n"
                     "   고치려면 .env 를 지우고 다시 실행하세요.")
        if code in ("SignatureDoesNotMatch", "InvalidAccessKeyId", "AccessDenied", "403"):
            sys.exit(f"❌ 자격증명이 거부됐습니다 ({code}).\n"
                     "   .env 의 S3_ACCESS_KEY_ID / S3_SECRET_ACCESS_KEY 를 확인하세요.\n"
                     "   고치려면 .env 를 지우고 다시 실행하세요.")
        sys.exit(f"❌ 저장소 점검 실패 ({name} {code}): {e}")

    try:
        try:
            s3.head_bucket(Bucket=bucket)
            print(f"  버킷 있음: {bucket}")
        except ClientError:
            s3.create_bucket(Bucket=bucket)
            print(f"  버킷 만듦: {bucket}")

        s3.put_bucket_versioning(Bucket=bucket,
                                 VersioningConfiguration={"Status": "Enabled"})
        status = s3.get_bucket_versioning(Bucket=bucket).get("Status")
    except Exception as e:      # noqa: BLE001 — 어떤 실패든 한글로 돌려준다
        bail(e)
    if status != "Enabled":
        sys.exit(f"❌ versioning 이 안 켜집니다 (지금: {status}). "
                 "덮어쓰기가 원본을 지우게 되므로 여기서 멈춥니다.")
    print("  versioning: Enabled")

    # 양성 대조 — 넣고 되읽어 바이트가 같은지
    key = f"_init-check/{uuid.uuid4().hex}.txt"
    body = b"k-ai-wiki init check"
    try:
        s3.put_object(Bucket=bucket, Key=key, Body=body)
        got = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    except Exception as e:      # noqa: BLE001
        bail(e)
    if got != body:
        sys.exit("❌ 되읽은 내용이 올린 것과 다릅니다. 저장소 설정을 확인하세요.")
    print("  쓰기·읽기: OK (되읽은 바이트 일치)")

    # 음성 대조 — 없는 키는 반드시 실패해야 한다
    try:
        s3.head_object(Bucket=bucket, Key=f"_init-check/절대없는키-{uuid.uuid4().hex}")
    except ClientError:
        print("  없는 키 조회: 정상적으로 실패 (음성 대조 OK)")
    else:
        sys.exit("❌ 없는 키가 조회에 성공했습니다 — 저장소나 프록시가 고장입니다. "
                 "이 상태로는 '이미 있음' 판정을 믿을 수 없습니다.")

    s3.delete_object(Bucket=bucket, Key=key)
    print("  정리 완료")


def main() -> int:
    print("k-ai-wiki — 내 위키 만들기\n" + "=" * 32)
    vals = write_env()
    check_store(vals)

    print("""
✅ 준비 끝났습니다.

다음 할 일 — 에이전트(Claude Code)를 이 폴더에서 열고 이렇게 말하세요:

    /wiki-seed

인터뷰로 `wiki-vault/나.md` 를 채웁니다. 그게 이 위키의 출발점입니다.
(그 전까지 위키는 비어 있는 게 정상입니다.)

자료를 넣고 싶어지면:  /wiki-upload
쌓인 자료를 지식으로:  /gardener
""")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n중단했습니다.")
        sys.exit(130)
