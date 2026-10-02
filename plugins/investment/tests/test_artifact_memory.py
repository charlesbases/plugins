"""Bounded artifact reads retain independent size and digest checks."""
from pathlib import Path
import sys
import tempfile
import tracemalloc
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/investment/scripts"
sys.path.insert(0, str(SCRIPTS))
from artifacts import Artifacts, CHUNK_BYTES
from contracts import EvidenceError


class ArtifactMemoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.objects = Artifacts(Path(temporary.name))

    def test_tiny_verified_read_does_not_allocate_a_full_chunk(self):
        raw = b"synthetic-ca-only"
        reference = self.objects.put_bytes(raw)
        tracemalloc.start()
        try:
            self.assertEqual(self.objects.read(reference), raw)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 1024 * 1024)

    def test_smaller_buffer_rejects_appended_truncated_and_substituted_bytes(self):
        raw = b"original evidence"
        reference = self.objects.put_bytes(raw)
        path = self.objects.base / reference["path"]
        for changed in (raw + b"extra", raw[:-1], b"X" + raw[1:]):
            with self.subTest(changed=changed):
                path.write_bytes(changed)
                with self.assertRaisesRegex(EvidenceError, "content or size changed"):
                    self.objects.read(reference)
        path.write_bytes(raw)
        self.assertEqual(self.objects.read(reference), raw)

    def test_invalid_declared_size_cannot_create_an_unbounded_read(self):
        reference = self.objects.put_bytes(b"original evidence")
        for size in (-1, -2, True, 1.0, "17", None, CHUNK_BYTES + 1):
            with self.subTest(size=size), self.assertRaises(EvidenceError):
                self.objects.read({**reference, "size": size})


if __name__ == "__main__":
    unittest.main()
