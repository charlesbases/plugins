"""Bounded, allowlisted HTTPS retrieval with an auditable redirect chain.

Transport provenance records what this client obtained, not a proof that the
publisher's claims are true. No redirects are followed by urllib itself.
"""

import codecs
import datetime as dt
import hashlib
import io
import json
import os
import re
import shutil
import ssl
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from email.parser import BytesParser
from functools import lru_cache

from contracts import fingerprint

REGISTRY_PATH = Path(__file__).with_name("source_registry.json")
MAX_BYTES = 16 * 1024 * 1024
REDIRECTS = {301, 302, 303, 307, 308}


class SourceError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise SourceError(message)


def load_registry():
    with REGISTRY_PATH.open(encoding="utf-8") as stream:
        value = json.load(stream)
    require(value.get("schema_version") == 1 and isinstance(value.get("sources"), dict), "Invalid source registry")
    return value


def source_rule(source_id, registry=None):
    registry = registry if registry is not None else load_registry()
    require(source_id in registry["sources"], "Source is not registered: " + str(source_id))
    return registry["sources"][source_id]


def checked_url(url, rule, redirected=False):
    require(isinstance(url, str) and len(url) <= 8192, "Invalid source URL")
    parsed = urllib.parse.urlsplit(url)
    require(parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password,
            "Source URL must use HTTPS without embedded credentials")
    require(not parsed.fragment and parsed.port in (None, 443), "Unsupported URL fragment or port")
    host = parsed.hostname.lower()
    require(host in rule["allowed_hosts"] + (rule["redirect_hosts"] if redirected else []), "Source host is not allowed")
    path = parsed.path
    if rule.get("normalize_double_press_slash") and path.startswith("//press/"):
        path = path[1:]
    require(re.search(rule["path_pattern"], path), "Source path is not allowed")
    return urllib.parse.urlunsplit(("https", host, urllib.parse.quote(path or "/", safe="/%:@-._~!$&'()*+,;="),
                                  urllib.parse.quote(parsed.query, safe="=&%/:?@-._~!$'()*+,;"), ""))


def identify_source(url, registry=None):
    registry = registry if registry is not None else load_registry()
    matches = []
    for name, rule in registry["sources"].items():
        try:
            checked_url(url, rule)
            matches.append(name)
        except (SourceError, ValueError):
            continue
    require(len(matches) == 1, "URL needs one unambiguous registered source")
    return matches[0]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@lru_cache(maxsize=1)
def _native_curl():
    binary = shutil.which("curl")
    require(binary is not None, "Windows HTTPS requires curl with Schannel system trust")
    result = subprocess.run([binary, "-q", "--version"], capture_output=True, timeout=5,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    version = result.stdout.decode("utf-8", errors="strict")
    require(result.returncode == 0 and "Schannel" in version, "Windows curl must use Schannel certificate validation")
    return binary, version.splitlines()[0]


class _CapturedResponse:
    def __init__(self, url, status, headers, data, version):
        self.url, self.status, self.headers, self.stream = url, status, headers, io.BytesIO(data)
        self.transport = "HTTPS_Windows_Schannel_certificate_validation"
        self.transport_details = {"backend": "Schannel", "curl_version": version}
    def geturl(self): return self.url
    def read(self, size): return self.stream.read(size)
    read1 = read
    def __enter__(self): return self
    def __exit__(self, *args): self.stream.close()


def _curl_open(url, timeout, max_bytes=MAX_BYTES, *, headers=None):
    binary, version = _native_curl()
    with tempfile.TemporaryDirectory(prefix="investment-https-") as temporary:
        work = Path(temporary)
        header_path, body_path = work / "headers", work / "body"
        command = [binary, "-q", "--silent", "--show-error", "--proto", "=https", "--max-redirs", "0",
                   "--max-time", str(timeout), "--connect-timeout", str(timeout), "--max-filesize", str(max_bytes),
                   "--header", "Accept-Encoding: identity", "--user-agent", "InvestmentResearch/0.0.1",
                   "--dump-header", str(header_path), "--output", str(body_path), "--write-out", "%{json}", url]
        for key, value in (headers or {}).items():
            command[2:2] = ["--header", key+": "+value]
        result = subprocess.run(command, capture_output=True, timeout=timeout+1,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        require(result.returncode == 0, "Verified HTTPS transport failed: " + result.stderr.decode("utf-8", errors="replace")[:400])
        metadata = json.loads(result.stdout.decode("utf-8"))
        require(metadata.get("ssl_verify_result") == 0 and metadata.get("url_effective") == url,
                "Native HTTPS validation or final URL differs")
        blocks = [block for block in header_path.read_bytes().replace(b"\r\n", b"\n").split(b"\n\n") if block.startswith(b"HTTP/")]
        require(blocks, "HTTPS response headers are absent")
        status_line, header_bytes = blocks[-1].split(b"\n", 1)
        status = int(status_line.split()[1])
        require(status == metadata.get("http_code"), "Native response status mismatch")
        require(body_path.stat().st_size <= max_bytes, "Oversized source response")
        return _CapturedResponse(url, status, BytesParser().parsebytes(header_bytes), body_path.read_bytes(), version)


def _urllib_open(url, timeout, *, fallback_from=None, headers=None):
    context = ssl.create_default_context()
    require(context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED, "HTTPS fallback must verify certificate and hostname")
    opener = urllib.request.build_opener(_NoRedirect(), urllib.request.HTTPSHandler(context=context))
    request = urllib.request.Request(url, headers={"User-Agent": "InvestmentResearch/0.0.1", "Accept-Encoding": "identity", **(headers or {})})
    try:
        response = opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        if error.code in REDIRECTS:
            response = error
        else:
            raise SourceError("Source HTTP failure: " + str(error.code)) from error
    response.transport = "HTTPS_default_certificate_validation"
    response.transport_details = {"backend": ssl.OPENSSL_VERSION, "certificate_verification": "required", "hostname_check": True}
    if fallback_from is not None:
        response.transport_details["fallback_from"] = fallback_from
    return response


def _open(url, timeout, max_bytes=MAX_BYTES, *, headers=None):
    extra = {"headers": headers} if headers else {}
    if os.name == "nt":
        started = time.monotonic()
        try:
            return _curl_open(url, timeout, max_bytes, **extra)
        except (SourceError, OSError, subprocess.SubprocessError) as error:
            remaining = timeout-(time.monotonic()-started)
            require(remaining > 0, "Verified HTTPS source budget exhausted before certificate-validating fallback")
            return _urllib_open(url, remaining, fallback_from={"backend": "Schannel", "reason": str(error)[:400]}, **extra)
    return _urllib_open(url, timeout, **extra)


def _registered_headers(rule):
    referer = rule.get("referer")
    if referer is None:
        return {}
    parsed = urllib.parse.urlsplit(referer)
    require(parsed.scheme == "https" and parsed.hostname in rule["referer_hosts"]
            and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment
            and parsed.path == "/" and parsed.port in (None, 443), "Unregistered source Referer")
    return {"Referer": referer}


def decode(data, content_type, charset, rule):
    if content_type in ("application/pdf", "application/octet-stream"):
        return None, None
    declared = charset
    if data.startswith(codecs.BOM_UTF8):
        declared = "utf-8-sig"
    if not declared:
        prefix = data[:4096].decode("ascii", errors="ignore")
        match = re.search(r"charset\s*=\s*[\"']?([A-Za-z0-9_-]+)", prefix, re.I)
        declared = match.group(1) if match else (rule.get("encoding") or "utf-8")
    try:
        encoding = codecs.lookup(declared).name
        require(encoding in {"utf-8", "utf-8-sig", "gb2312", "gbk", "gb18030", "ascii"}, "Unsupported declared source encoding")
        return data.decode(encoding, errors="strict"), encoding
    except (LookupError, UnicodeError) as error:
        raise SourceError("Source text cannot be decoded losslessly") from error


def validate_capture_provenance(record, registry=None):
    registry = registry if registry is not None else load_registry()
    rule = source_rule(record["registry_source_id"], registry)
    require(record["registry_hash"] == fingerprint(registry), "Source registry identity changed")
    require(record.get("request_headers", {}) == _registered_headers(rule), "Source request header identity changed")
    current = checked_url(record["requested_url"], rule)
    chain = record["redirect_chain"]
    require(isinstance(chain, list) and len(chain) <= 5, "Invalid redirect inventory")
    seen = {current}
    for hop in chain:
        require(set(hop) == {"url", "status", "location", "next_url"} and hop["url"] == current
                and type(hop["status"]) is int and hop["status"] in REDIRECTS, "Redirect chain identity mismatch")
        nxt = checked_url(urllib.parse.urljoin(current, hop["location"]), rule, True)
        require(nxt == hop["next_url"] and nxt not in seen, "Invalid redirect target or cycle")
        seen.add(nxt)
        current = nxt
    require(current == record["final_url"] and record["http_status"] == 200, "Final source URL/status mismatch")
    require(record["transport"] in ("HTTPS_default_certificate_validation", "HTTPS_Windows_Schannel_certificate_validation"),
            "Unverified source transport")
    if record["transport"] == "HTTPS_Windows_Schannel_certificate_validation":
        require(record.get("transport_details", {}).get("backend") == "Schannel"
                and "Schannel" in record.get("transport_details", {}).get("curl_version", ""), "Missing native TLS identity")
    require(record["content_type"] in rule["media_types"], "Source content type is not registered")
    return True


def fetch(url, source_id, timeout=20, max_bytes=MAX_BYTES, registry=None):
    registry = registry if registry is not None else load_registry()
    rule = source_rule(source_id, registry)
    headers = _registered_headers(rule)
    require(type(timeout) in (int, float) and 0 < timeout <= 30, "Bounded source timeout required")
    require(type(max_bytes) is int and 0 < max_bytes <= MAX_BYTES, "Bounded source response limit required")
    current = requested = checked_url(url, rule)
    chain, seen = [], {current}
    deadline = time.monotonic() + timeout
    for _ in range(6):
        remaining = deadline - time.monotonic()
        require(remaining > 0, "Source elapsed-time budget exceeded")
        with _open(current, remaining, max_bytes, **({"headers": headers} if headers else {})) as response:
            require(response.geturl() == current, "Transport followed an unrecorded redirect")
            status = response.status
            if status in REDIRECTS:
                require(len(chain) < 5, "Too many source redirects")
                location = response.headers.get("Location")
                require(isinstance(location, str) and location, "Redirect has no location")
                nxt = checked_url(urllib.parse.urljoin(current, location), rule, True)
                require(nxt not in seen, "Source redirect cycle")
                chain.append({"url": current, "status": status, "location": location, "next_url": nxt})
                seen.add(nxt)
                current = nxt
                continue
            require(status == 200, "Source HTTP status is not successful")
            content_type = response.headers.get_content_type()
            require(content_type in rule["media_types"], "Source content type is not registered")
            require(response.headers.get("Content-Encoding", "identity").lower() == "identity", "Unsupported source content encoding")
            chunks, total = [], 0
            reader = getattr(response, "read1", response.read)
            while total <= max_bytes:
                require(time.monotonic() < deadline, "Source elapsed-time budget exceeded")
                chunk = reader(min(65536, max_bytes + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
            data = b"".join(chunks)
            require(0 < len(data) <= max_bytes, "Empty or oversized source response")
            text, encoding = decode(data, content_type, response.headers.get_content_charset(), rule)
            transport = getattr(response, "transport", "HTTPS_default_certificate_validation")
            transport_details = getattr(response, "transport_details", {"backend": ssl.OPENSSL_VERSION})
        record = {"registry_source_id": source_id, "registry_hash": fingerprint(registry),
                  "requested_url": requested, "final_url": current, "redirect_chain": chain,
                  "http_status": 200, "content_type": content_type, "encoding": encoding,
                  "transport": transport, "transport_details": transport_details,
                  "retrieved_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                  "raw_sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data),
                  "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest() if text is not None else None}
        if headers:
            record["request_headers"] = headers
        validate_capture_provenance(record, registry)
        return {**record, "raw_bytes": data, "text": text}
    raise SourceError("Source redirect budget exceeded")
