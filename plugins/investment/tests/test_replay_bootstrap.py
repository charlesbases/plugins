"""Fresh-process historical validation loads the actual isolated IANA runtime."""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]/"skills/investment/scripts"


class HistoricalBootstrapTests(unittest.TestCase):
    def test_completed_source_validation_bootstraps_timezone_without_inherited_pythonpath(self):
        import tzdata
        sys.path.insert(0, str(SCRIPTS))
        import allocation_runtime
        runtime = Path(tzdata.__file__).resolve().parents[1]
        expected = "model-"+allocation_runtime.sha(SCRIPTS/"requirements-model.txt")[:16]
        if runtime.name != expected or runtime.parent.name != "runtime":
            self.skipTest("This regression requires the exposed isolated pinned runtime")
        script = r'''
import importlib.abc, json, pathlib, sys, types, zoneinfo
scripts, runtime, root = sys.argv[1:]
sys.path.insert(0, scripts)
zoneinfo.reset_tzpath(())
zoneinfo.ZoneInfo.clear_cache()
class IsolatedTimezone(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "tzdata" or fullname.startswith("tzdata."):
            if runtime not in sys.path:
                raise ModuleNotFoundError("IANA package unavailable until isolated runtime bootstrap")
sys.meta_path.insert(0, IsolatedTimezone())
try:
    zoneinfo.ZoneInfo("Asia/Shanghai")
    raise AssertionError("The regression did not start with an isolated timezone gap")
except zoneinfo.ZoneInfoNotFoundError:
    pass
import pipeline, stage_validation
seen = []
def validate(request, result, store, artifacts, bindings):
    seen.append(str(zoneinfo.ZoneInfo("Asia/Shanghai")))
    assert bindings == {"historical": True}
    return {"status": "passed", "scope": "test_historical_timezone_dependency", "checks": ["IANA_lookup"]}
stage_validation.validate_operation_result = validate
original = {"status": "partial", "snapshot": "unchanged-test-source-snapshot"}
request = {"schema_version": 4, "request_id": "same-completed-id", "operation": "industry_prepare", "payload": {}}
store = types.SimpleNamespace(root=pathlib.Path(root))
artifacts = types.SimpleNamespace(read_json=lambda reference: {})
actual = pipeline._completed_review(request, original, store, artifacts)
print(json.dumps({"same_result": actual == original, "validated_timezone": seen,
                  "runtime_loaded": runtime in sys.path}))
'''
        environment = dict(os.environ)
        environment.pop("PYTHONPATH", None)
        environment["PYTHONUTF8"] = "1"
        completed = subprocess.run([sys.executable, "-B", "-c", script, str(SCRIPTS),
                                    str(runtime), str(runtime.parent.parent)],
                                   env=environment, text=True, encoding="utf-8", capture_output=True, timeout=30)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        observed = json.loads(completed.stdout)
        self.assertTrue(observed["same_result"])
        self.assertEqual(observed["validated_timezone"], ["Asia/Shanghai"])
        self.assertTrue(observed["runtime_loaded"])


if __name__ == "__main__":
    unittest.main()
