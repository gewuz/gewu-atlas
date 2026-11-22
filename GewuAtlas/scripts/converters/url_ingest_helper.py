#!/usr/bin/env python3
"""URL list parsing and SSRF-safe downloader adapted from 116-gwf-v2-R6 url_ingest."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from http.client import HTTPException
from pathlib import Path
from typing import Dict, List, Optional, Tuple


def parse_url_request(text: str) -> Optional[Tuple[List[str], str, Dict[str, str]]]:
    """Parse standalone URL list lines in DropZone markdown (skip if starts with frontmatter)."""
    if text.lstrip("\ufeff").startswith("---"):
        return None
    urls: List[str] = []
    global_lines: List[str] = []
    notes: Dict[str, List[str]] = {}
    current: Optional[str] = None
    fence = ""
    for line in text.splitlines():
        stripped = line.strip()
        marker = re.match(r"^(`{3,}|~{3,})", stripped)
        if marker:
            if not fence:
                fence = marker.group(1)[0]
            elif marker.group(1)[0] == fence:
                fence = ""
        candidate = re.sub(r"^(?:[-*+] |\d+[.)] )", "", stripped)
        match = re.fullmatch(r"\[[^\]\n]*\]\((https?://[^\s]+)\)", candidate)
        value = match.group(1) if match else candidate.strip("<>")
        is_url = bool(not fence and not marker and re.fullmatch(r'https?://[^\s<>`"|]+', value))
        if is_url:
            parsed = urllib.parse.urlsplit(value)
            if not parsed.hostname or parsed.username or parsed.password:
                return None
            if value not in urls:
                urls.append(value)
                notes[value] = []
            current = value
        else:
            (global_lines if current is None else notes[current]).append(line)
    if not urls:
        return None
    return (
        urls,
        "\n".join(global_lines).strip(),
        {u: "\n".join(lines).strip() for u, lines in notes.items()},
    )


def extract_url_list(text: str) -> Optional[List[str]]:
    req = parse_url_request(text)
    return req[0] if req else None


_PUBLIC_DNS_CACHE: Dict[str, Tuple[float, List[ipaddress._BaseAddress]]] = {}
MAX_URL_DOWNLOAD_BYTES = 50 * 1024 * 1024  # 50 MiB hard cap per ingested file


def _verify_connected_peer_ip(root: Path, response: object) -> None:
    """Inspect connected socket peer IP (if available) to defeat DNS Rebinding to private/loopback addresses."""
    sock_obj = None
    for chain in (("fp", "raw", "_sock"), ("fp", "_sock"), ("_sock",)):
        cur = response
        for attr in chain:
            cur = getattr(cur, attr, None)
            if cur is None:
                break
        if cur is not None and hasattr(cur, "getpeername"):
            sock_obj = cur
            break
    if sock_obj is None:
        return
    try:
        peer = sock_obj.getpeername()
    except Exception:
        return
    if not isinstance(peer, tuple) or not peer or not isinstance(peer[0], str):
        return
    try:
        addr = ipaddress.ip_address(peer[0])
    except ValueError:
        return
    fixture = _is_test_fixture_root(root)
    if fixture and addr.is_loopback:
        return
    if addr.version == 4 and addr in ipaddress.ip_network("198.18.0.0/15"):
        return
    if not addr.is_global:
        raise ValueError("检测到 DNS Rebinding 私网/环回连接，已阻断")


def public_dns_addresses(host: str):
    cached = _PUBLIC_DNS_CACHE.get(host)
    if cached and cached[0] > time.monotonic():
        return cached[1]

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            raise ValueError("公共DNS核验不接受重定向")

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    addresses = []
    ttl = 60
    for kind in ("A", "AAAA"):
        endpoint = "https://cloudflare-dns.com/dns-query?" + urllib.parse.urlencode({"name": host, "type": kind})
        request = urllib.request.Request(endpoint, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/dns-json"})
        payload = b""
        for attempt in range(3):
            try:
                with opener.open(request, timeout=20) as response:
                    payload = response.read(1048577)
                break
            except (OSError, HTTPException):
                if attempt == 2:
                    raise
        if len(payload) > 1048576:
            raise OSError("公共DNS核验响应过大")
        try:
            data = json.loads(payload)
        except (ValueError, UnicodeError) as exc:
            raise OSError("公共DNS响应无法解析") from exc
        if not isinstance(data, dict):
            raise OSError("公共DNS响应结构无效")
        if data.get("Status") != 0:
            raise OSError("公共DNS核验失败")
        answers = data.get("Answer", [])
        if not isinstance(answers, list):
            raise OSError("公共DNS响应记录结构无效")
        for answer in answers:
            if not isinstance(answer, dict):
                raise OSError("公共DNS响应记录结构无效")
            if answer.get("type") in (1, 28):
                try:
                    value = answer["data"]
                    if not isinstance(value, str):
                        raise ValueError("address must be text")
                    address = ipaddress.ip_address(value)
                    if address.version != (4 if answer["type"] == 1 else 6):
                        raise ValueError("address family mismatch")
                    answer_ttl = int(answer.get("TTL", 0))
                except (KeyError, TypeError, ValueError, OverflowError) as exc:
                    raise OSError("公共DNS响应地址或TTL无效") from exc
                addresses.append(address)
                ttl = min(ttl, max(0, answer_ttl))
    if not addresses:
        raise OSError("公共DNS未返回目标地址")
    if any(not addr.is_global for addr in addresses):
        raise ValueError("公共DNS未确认目标为公网地址")
    if ttl:
        _PUBLIC_DNS_CACHE[host] = (time.monotonic() + ttl, addresses)
    return addresses


def _is_test_fixture_root(root: Path) -> bool:
    marker = root / ".gewu-url-test-root"
    try:
        return marker.is_file() and marker.read_text(encoding="utf-8").strip() == root.name
    except OSError:
        return False


def check_url(root: Path, url: str, *, resolve_dns: bool = True) -> None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("仅允许不携带凭据的 HTTP(S) URL")
    fixture = _is_test_fixture_root(root)
    synthetic = False
    try:
        literal = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        literal = None
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if literal is not None and not literal.is_global and not (fixture and literal.is_loopback):
        raise ValueError("禁止访问私网/环回/保留地址")
    if not resolve_dns:
        return  # Offline planning only; actual requests always perform DNS/redirect checks.
    results = socket.getaddrinfo(parsed.hostname, port)
    if not results:
        raise OSError("目标域名未解析到地址")
    for item in results:
        addr = ipaddress.ip_address(item[4][0])
        if fixture and addr.is_loopback:
            continue
        if not addr.is_global:
            if literal is None and addr.version == 4 and addr in ipaddress.ip_network("198.18.0.0/15"):
                synthetic = True
            else:
                raise ValueError("禁止访问私网/环回/保留地址")
    if synthetic:
        public_dns_addresses(parsed.hostname)


def download_url(root: Path, url: str, stage: Path, library: Optional[Path] = None) -> Path:
    check_url(root, url)

    class SafeRedirect(urllib.request.HTTPRedirectHandler):
        http_error_308 = urllib.request.HTTPRedirectHandler.http_error_302

        def redirect_request(self, req, fp, code, msg, headers, newurl):
            check_url(root, newurl)
            return super().redirect_request(req, fp, 307 if code == 308 else code, msg, headers, newurl)

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), SafeRedirect())
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; Gewu-URL-Ingest/2)"})
    response = None
    for attempt in range(3):
        try:
            response = opener.open(request, timeout=30)
            break
        except urllib.error.HTTPError:
            raise
        except (OSError, HTTPException):
            if attempt == 2:
                raise
            check_url(root, url)
    assert response is not None
    with response:
        check_url(root, response.geturl())
        _verify_connected_peer_ip(root, response)
        expected = response.headers.get("Content-Length")
        if expected is not None:
            if not expected.strip().isdigit():
                raise ValueError("HTTP Content-Length 无效")
            if int(expected) > MAX_URL_DOWNLOAD_BYTES:
                raise ValueError("下载内容超过最大体积上限")
        # HTTP names and MIME are hints; PDF signatures take precedence over either.
        first = response.read(65536)
        is_pdf = first.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"%PDF-")
        mime = response.headers.get_content_type()
        mapping = {
            "text/html": ".html",
            "application/xhtml+xml": ".html",
            "application/pdf": ".pdf",
            "text/plain": ".txt",
            "text/markdown": ".txt",
            "text/x-markdown": ".txt",
            "text/csv": ".csv",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
        }
        suffix = mapping.get(mime)
        if suffix is None:
            name = response.headers.get_filename() or ""
            suffix = (
                Path(name).suffix.lower()
                or Path(urllib.parse.unquote(urllib.parse.urlsplit(response.geturl()).path)).suffix.lower()
            )
        if suffix in {".md", ".markdown"}:
            suffix = ".txt"
        if is_pdf:
            suffix = ".pdf"
        if suffix not in {".html", ".htm", ".pdf", ".docx", ".pptx", ".xlsx", ".csv", ".txt", ".rtf"}:
            raise ValueError("下载格式暂无转译支持: " + mime)
        for part in (stage, *stage.parents):
            if part == root:
                break
            if part.is_symlink():
                raise ValueError("下载临时路径不能包含软链")
        stage.mkdir(parents=True, exist_ok=True)
        stem = "URL-" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:20]
        version = 1
        chosen = stem
        while library is not None and (library / (chosen + ".md")).exists():
            version += 1
            chosen = f"{stem}-{version}"
        path = stage / (chosen + suffix)
        if path.is_symlink():
            raise ValueError("下载临时文件不能为软链")
        total_written = len(first)
        if total_written > MAX_URL_DOWNLOAD_BYTES:
            raise ValueError("下载内容超过最大体积上限")
        with path.open("wb") as handle:
            handle.write(first)
            while True:
                data = response.read(65536)
                if not data:
                    break
                total_written += len(data)
                if total_written > MAX_URL_DOWNLOAD_BYTES:
                    handle.close()
                    path.unlink(missing_ok=True)
                    raise ValueError("下载内容超过最大体积上限")
                handle.write(data)
        if not path.stat().st_size:
            raise ValueError("下载内容为空")
        if expected is not None and path.stat().st_size != int(expected):
            raise ValueError("下载不完整，内容长度与HTTP声明不一致")
        return path
