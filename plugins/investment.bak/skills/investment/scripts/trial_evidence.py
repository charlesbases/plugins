"""RFC 3161 digest timestamps under an explicitly pinned trust policy.

Only a SHA-256 timestamp query is transmitted; account/payload bytes stay local.
Signature, purpose and certificate dates are checked with OpenSSL at signed
genTime. This proves existence under the caller's CA policy, not unseen data,
independent authority governance, investment efficacy, or revocation status.
References: RFC 3161 sections 2.4 and 4; OpenSSL 3.5 openssl-ts/openssl-cms.
"""

import base64
import datetime as dt
import hashlib
import json
import math
import os
import re
import shutil
import ssl
import stat
import subprocess
import tempfile
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

MAX_PAYLOAD_BYTES = 16 * 1024 * 1024
MAX_RECEIPT_BYTES = 1024 * 1024
SHA256_OID = "2.16.840.1.101.3.4.2.1"
SIGNED_DATA_OID = "1.2.840.113549.1.7.2"
TST_INFO_OID = "1.2.840.113549.1.9.16.1.4"
UTC = dt.timezone.utc


class EvidenceError(ValueError):
    pass


class EvidenceTransportError(OSError):
    """The remote delivery result may be unknown; no business effect is implied."""


def require(condition, message):
    if not condition:
        raise EvidenceError(message)


def canonical_bytes(obj):
    """Deterministic project JSON encoding, not an RFC 8785 implementation."""
    def valid(value, depth=0):
        require(depth <= 64, "JSON nesting limit exceeded")
        if isinstance(value, dict):
            require(all(type(k) is str for k in value), "JSON keys must be strings")
            for child in value.values():
                valid(child, depth + 1)
        elif isinstance(value, list):
            for child in value:
                valid(child, depth + 1)
        else:
            require(value is None or type(value) in (str, int, float, bool), "Unsupported JSON value")
            require(type(value) is not float or math.isfinite(value), "Nonfinite JSON value")
    valid(obj)
    try:
        data = json.dumps(obj, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, UnicodeError) as exc:
        raise EvidenceError("Invalid JSON encoding") from exc
    require(len(data) <= MAX_PAYLOAD_BYTES, "Payload exceeds size limit")
    return data


def digest(obj):
    return hashlib.sha256(canonical_bytes(obj)).hexdigest()


def _payload(value):
    require(type(value) is bytes and len(value) <= MAX_PAYLOAD_BYTES,
            "Payload must be bounded bytes")
    return hashlib.sha256(value).hexdigest()


def _owned_path(value):
    require(isinstance(value, (str, os.PathLike)), "Evidence path required")
    path = Path(value).absolute()
    require(".." not in path.parts, "Parent traversal is not allowed")
    for item in reversed((path,) + tuple(path.parents)):
        require(not item.is_symlink() and not (hasattr(item, "is_junction") and item.is_junction()),
                "Linked evidence path is not allowed")
        if item.exists() and item != path:
            require(item.is_dir(), "Evidence ancestor must be a directory")
    require(path.resolve() == path, "Evidence path is redirected")
    return path


def _read(path, limit=MAX_RECEIPT_BYTES):
    path = _owned_path(path)
    info = path.stat()
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "Owned regular file required")
    require(0 < info.st_size <= limit, "Evidence file is empty or exceeds size limit")
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    require(len(data) == info.st_size, "Evidence file changed or exceeds size limit")
    return data


def _number(value, name, maximum):
    require(type(value) in (int, float) and math.isfinite(value) and 0 <= value <= maximum,
            name + " must be a bounded nonnegative number")
    return float(value)


def _trust(config):
    require(isinstance(config, dict), "Pinned timestamp trust configuration required")
    url = config.get("tsa_url")
    require(isinstance(url, str), "HTTP(S) TSA URL required")
    parsed = urllib.parse.urlsplit(url)
    require(parsed.scheme in ("http", "https") and parsed.hostname and not parsed.username
            and not parsed.password and not parsed.fragment, "TSA URL must use HTTP(S) without credentials or fragment")
    ca_value = config.get("ca_file")
    require(isinstance(ca_value, (str, os.PathLike)), "Pinned ca_file required")
    ca = Path(ca_value)
    require(ca.is_absolute(), "Pinned ca_file must be absolute")
    ca_bytes = _read(ca)
    ca_hash = config.get("ca_sha256")
    require(isinstance(ca_hash, str) and re.fullmatch(r"[a-f0-9]{64}", ca_hash)
            and hashlib.sha256(ca_bytes).hexdigest() == ca_hash, "Pinned CA bundle hash differs")
    policy = config.get("policy_oid")
    require(isinstance(policy, str) and len(policy) <= 256
            and re.fullmatch(r"(?:0|1|2)(?:\.(?:0|[1-9][0-9]*)){1,31}", policy),
            "Pinned numeric timestamp policy_oid required")
    parts = [int(part) for part in policy.split(".")]
    require(parts[0] == 2 or parts[1] < 40, "Invalid timestamp policy OID")
    skew = _number(config.get("max_clock_skew_seconds", 0), "max_clock_skew_seconds", 300)
    timeout = _number(config.get("timeout_seconds", 20), "timeout_seconds", 30)
    require(timeout > 0, "Positive timeout required")
    accuracy = config.get("policy_accuracy_seconds")
    if accuracy is not None:
        accuracy = _number(accuracy, "policy_accuracy_seconds", 300)
    signer = config.get("signer_sha256")
    require(signer is None or isinstance(signer, str) and re.fullmatch(r"[a-f0-9]{64}", signer),
            "signer_sha256 must be a DER certificate SHA-256")
    supplied = config.get("openssl_binary")
    require(supplied is None or isinstance(supplied, str) and supplied, "Invalid OpenSSL executable")
    binary = shutil.which(supplied or "openssl")
    require(binary is not None, "OpenSSL executable not available")
    return {"url": url, "ca_bytes": ca_bytes, "ca_sha256": ca_hash, "policy_oid": policy,
            "skew": skew, "timeout": timeout, "accuracy": accuracy, "signer_sha256": signer,
            "binary": str(Path(binary).resolve())}


def _run(binary, arguments, directory, timeout):
    env = dict(os.environ, OPENSSL_CONF=os.devnull)
    try:
        result = subprocess.run([binary, *map(str, arguments)], cwd=directory, env=env,
                                capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise EvidenceError("OpenSSL command could not complete") from exc
    require(result.returncode == 0,
            "OpenSSL verification/encoding failed: " + result.stderr.decode("utf-8", errors="replace")[-1500:])
    require(len(result.stdout) <= MAX_RECEIPT_BYTES, "OpenSSL output exceeds size limit")
    return result.stdout


def _tlv(data, offset=0):
    start = offset
    require(offset + 2 <= len(data), "Truncated DER")
    tag, size = data[offset], data[offset + 1]
    require(tag & 31 != 31, "High-tag DER is unsupported")
    offset += 2
    if size & 128:
        count = size & 127
        require(1 <= count <= 4 and offset + count <= len(data), "Invalid DER length")
        require(data[offset] != 0, "Noncanonical DER length")
        size = int.from_bytes(data[offset:offset + count], "big")
        require(size >= 128, "Noncanonical DER length")
        offset += count
    end = offset + size
    require(end <= len(data), "Truncated DER value")
    return (tag, data[offset:end], data[start:end]), end


def _children(content):
    items, offset = [], 0
    while offset < len(content):
        require(len(items) < 4096, "Too many DER fields")
        item, offset = _tlv(content, offset)
        items.append(item)
    return items


def _sequence(data):
    item, end = _tlv(data)
    require(item[0] == 48 and end == len(data), "Single DER sequence required")
    return _children(item[1])


def _integer(node, tag=2):
    value = node[1]
    require(node[0] == tag and 1 <= len(value) <= 21 and not value[0] & 128
            and (len(value) == 1 or value[0] != 0 or value[1] & 128), "Invalid unsigned DER integer")
    return int.from_bytes(value, "big")


def _oid(node):
    require(node[0] == 6 and 1 <= len(node[1]) <= 256, "Bounded DER OID required")
    parts, value, continued = [], 0, False
    for byte in node[1]:
        require(continued or byte != 128, "Noncanonical OID")
        value = (value << 7) | (byte & 127)
        continued = bool(byte & 128)
        if not continued:
            parts.append(value)
            value = 0
    require(not continued and parts, "Truncated OID")
    first = min(parts[0] // 40, 2)
    return ".".join(map(str, [first, parts[0] - first * 40, *parts[1:]]))


def _imprint(node):
    fields = _sequence(node[2])
    require(len(fields) == 2, "Malformed message imprint")
    algorithm = _sequence(fields[0][2])
    require(len(algorithm) in (1, 2) and _oid(algorithm[0]) == SHA256_OID,
            "Timestamp imprint must use SHA-256")
    require(len(algorithm) == 1 or algorithm[1][2] == b"\x05\x00", "Unexpected digest parameters")
    require(fields[1][0] == 4 and len(fields[1][1]) == 32, "SHA-256 imprint must be 32 bytes")
    return fields[1][1].hex()


def _query(data):
    fields = _sequence(data)
    require(len(fields) == 5 and _integer(fields[0]) == 1, "Expected v1 query with policy, nonce and certificate request")
    require(fields[4][2] == b"\x01\x01\xff", "TSA signing certificate must be requested")
    nonce = _integer(fields[3])
    require(nonce > 0 and nonce.bit_length() <= 160, "Positive bounded query nonce required")
    return {"imprint": _imprint(fields[1]), "policy_oid": _oid(fields[2]), "nonce": nonce}


def _signed_info(response):
    outer = _sequence(response)
    require(len(outer) == 2, "Timestamp response has no token")
    status = _sequence(outer[0][2])
    require(status and _integer(status[0]) in (0, 1), "TSA did not grant a timestamp")
    token = _sequence(outer[1][2])
    require(len(token) == 2 and _oid(token[0]) == SIGNED_DATA_OID and token[1][0] == 160,
            "Timestamp requires CMS SignedData")
    signed = _sequence(token[1][1])
    require(len(signed) >= 4, "Truncated CMS SignedData")
    encapsulated = _sequence(signed[2][2])
    require(len(encapsulated) == 2 and _oid(encapsulated[0]) == TST_INFO_OID
            and encapsulated[1][0] == 160, "Signed content must be TSTInfo")
    octet, end = _tlv(encapsulated[1][1])
    require(octet[0] == 4 and end == len(encapsulated[1][1]), "DER TSTInfo content required")
    fields = _sequence(octet[1])
    require(len(fields) >= 5 and _integer(fields[0]) == 1, "Expected TSTInfo v1")
    require(fields[4][0] == 24, "Signed GeneralizedTime required")
    try:
        text = fields[4][1].decode("ascii")
        require(re.fullmatch(r"[0-9]{14}(?:\.[0-9]{0,5}[1-9])?Z", text), "Unsupported or noncanonical genTime")
        moment = dt.datetime.strptime(text, "%Y%m%d%H%M%S.%fZ" if "." in text else "%Y%m%d%H%M%SZ").replace(tzinfo=UTC)
    except (UnicodeError, ValueError) as exc:
        raise EvidenceError("Invalid signed genTime") from exc
    pos, accuracy = 5, None
    if pos < len(fields) and fields[pos][0] == 48:
        accuracy, last_tag = 0.0, -1
        order = {2: 0, 128: 1, 129: 2}
        for part in _sequence(fields[pos][2]):
            require(part[0] in order and order[part[0]] > last_tag, "Invalid timestamp accuracy")
            last_tag = order[part[0]]
            number = _integer(part, part[0])
            require(part[0] == 2 or 1 <= number <= 999, "Invalid fractional timestamp accuracy")
            accuracy += number / (1, 1000, 1000000)[last_tag]
        pos += 1
    if pos < len(fields) and fields[pos][0] == 1:
        require(fields[pos][2] == b"\x01\x01\xff", "Noncanonical ordering flag")
        pos += 1
    require(pos < len(fields) and fields[pos][0] == 2, "Signed nonce is missing")
    nonce = _integer(fields[pos])
    pos += 1
    remaining = [part[0] for part in fields[pos:]]
    require(remaining in ([], [160], [161], [160, 161]), "Unexpected TSTInfo fields")
    return {"token_der": outer[1][2], "content_der": octet[1], "gen_time": moment,
            "accuracy": accuracy, "nonce": nonce, "serial": str(_integer(fields[3])),
            "policy_oid": _oid(fields[1]), "imprint": _imprint(fields[2])}


def _moment(value):
    if isinstance(value, str):
        try:
            value = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise EvidenceError("Invalid cutoff time") from exc
    require(isinstance(value, dt.datetime) and value.tzinfo is not None and value.utcoffset() is not None,
            "Timezone-aware cutoff time required")
    return value.astimezone(UTC)


def _now():
    return dt.datetime.now(UTC)


def _verify(payload_bytes, query_bytes, response_bytes, trust, not_after):
    imprint = _payload(payload_bytes)
    query, info = _query(query_bytes), _signed_info(response_bytes)
    require(query["imprint"] == info["imprint"] == imprint, "Payload imprint differs")
    require(query["policy_oid"] == info["policy_oid"] == trust["policy_oid"], "Timestamp policy differs")
    require(query["nonce"] == info["nonce"], "Timestamp nonce differs")
    # Parse first only to select certificate verification time. No parsed time is
    # returned or trusted until BOTH CMS and RFC 3161 verification succeed.
    with tempfile.TemporaryDirectory(prefix="investment-timestamp-") as temporary:
        work = Path(temporary)
        for name, value in (("query.tsq", query_bytes), ("response.tsr", response_bytes),
                            ("token.der", info["token_der"]), ("ca.pem", trust["ca_bytes"])):
            (work / name).write_bytes(value)
        validation = ["-CAfile", "ca.pem", "-attime", str(int(info["gen_time"].timestamp())),
                      "-purpose", "timestampsign", "-x509_strict", "-auth_level", "2"]
        _run(trust["binary"], ["ts", "-verify", "-queryfile", "query.tsq", "-in", "response.tsr",
                              *validation], work, trust["timeout"])
        content = _run(trust["binary"], ["cms", "-verify", "-binary", "-inform", "DER", "-in", "token.der",
                       "-signer", "signer.pem", "-no-CApath", "-no-CAstore", "-cades", *validation], work, trust["timeout"])
        require(content == info["content_der"], "Verified content differs from parsed TSTInfo")
        pem = _read(work / "signer.pem")
        matches = re.findall(rb"-----BEGIN CERTIFICATE-----\s*([A-Za-z0-9+/=\r\n]+)-----END CERTIFICATE-----", pem)
        require(len(matches) == 1, "Exactly one timestamp signer required")
        signer_hash = hashlib.sha256(base64.b64decode(re.sub(rb"\s", b"", matches[0]), validate=True)).hexdigest()
        require(trust["signer_sha256"] is None or signer_hash == trust["signer_sha256"], "Timestamp signer pin differs")
    moment = info["gen_time"]
    require(moment <= _now() + dt.timedelta(seconds=trust["skew"]), "Timestamp is in the future")
    accuracy = info["accuracy"] if info["accuracy"] is not None else trust["accuracy"]
    require(accuracy is not None and math.isfinite(accuracy) and 0 <= accuracy <= 300,
            "Timestamp accuracy absent or unsupported; pin documented policy_accuracy_seconds")
    upper = moment + dt.timedelta(seconds=accuracy)
    if not_after is not None:
        require(upper <= _moment(not_after), "Timestamp upper time bound is later than cutoff")
    return {"verification": "passed", "verification_scope": "RFC3161_under_caller_pinned_trust",
            "gen_time": moment.isoformat(), "upper_time_bound": upper.isoformat(),
            "accuracy_seconds": accuracy, "imprint": imprint, "policy_oid": info["policy_oid"],
            "nonce": str(info["nonce"]), "serial": info["serial"], "signer_sha256": signer_hash,
            "ca_sha256": trust["ca_sha256"], "request_sha256": hashlib.sha256(query_bytes).hexdigest(),
            "response_sha256": hashlib.sha256(response_bytes).hexdigest(), "revocation_checked": False}


def verify_receipt(payload_bytes, receipt_dir, trust_config, not_after=None):
    """Verify exact payload bytes, not a saved JSON claim about token time."""
    trust = _trust(trust_config)
    folder = _owned_path(receipt_dir)
    require(folder.is_dir(), "Timestamp receipt directory required")
    require({p.name for p in folder.iterdir()} == {"request.tsq", "response.tsr"},
            "Receipt directory must contain only request.tsq and response.tsr")
    query = _read(folder / "request.tsq")
    response = _read(folder / "response.tsr")
    return _verify(payload_bytes, query, response, trust, not_after)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise EvidenceError("Timestamp endpoint redirects are not accepted")


def _post(url, data, timeout):
    # RFC 3161 section 3.4 permits HTTP: only the imprint/nonce query is sent.
    # Authentication comes from the pinned signing chain, not this transport.
    # Trust roots are local pinned bytes and are never fetched here.
    opener = urllib.request.build_opener(_NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    request = urllib.request.Request(url, data=data, method="POST", headers={
        "Content-Type": "application/timestamp-query", "Accept": "application/timestamp-reply"})
    try:
        with opener.open(request, timeout=timeout) as response:
            require(response.status == 200 and response.geturl() == url, "Unexpected TSA HTTP response")
            require(response.headers.get_content_type() == "application/timestamp-reply", "Unexpected TSA content type")
            value = response.read(MAX_RECEIPT_BYTES + 1)
    except OSError as exc:
        raise EvidenceTransportError("TSA delivery is unknown: " + str(exc)) from exc
    require(0 < len(value) <= MAX_RECEIPT_BYTES, "TSA response is empty or exceeds size limit")
    return value


def _atomic_evidence(path, value):
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("xb") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    if os.name != "nt":
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _resume_files(folder, payload_bytes, trust, not_after):
    """Recover owned pre-rename files; callers serialize the persistent operation."""
    for name in ("request.tsq", "response.tsr"):
        target = folder / name
        pending = sorted(folder.glob("." + name + ".*.tmp"))
        for candidate in pending:
            require(re.fullmatch(r"\." + re.escape(name) + r"\.[0-9a-f]{32}\.tmp", candidate.name),
                    "Unexpected evidence staging name")
            _owned_path(candidate)
            require(candidate.stat().st_nlink == 1, "Linked temporary evidence is not allowed")
            if not target.exists():
                try:
                    data = _read(candidate)
                    if name == "request.tsq":
                        query = _query(data)
                        require(query["imprint"] == _payload(payload_bytes)
                                and query["policy_oid"] == trust["policy_oid"], "Query binding differs")
                    else:
                        require((folder / "request.tsq").exists(), "Response has no fixed query")
                        _verify(payload_bytes, _read(folder / "request.tsq"), data, trust, not_after)
                except EvidenceError:
                    # An interrupted temporary write is not a committed receipt.
                    candidate.unlink()
                    continue
                os.replace(candidate, target)
            else:
                candidate.unlink()


def anchor(payload_bytes, destination_dir, trust_config, not_after=None, *, guard=None,
           fixed_query=None, on_query=None):
    """Resume a fixed query or reuse its receipt; only the digest is transmitted.

    The persistent operation owner must serialize calls for this destination.
    A lost remote response can require retransmission of the same TSQ; the TSA
    is not assumed to provide exactly-once signing. An existing valid receipt
    can be recovered after cutoff, but a new request cannot be sent after it.
    """
    imprint, trust = _payload(payload_bytes), _trust(trust_config)
    folder = _owned_path(destination_dir)
    if guard:
        guard()
    folder.mkdir(parents=True, exist_ok=True)
    _resume_files(folder, payload_bytes, trust, not_after)
    require({p.name for p in folder.iterdir()} <= {"request.tsq", "response.tsr"},
            "Unexpected timestamp operation files")
    if (folder / "response.tsr").exists():
        receipt = verify_receipt(payload_bytes, folder, trust_config, not_after)
        return {**receipt, "tsa_url": trust["url"], "receipt_dir": str(folder), "reused_receipt": True}
    if not_after is not None:
        require(_now() <= _moment(not_after), "Timestamp delivery cutoff has passed")
    query_path = folder / "request.tsq"
    query = _read(query_path) if query_path.exists() else fixed_query if fixed_query is not None else _run(trust["binary"], [
        "ts", "-query", "-digest", imprint, "-sha256", "-cert", "-tspolicy", trust["policy_oid"]], folder, trust["timeout"])
    requested = _query(query)
    require(requested["imprint"] == imprint and requested["policy_oid"] == trust["policy_oid"], "Query binding differs")
    if not query_path.exists():
        _atomic_evidence(query_path, query)
    if on_query:
        on_query(query)
    if guard:
        guard()
    response = _post(trust["url"], query, trust["timeout"])
    attempts = folder.with_name(folder.name + "-attempts")
    attempts.mkdir(exist_ok=True)
    _atomic_evidence(attempts / (uuid.uuid4().hex + ".tsr"), response)
    receipt = _verify(payload_bytes, query, response, trust, not_after)
    if guard:
        guard()
    _atomic_evidence(folder / "response.tsr", response)
    return {**receipt, "tsa_url": trust["url"], "receipt_dir": str(folder), "reused_receipt": False}
