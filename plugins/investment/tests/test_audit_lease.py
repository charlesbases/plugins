"""Real SQLite/heartbeat regressions; no financial prediction qualification."""
import tempfile
import time
import unittest
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"skills/investment/scripts"))
from state_store import Store
from artifacts import Artifacts
import verify


class AuditLeaseTests(unittest.TestCase):
    def test_slow_original_artifact_validation_renews_real_owner_generation(self):
        with tempfile.TemporaryDirectory() as td:
            store=Store(Path(td),"audit-lease",timeout=.01,lease_seconds=.3,heartbeat_seconds=.025)
            store.begin("audit",{"fixture":"real_lease_and_SQLite"})
            with store.lease("audit"):
                artifacts=Artifacts(store.base)
                store.put("fixture","raw",{"ref":artifacts.put_json({"truth":17})})
                context=store.require_context()
                before=store.operation("audit")
                paused=[]
                def check(value):
                    verify.references(value,artifacts)
                    if not paused:
                        paused.append(True)
                        # Deliberately exceed the actual lease while the real
                        # background worker must still commit heartbeat renewals.
                        time.sleep(.55)
                result=store.audit(check)
                self.assertEqual(result["status"],"passed")
                self.assertEqual(store.assert_owned(context),context)
                after=store.operation("audit")
                self.assertEqual((after["owner"],after["generation"]),(before["owner"],before["generation"]))
                self.assertGreater(after["lease_until"],before["lease_until"])
                self.assertEqual((store.lease_seconds,store.heartbeat_seconds),(.3,.025))
                store.complete("audit",result)
            self.assertEqual(store.operation("audit")["status"],"completed")

    def test_snapshot_keeps_original_inventory_during_owned_callback_write(self):
        with tempfile.TemporaryDirectory() as td:
            store=Store(Path(td),"snapshot")
            store.begin("audit",{"fixture":"consistent_snapshot"})
            with store.lease("audit"):
                store.put("fixture","before",{"value":1})
                added=[]
                def check(value):
                    if not added:
                        added.append(True)
                        store.put("fixture","after",{"value":2})
                original=store.audit(check)
                self.assertEqual(original["records"],1)
                self.assertEqual(store.audit()["records"],2)
                self.assertEqual(store.get("fixture","before"),{"value":1})
                self.assertEqual(store.get("fixture","after"),{"value":2})
                store.assert_owned()


if __name__=="__main__":
    unittest.main()
