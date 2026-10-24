"""Read-only Cloudflare DNS access. Credentials are never retained."""
from __future__ import annotations

import ipaddress
import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

API = "https://api.cloudflare.com/client/v4"
MAX_RECORDS = 100
# Official Cloudflare proxy networks: https://www.cloudflare.com/ips/
CF_NETWORKS = tuple(ipaddress.ip_network(net) for net in (
    "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22",
    "141.101.64.0/18", "108.162.192.0/18", "190.93.240.0/20", "188.114.96.0/20",
    "197.234.240.0/22", "198.41.128.0/17", "162.158.0.0/15", "104.16.0.0/13",
    "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22", "2400:cb00::/32",
    "2606:4700::/32", "2803:f800::/32", "2405:b500::/32", "2405:8100::/32",
    "2a06:98c0::/29", "2c0f:f248::/32",
))


def is_cloudflare_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
        return address.is_global and any(address in net for net in CF_NETWORKS)
    except ValueError:
        return False


def validate_domain(value: str) -> str:
    value = value.strip().rstrip(".").lower()
    try:
        value = value.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("域名格式不正确") from exc
    labels = value.split(".")
    if len(value) > 253 or len(labels) < 2 or any(
        not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
        for label in labels
    ):
        raise ValueError("请输入完整 DNS 域名，例如 cf.example.com")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return value
    raise ValueError("这里需要填写域名，而不是 IP")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def cf_api(path: str, token: str, params: dict | None = None) -> dict:
    url = API + path + ("?" + urlencode(params) if params else "")
    req = Request(url, headers={"Authorization": f"Bearer {token}",
                               "Accept": "application/json", "User-Agent": "CFIPDesk/2.0"})
    try:
        # Do not send a credential through a configured HTTP proxy or a redirect.
        with build_opener(ProxyHandler({}), NoRedirect()).open(req, timeout=12) as response:
            data = json.loads(response.read(4 * 1024 * 1024))
    except HTTPError as exc:
        message = {401: "API Token 无效", 403: "Token 缺少 Zone Read / DNS Read 权限",
                   429: "Cloudflare API 请求过于频繁，请稍后重试"}.get(exc.code)
        raise ValueError(message or f"Cloudflare API 请求失败（HTTP {exc.code}）") from None
    except (URLError, TimeoutError, OSError):
        raise ValueError("连接 Cloudflare API 失败，请检查本机网络") from None
    except (json.JSONDecodeError, UnicodeError):
        raise ValueError("Cloudflare API 返回了无效响应") from None
    if not isinstance(data, dict) or not data.get("success"):
        # Avoid echoing an upstream error containing user-supplied values.
        raise ValueError("Cloudflare API 拒绝了请求，请检查 Token 权限和 Zone ID")
    return data


def get_zone_id(domain: str, token: str, provided: str = "") -> str:
    if provided:
        if not re.fullmatch(r"[a-fA-F0-9]{32}", provided):
            raise ValueError("Zone ID 应为 32 位十六进制字符串")
        return provided
    labels = domain.split(".")
    for offset in range(len(labels) - 1):
        candidate = ".".join(labels[offset:])
        data = cf_api("/zones", token, {"name": candidate, "per_page": 50})
        for zone in data.get("result", []):
            if zone.get("name") == candidate:
                return zone["id"]
    raise ValueError("Token 可访问的 Zone 中没有找到此域名；也可以手动填写 Zone ID")


def fetch_records(domain: str, token: str, provided_zone: str = "") -> dict:
    zone = get_zone_id(domain, token, provided_zone.strip())
    unique = {}
    truncated = False
    for kind in ("A", "AAAA"):
        page = 1
        while True:
            data = cf_api(f"/zones/{zone}/dns_records", token,
                          {"name": domain, "type": kind, "per_page": 100, "page": page})
            entries = data.get("result", [])
            for record in entries:
                if record.get("name", "").lower().rstrip(".") != domain:
                    continue
                try:
                    ip = str(ipaddress.ip_address(record["content"]))
                except (ValueError, KeyError):
                    continue
                if len(unique) >= MAX_RECORDS and ip not in unique:
                    truncated = True
                    break
                unique[ip] = {"ip": ip, "type": kind, "proxied": bool(record.get("proxied")),
                              "eligible": is_cloudflare_ip(ip), "ttl": record.get("ttl")}
            if truncated:
                break
            pages = int(data.get("result_info", {}).get("total_pages", 1))
            if page >= pages or not entries:
                break
            page += 1
            if page > 10:
                truncated = True
                break
        if truncated:
            break
    if not unique:
        raise ValueError("该域名没有直接配置的 A / AAAA 记录（CNAME 不会展开）")
    return {"domain": domain, "zone_id": zone, "records": list(unique.values()),
            "truncated": truncated}
