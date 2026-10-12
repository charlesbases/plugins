"""Internal bounded file operations. Business state belongs to state_store."""
import os
import re
import stat
import uuid
from pathlib import Path

from contracts import ContractError, canonical_bytes, strict_json_loads

MAX_SMALL_BYTES = 1024 * 1024
JSON_ARTIFACT_TAG = "@investment-json-artifact:1"


def default_root():
    configured = os.environ.get("INVESTMENT_DATA_ROOT")
    if configured is not None:
        # A task-specific override isolates research and accounts without
        # changing the host's HOME, Codex credentials or installed plugins.
        if os.name == "nt" and re.match(r"^/[A-Za-z]/", configured):
            configured = configured[1].upper() + ":/" + configured[3:]
        path = Path(configured)
        if not configured or not path.is_absolute():
            raise ContractError("INVESTMENT_DATA_ROOT must name an absolute data directory")
        return path
    value = os.environ.get("HOME") or os.environ.get("USERPROFILE") or str(Path.home())
    if os.name == "nt" and re.match(r"^/[A-Za-z]/", value):
        value = value[1].upper() + ":/" + value[3:]
    path = Path(value)
    if not path.is_absolute():
        raise ContractError("HOME must be absolute")
    return path / ".investment"


def plan_path(root, plan):
    if type(plan) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", plan):
        raise ContractError("Invalid plan identifier")
    root = Path(root).resolve()
    path = (root / "plans" / plan).resolve()
    if not path.is_relative_to(root):
        raise ContractError("Plan path escapes data root")
    return path


def path_present(path):
    return os.path.lexists(path)


def record_file_stat(path):
    result = Path(path).lstat()
    if not stat.S_ISREG(result.st_mode) or Path(path).is_symlink():
        raise ContractError("Expected a regular nonsymlink file")
    return result


def read_object(path):
    record_file_stat(path)
    with Path(path).open("rb") as stream:
        data = stream.read(MAX_SMALL_BYTES + 1)
    if len(data) > MAX_SMALL_BYTES:
        raise ContractError("JSON exceeds 1 MiB; use partitioned artifacts")
    value = strict_json_loads(data.decode("utf-8"))
    if type(value) is not dict:
        raise ContractError("Expected JSON object")
    return value


def sync_directory(path):
    # Windows has no portable directory fsync through os.open; SQLite performs
    # its own native durability calls. File data is flushed on both platforms.
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def durable_replace(source, target):
    """Publish a flushed file using the platform's durable rename interface."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        move = kernel.MoveFileExW
        move.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
        move.restype = wintypes.BOOL
        # REPLACE_EXISTING | WRITE_THROUGH; never copy across volumes.
        if not move(str(Path(source).resolve()), str(Path(target).resolve()), 0x1 | 0x8):
            raise ctypes.WinError(ctypes.get_last_error())
    else:
        os.replace(source, target)
        sync_directory(Path(target).parent)


def write_small_json(path, value):
    data = canonical_bytes(value)
    if len(data) > MAX_SMALL_BYTES:
        raise ContractError("JSON exceeds 1 MiB; use partitioned artifacts")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        durable_replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_artifact_json(path, value):
    """Keep metadata files small while preserving a complete large object."""
    data = canonical_bytes(value)
    if len(data) <= MAX_SMALL_BYTES:
        write_small_json(path, value)
        return
    # Import lazily: Artifacts uses the durable file primitives above.
    from artifacts import Artifacts
    reference = Artifacts(Path(path).parent).put_bytes(data)
    write_small_json(path, {JSON_ARTIFACT_TAG: reference})


def read_artifact_json(path):
    """Resolve only our explicit reference envelope, verifying every byte."""
    value = read_object(path)
    if JSON_ARTIFACT_TAG not in value:
        return value
    if set(value) != {JSON_ARTIFACT_TAG} or type(value[JSON_ARTIFACT_TAG]) is not dict:
        raise ContractError("Invalid JSON artifact reference envelope")
    from artifacts import Artifacts
    decoded = strict_json_loads(Artifacts(Path(path).parent).read(value[JSON_ARTIFACT_TAG]).decode("utf-8"))
    if type(decoded) is not dict:
        raise ContractError("Expected referenced JSON object")
    return decoded
