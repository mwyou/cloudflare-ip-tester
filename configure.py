"""Interactive Docker setup; the Token prompt does not echo credentials."""
import getpass
import os
from pathlib import Path
import re

from cloudflare import validate_domain


def main():
    target = Path(__file__).resolve().parent / ".env"
    if target.exists():
        raise SystemExit(".env 已存在；请直接编辑它，本工具不会覆盖已有凭据。")
    try:
        domain = validate_domain(input("优选域名（必填，例如 cf.example.com）: ").strip())
    except ValueError as error:
        raise SystemExit(str(error)) from None
    token = getpass.getpass("Cloudflare API Token（输入不显示）: ").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{10,512}", token):
        raise SystemExit("Token 为空或格式不正确，未创建配置文件。")
    zone = input("Zone ID（可留空自动查找）: ").strip()
    if zone and not re.fullmatch(r"[a-fA-F0-9]{32}", zone):
        raise SystemExit("Zone ID 应为 32 位十六进制字符。")
    text = target.with_name(".env.example").read_text(encoding="utf-8")
    text = re.sub(r"(?m)^CF_DOMAIN=.*$", lambda _: "CF_DOMAIN=" + domain, text, count=1)
    text = text.replace("CF_API_TOKEN=\n", "CF_API_TOKEN=" + token + "\n", 1)
    text = text.replace("CF_ZONE_ID=\n", "CF_ZONE_ID=" + zone + "\n", 1)
    handle = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(text)
    print("已创建 .env。运行 docker compose up -d 启动自动优选。")


if __name__ == "__main__":
    main()
