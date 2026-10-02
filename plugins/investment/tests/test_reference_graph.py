"""Immutable reference DAG checks, with actual bytes and fresh call boundaries."""
import copy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/investment/scripts"))
from artifacts import Artifacts
from contracts import EvidenceError
import verify


class ReferenceGraphTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.artifacts = Artifacts(Path(temporary.name))
        self.leaf = self.artifacts.put_bytes(b"original source bytes")
        self.parent = self.artifacts.put_json({"left": self.leaf, "right": self.leaf})

    def test_shared_graph_reads_each_complete_reference_once(self):
        with patch.object(self.artifacts, "read", wraps=self.artifacts.read) as read:
            verify.references([self.parent, self.parent, self.leaf], self.artifacts)
        self.assertEqual(read.call_count, 2)

    def test_same_hash_cannot_hide_invalid_size_or_path(self):
        wrong_size = {**self.leaf, "size": self.leaf["size"] + 1}
        with self.assertRaises(EvidenceError):
            verify.references([self.leaf, wrong_size], self.artifacts)
        wrong_path = {**self.leaf, "path": "../outside"}
        with self.assertRaises(EvidenceError):
            verify.references([self.leaf, wrong_path], self.artifacts)

    def test_later_call_detects_changed_physical_bytes(self):
        verify.references([self.parent, self.parent], self.artifacts)
        self.artifacts._path(self.leaf["path"]).write_bytes(b"corrupted bytes")
        with self.assertRaises(EvidenceError):
            verify.references([self.parent, self.parent], self.artifacts)

    def test_binary_visit_does_not_hide_json_child_verification(self):
        binary_parent = copy.deepcopy(self.parent)
        binary_parent.pop("media_type")
        self.artifacts._path(self.leaf["path"]).write_bytes(b"corrupted bytes")
        with self.assertRaises(EvidenceError):
            verify.references([binary_parent, self.parent], self.artifacts)


if __name__ == "__main__":
    unittest.main()
