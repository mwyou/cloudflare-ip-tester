"""Manually invoked, repository-scoped Docker Hub tag cleanup. Never delete digests."""
import json
import os
import re
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

REPOSITORY = "cotelforce/cloudflare-ip-tester"
BASE = "https://hub.docker.com"


def version(value):
    if not re.fullmatch(r"\d+\.\d+\.\d+", value):
        raise ValueError("Expected a numeric MAJOR.MINOR.PATCH version")
    return tuple(map(int, value.split(".")))


def other_version_tags(tags, keep):
    version(keep)
    return sorted(name for name in tags if name != keep and re.fullmatch(r"\d+\.\d+\.\d+", name))


def main():
    keep = os.environ["KEEP_VERSION"]
    version(keep)
    if os.environ["DOCKERHUB_USERNAME"] != "cotelforce":
        raise ValueError("Unexpected Docker Hub account")
    token = None

    def request(path, method="GET", data=None):
        url = path if path.startswith("https://") else BASE + path
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "hub.docker.com":
            raise ValueError("Unexpected API pagination destination")
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        req = Request(url, json.dumps(data).encode() if data else None, headers, method=method)
        try:
            with urlopen(req, timeout=30) as response:
                body = response.read()
                return json.loads(body) if body else None
        except HTTPError as exc:
            # Never print response bodies or credential-bearing requests.
            raise RuntimeError(f"Docker Hub {method} {parsed.path}: HTTP {exc.code}") from None

    session = request("/v2/auth/token", "POST", {
        "identifier": os.environ["DOCKERHUB_USERNAME"], "secret": os.environ["DOCKERHUB_TOKEN"]})
    token = session["access_token"]
    print("::add-mask::" + token, flush=True)
    path = f"/v2/namespaces/cotelforce/repositories/cloudflare-ip-tester/tags"

    def list_tags():
        tags = {}
        next_page = path + "?page_size=100"
        while next_page:
            result = request(next_page)
            tags.update((tag["name"], tag) for tag in result["results"])
            next_page = result.get("next")
        return tags

    tags = list_tags()
    # Both release and latest must already exist and point at the same image.
    if not tags.get(keep, {}).get("digest") or tags[keep]["digest"] != tags.get("latest", {}).get("digest"):
        raise ValueError("Kept version and latest must reference the same published image")
    candidates = other_version_tags(tags, keep)
    print(json.dumps({"keep": [keep, "latest"], "delete": candidates}), flush=True)
    if os.environ.get("APPLY") != "true":
        return
    for tag in candidates:
        request(f"/v2/repositories/{REPOSITORY}/tags/{tag}/", "DELETE")
        print("Deleted version tag " + tag, flush=True)
    remaining = list_tags()
    if any(tag in remaining for tag in candidates):
        raise RuntimeError("Some old version tags still exist")
    for tag in (keep, "latest"):
        if remaining.get(tag, {}).get("digest") != tags[tag]["digest"]:
            raise RuntimeError("Kept tag changed during cleanup")
    print(json.dumps({"remaining": sorted(remaining)}), flush=True)


if __name__ == "__main__":
    main()
