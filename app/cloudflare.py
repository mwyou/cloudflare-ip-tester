"""Cloudflare proxy ranges and input validation for the Docker service."""
from __future__ import annotations

import ipaddress
import re
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler


API = "https://api.cloudflare.com/client/v4"

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


def validate_url(value: str) -> str:
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 32 or c.isspace() for c in value):
        raise ValueError("测速 URL 格式不正确")
    try:
        parsed = urlsplit(value)
        port = parsed.port
        host = parsed.hostname
        if parsed.scheme not in ("http", "https") or not host or parsed.username or parsed.password:
            raise ValueError()
        if port not in (None, 80, 443) or parsed.fragment:
            raise ValueError()
        # A hostname is needed for certificate validation and Host routing.
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ValueError()
        host = host.encode("idna").decode("ascii")
        if not all(label and len(label) <= 63 and all(c.isalnum() or c == "-" for c in label)
                   for label in host.split(".")):
            raise ValueError()
        authority = host + (f":{port}" if port else "")
        return urlunsplit((parsed.scheme, authority, quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~"),
                           quote(parsed.query, safe="%=&?/:;+,$@!()*'-._~"), ""))
    except (ValueError, UnicodeError):
        raise ValueError("测速地址需为 HTTP(S) 下载 URL，端口仅支持 80 / 443") from None
