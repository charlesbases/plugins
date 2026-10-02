"""Runtime and bounded research IO for the sole Elastic Net/CVaR pipeline."""

import hashlib
import importlib.metadata
import json
import subprocess
import sys
import uuid
from pathlib import Path

import research
from research_data import read_table
from file_io import read_object, strict_json_loads

PIPELINE_ID = "elastic-net-cvar-allocation"
SCRIPTS = Path(__file__).resolve().parent


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ensure_runtime(root):
    require(sys.version_info >= (3, 12), "Pinned scientific dependencies require Python 3.12 or later")
    requirements = SCRIPTS / "requirements-model.txt"
    expected = dict(line.split("==") for line in requirements.read_text().splitlines() if line.strip())
    folder = research.owned_path(root, "runtime/model-" + sha(requirements)[:16])
    if folder.is_dir():
        sys.path.insert(0, str(folder))

    def versions():
        try:
            return {name: importlib.metadata.version(name) for name in expected}
        except importlib.metadata.PackageNotFoundError:
            return {}

    current = versions()
    if current != expected:
        require(not folder.exists(), "Pinned model runtime is incomplete; inspect it before retrying")
        folder.parent.mkdir(parents=True, exist_ok=True)
        stage = folder.with_name(folder.name + "-install-" + uuid.uuid4().hex[:8])
        stage.mkdir()
        with (stage / "install.log").open("wb") as log:
            result = subprocess.run([sys.executable, "-B", "-m", "pip", "install", "--disable-pip-version-check",
                "--only-binary=:all:", "--no-compile", "--target", str(stage), "-r", str(requirements)],
                stdout=log, stderr=subprocess.STDOUT, timeout=180)
        require(result.returncode == 0, "Dependency installation failed; see " + str(stage / "install.log"))
        stage.rename(folder)
        sys.path.insert(0, str(folder))
        current = versions()
    require(current == expected, "Dependency versions do not match the source lock")
    return current


def rows_write(folder, records, field="decision_date"):
    """Partition research output by month, preserving producer order."""
    buckets = {}
    for position, source in enumerate(records):
        require("_record_order" not in source, "Reserved storage metadata field")
        row = {**source, "_record_order": position}
        month = str(row.get(field, "undated"))[:7]
        require(month == "undated" or len(month) == 7 and month[4] == "-", "Bad record month")
        buckets.setdefault(month, []).append(row)
    folder.mkdir(parents=True, exist_ok=False)
    for month, values in sorted(buckets.items()):
        part, size, stream = 1, 0, None
        try:
            for value in values:
                line = (json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
                require(len(line) <= 16 * 1024 * 1024, "Single research record exceeds shard size")
                if stream is None or size + len(line) > 16 * 1024 * 1024:
                    if stream:
                        stream.close()
                        part += 1
                    stream = (folder / f"{month}-{part:06d}.jsonl").open("xb")
                    size = 0
                stream.write(line)
                size += len(line)
        finally:
            if stream:
                stream.close()


def rows_read(folder):
    rows = [strict_json_loads(line) for path in sorted(folder.glob("*.jsonl"))
            for line in path.read_text(encoding="utf-8").splitlines() if line]
    require(sorted(r.get("_record_order", -1) for r in rows) == list(range(len(rows))),
            "Record order inventory changed")
    rows.sort(key=lambda r: r["_record_order"])
    for row in rows:
        row.pop("_record_order")
    return rows


def load_data(root, base, requested):
    data_run = research.resolve_run(base, requested)
    audit = research.verify_selected(root, base, data_run)
    manifest = read_object(data_run / "manifest.json")
    policy = read_object(data_run / "policy.json")
    nav = {code: read_table(data_run, "normalized", code) for code in manifest["codes"]}
    features = {code: read_table(data_run, "features", code) for code in manifest["codes"]}
    return data_run, audit, manifest, policy, nav, features
