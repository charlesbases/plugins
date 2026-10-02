"""Actual artifact-byte checks, scoped to one storage audit invocation."""
import collections
import copy
import hashlib
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"skills/investment/scripts"))
from artifacts import Artifacts
from contracts import EvidenceError, fingerprint
from state_store import Store
import verify


class CountingArtifacts(Artifacts):
    def __init__(self,base):
        super().__init__(base)
        self.reads=collections.Counter()
    def read(self,reference):
        self.reads[fingerprint(reference)]+=1
        return super().read(reference)


class ReferenceCheckerTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store=Store(Path(temp.name),"reference-audit")
        self.store.begin("audit",{"fixture":"original_reference_bytes"})
        self.enterContext(self.store.lease("audit"))
        self.artifacts=CountingArtifacts(self.store.base)

    def test_one_audit_reads_shared_DAG_once_and_next_audit_detects_changed_bytes(self):
        leaf=self.artifacts.put_json({"original":7})
        nested=self.artifacts.put_json({"leaf":leaf})
        self.store.put("fixture","one",{"nested":nested})
        self.store.put("fixture","two",{"nested":nested,"direct_leaf":leaf})
        self.artifacts.reads.clear()
        self.assertEqual(self.store.audit(verify.reference_checker(self.artifacts))["status"],"passed")
        self.assertEqual(self.artifacts.reads[fingerprint(nested)],1)
        self.assertEqual(self.artifacts.reads[fingerprint(leaf)],1)
        # Only this isolated adversarial fixture is altered. A fresh invocation
        # must physically recheck its bytes even though all references are equal.
        (self.artifacts.base/leaf["path"]).write_bytes(b'{"original":8}')
        with self.assertRaisesRegex(EvidenceError,"content or size changed"):
            self.store.audit(verify.reference_checker(self.artifacts))
        self.assertEqual(self.artifacts.reads[fingerprint(leaf)],2)

    def test_different_complete_size_or_media_metadata_is_not_memoized(self):
        leaf=self.artifacts.put_bytes(b"not-json")
        checker=verify.reference_checker(self.artifacts)
        checker(leaf)
        wrong_size={**leaf,"size":leaf["size"]+1}
        with self.assertRaisesRegex(EvidenceError,"content or size changed"):
            checker(wrong_size)
        self.assertGreater(self.artifacts.reads[fingerprint(wrong_size)],0)
        wrong_media={**leaf,"media_type":"application/json"}
        for _ in range(2):
            with self.assertRaises(ValueError):
                checker(wrong_media)
        # A failed parse must never become a verified entry in the private set.
        self.assertEqual(self.artifacts.reads[fingerprint(wrong_media)],2)

    def test_changed_chunk_order_with_same_parent_hash_is_rechecked(self):
        first=self.artifacts.put_bytes(b"alpha")
        second=self.artifacts.put_bytes(b"beta")
        compound={"sha256":hashlib.sha256(b"alphabeta").hexdigest(),"size":9,"chunks":[first,second]}
        checker=verify.reference_checker(self.artifacts)
        checker(compound)
        changed=copy.deepcopy(compound)
        changed["chunks"].reverse()
        with self.assertRaisesRegex(EvidenceError,"content or size changed"):
            checker(changed)
        self.assertGreater(self.artifacts.reads[fingerprint(changed)],0)


if __name__=="__main__":
    unittest.main()
