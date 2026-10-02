"""Verified, content-addressed artifacts with bounded individual files."""
import datetime as dt
import hashlib
import os
import uuid
from pathlib import Path

from contracts import EvidenceError, canonical_bytes, strict_json_loads
from file_io import durable_replace

CHUNK_BYTES = 16 * 1024 * 1024


class Artifacts:
    def __init__(self, base):
        self.base = Path(base).resolve()

    def _path(self, relative):
        path = (self.base / relative).resolve()
        if not path.is_relative_to(self.base / "objects") or path.is_symlink():
            raise EvidenceError("Artifact escapes the object store")
        return path

    def put_bytes(self, data):
        if type(data) is not bytes:
            raise TypeError("put_bytes requires bytes")
        digest = hashlib.sha256(data).hexdigest()
        if len(data) > CHUNK_BYTES:
            return {"sha256": digest, "size": len(data), "chunks": [
                self.put_bytes(data[i:i + CHUNK_BYTES]) for i in range(0, len(data), CHUNK_BYTES)]}
        relative = f"objects/{dt.datetime.now(dt.timezone.utc):%Y/%m}/{digest[:2]}/{digest}"
        path = self._path(relative)
        reference = {"sha256": digest, "size": len(data), "path": relative}
        if path.exists():
            self.read(reference)
            return reference
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(digest + "." + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            durable_replace(temporary, path)
            self.read(reference)
        finally:
            temporary.unlink(missing_ok=True)
        return reference

    def put_json(self, value):
        return {**self.put_bytes(canonical_bytes(value)), "media_type": "application/json"}

    def read(self, reference):
        if "chunks" in reference:
            data = b"".join(self.read(chunk) for chunk in reference["chunks"])
        else:
            path = self._path(reference["path"])
            size = reference["size"]
            if type(size) is not int or size < 0:
                raise EvidenceError("Invalid unpartitioned artifact size")
            if size > CHUNK_BYTES:
                raise EvidenceError("Oversized unpartitioned artifact")
            with path.open("rb") as stream:
                data = stream.read(size + 1)
        if len(data) != reference["size"] or hashlib.sha256(data).hexdigest() != reference["sha256"]:
            raise EvidenceError("Artifact content or size changed")
        return data

    def read_json(self, reference):
        return strict_json_loads(self.read(reference).decode("utf-8"))
