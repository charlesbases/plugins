"""One independent phase's current registry bytes and private parsed original.

No source or financial qualification is cached. Each new phase reads actual
CAS bytes again; mutable consumers receive detached containers, and a scalar
fingerprint is reused only after complete content equality with the original.
"""
import copy
import math
import struct
from contextlib import contextmanager
from contextvars import ContextVar

from contracts import fingerprint, require, strict_json_loads

_ACTIVE = ContextVar("investment_registry_phase", default=None)


def _same_content(left, right):
    """JSON content equality, including bool/int/float and signed zero."""
    kind = type(right)
    if type(left) is not kind:
        return False
    if kind is dict:
        return (len(left) == len(right) and all(type(key) is str for key in left)
                and all(type(key) is str for key in right)
                and left.keys() == right.keys()
                and all(_same_content(left[key], value) for key, value in right.items()))
    if kind is list:
        return len(left) == len(right) and all(_same_content(a, b) for a, b in zip(left, right))
    if kind in (str, bool, int, type(None)):
        return left is right or left == right
    if kind is float:
        return (math.isfinite(left) and math.isfinite(right)
                and struct.pack(">d", left) == struct.pack(">d", right))
    return False


class RegistryPhase:
    def __init__(self, store, artifacts, inputs):
        self.store, self.artifacts = store, artifacts
        self.__entries = {}
        self._bind_inputs(inputs)

    def _bind_inputs(self, value):
        if type(value) is dict:
            for key, child in value.items():
                if key in {"run_id", "analysis_run_id"} and type(child) is str and child:
                    run = self.store.get("analysis_run", child)
                    if (type(run) is dict and run.get("free_news_only") is True
                            and run.get("registry_snapshot_ref") is not None):
                        self._bind(run)
                elif type(child) in (dict, list):
                    self._bind_inputs(child)
        elif type(value) is list:
            for child in value:
                if type(child) in (dict, list):
                    self._bind_inputs(child)

    def _bind(self, run):
        require(_same_content(run, self.store.get("analysis_run", run["run_id"])),
                "Registry phase changed its authoritative analysis run")
        reference, expected = run["registry_snapshot_ref"], run["source_registry_hash"]
        key = fingerprint(reference)
        if key not in self.__entries:
            self.__entries[key] = {"reference": copy.deepcopy(reference), "expected": expected,
                                   "original": None, "hash": None, "nested_checked": False}
        else:
            require(self.__entries[key]["expected"] == expected,
                    "One source registry has conflicting authoritative fingerprints")
        return self.__entries[key]

    def _load(self, entry):
        if entry["original"] is None:
            # Actual CAS read validates every chunk and complete size/SHA.
            raw = self.artifacts.read(entry["reference"])
            original = strict_json_loads(raw.decode("utf-8"))
            require(type(original) is dict, "Source registry must be an object")
            original_hash = fingerprint(original)
            require(original_hash == entry["expected"], "Frozen source registry fingerprint differs")
            entry["original"], entry["hash"] = original, original_hash
        return entry["original"]

    def read(self, run):
        return copy.deepcopy(self._load(self._bind(run)))

    def content_hash(self, value):
        for entry in self.__entries.values():
            original = entry["original"]
            # Container identity is not evidence. Equality checks the complete
            # values and list order; a changed view must get its changed hash.
            if original is not None and _same_content(value, original):
                return entry["hash"]
        return fingerprint(value)

    def check_reference(self, reference, walk):
        entry = self.__entries.get(fingerprint(reference))
        if entry is None or not _same_content(entry["reference"], reference):
            return False
        original = self._load(entry)
        if not entry["nested_checked"]:
            self._walk_references(original, walk)
            entry["nested_checked"] = True
        return True

    def _walk_references(self, value, walk):
        # The external walker receives only detached CAS references. It can
        # neither retain nor mutate this phase's private registry basis.
        if type(value) is dict:
            if {"sha256", "size"} <= value.keys() and ("path" in value or "chunks" in value):
                walk(copy.deepcopy(value))
                return
            for child in value.values():
                self._walk_references(child, walk)
        elif type(value) is list:
            for child in value:
                self._walk_references(child, walk)


@contextmanager
def registry_phase(store, artifacts, *, inputs=None, fresh=True):
    existing = _ACTIVE.get()
    if (not fresh and existing is not None and existing.store is store
            and existing.artifacts is artifacts):
        yield existing
        return
    phase = RegistryPhase(store, artifacts, inputs)
    token = _ACTIVE.set(phase)
    try:
        yield phase
    finally:
        _ACTIVE.reset(token)


def read_registry(run, store, artifacts):
    phase = _ACTIVE.get()
    if phase is None or phase.store is not store or phase.artifacts is not artifacts:
        return artifacts.read_json(run["registry_snapshot_ref"])
    return phase.read(run)


def registry_fingerprint(value):
    phase = _ACTIVE.get()
    return phase.content_hash(value) if phase is not None else fingerprint(value)


def checked_registry_reference(reference, artifacts, walk):
    phase = _ACTIVE.get()
    return bool(phase is not None and phase.artifacts is artifacts
                and phase.check_reference(reference, walk))
