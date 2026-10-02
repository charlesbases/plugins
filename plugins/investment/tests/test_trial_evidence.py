"""Ephemeral local-CA tests prove implementation, not real preregistration."""

import datetime as dt
import hashlib
import importlib.util
import json
import os
import shutil
import ssl
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "skills/investment/scripts/trial_evidence.py"
SPEC = importlib.util.spec_from_file_location("investment_trial_evidence", SCRIPT)
evidence = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(evidence)
POLICY = "1.2.3.4.1"
VALIDATION_ROOT = Path(os.environ.get("INVESTMENT_EVIDENCE_TEST_ROOT",
    str(Path(tempfile.gettempdir()) / "investment-evidence-tests")))


def der(tag, content):
    size = len(content)
    length = bytes([size]) if size < 128 else bytes([128 + (size.bit_length() + 7) // 8]) + size.to_bytes((size.bit_length() + 7) // 8, "big")
    return bytes([tag]) + length + content


class TrialEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.openssl = shutil.which("openssl")
        if cls.openssl is None:
            raise RuntimeError("Real OpenSSL is required for timestamp evidence tests")
        VALIDATION_ROOT.mkdir(parents=True, exist_ok=True)
        cls.temporary = tempfile.TemporaryDirectory(prefix="local-tsa-", dir=VALIDATION_ROOT)
        cls.root = Path(cls.temporary.name).resolve()
        cls.temp_patch = patch.object(tempfile, "tempdir", str(cls.root))
        cls.temp_patch.start()
        cls.addClassCleanup(cls.temp_patch.stop)
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.command("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-sha256", "-days", "2",
                    "-keyout", "ca.key", "-out", "ca.pem", "-subj", "/CN=Local Test CA Only",
                    "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign",
                    "-addext", "subjectKeyIdentifier=hash")
        cls.command("req", "-new", "-newkey", "rsa:2048", "-nodes", "-keyout", "tsa.key",
                    "-out", "tsa.csr", "-subj", "/CN=Local Test TSA Only")
        (cls.root / "tsa.ext").write_text("basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\n"
            "extendedKeyUsage=critical,timeStamping\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid,issuer\n", encoding="ascii")
        cls.command("x509", "-req", "-in", "tsa.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial",
                    "-out", "tsa.pem", "-days", "2", "-sha256", "-extfile", "tsa.ext")
        cls.command("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-sha256", "-days", "2",
                    "-keyout", "foreign.key", "-out", "foreign.pem", "-subj", "/CN=Foreign Test CA Only",
                    "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign",
                    "-addext", "subjectKeyIdentifier=hash")
        (cls.root / "tsa.conf").write_text("[tsa]\ndefault_tsa=server\n[server]\nserial=tsa.serial\n"
            "signer_cert=tsa.pem\nsigner_key=tsa.key\ncerts=ca.pem\nsigner_digest=sha256\n"
            "default_policy=" + POLICY + "\nother_policies=1.2.3.4.2\ndigests=sha256\n"
            "accuracy=secs:1\nordering=yes\ntsa_name=yes\ness_cert_id_alg=sha256\n", encoding="ascii")
        cls.payload = evidence.canonical_bytes({"study": "synthetic local evidence test", "account": "never transmitted"})
        cls.trust = {"tsa_url": "https://timestamp.invalid/tsr", "ca_file": str(cls.root / "ca.pem"),
                     "ca_sha256": hashlib.sha256((cls.root / "ca.pem").read_bytes()).hexdigest(),
                     "policy_oid": POLICY, "max_clock_skew_seconds": 0, "openssl_binary": cls.openssl}
        cls.command("ts", "-query", "-digest", hashlib.sha256(cls.payload).hexdigest(), "-sha256",
                    "-cert", "-tspolicy", POLICY, "-out", "query.tsq")
        cls.command("ts", "-reply", "-config", "tsa.conf", "-queryfile", "query.tsq", "-out", "response.tsr")
        cls.query = (cls.root / "query.tsq").read_bytes()
        cls.response = (cls.root / "response.tsr").read_bytes()

    @classmethod
    def command(cls, *arguments):
        result = subprocess.run([cls.openssl, *arguments], cwd=cls.root,
            env=dict(os.environ, OPENSSL_CONF=os.devnull), capture_output=True, timeout=30)
        if result.returncode:
            raise AssertionError(result.stderr.decode("utf-8", errors="replace"))
        return result.stdout

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="receipt-", dir=self.root)
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.receipt = self.folder / "timestamp"
        self.receipt.mkdir()
        (self.receipt / "request.tsq").write_bytes(self.query)
        (self.receipt / "response.tsr").write_bytes(self.response)

    def verify(self, trust=None, not_after=None):
        return evidence.verify_receipt(self.payload, self.receipt, trust or self.trust, not_after)

    def signed_at(self, moment, remove_accuracy=False):
        fields = evidence._sequence(evidence._signed_info(self.response)["content_der"])
        encoded = [item[2] for item in fields]
        encoded[4] = der(24, moment.strftime("%Y%m%d%H%M%SZ").encode("ascii"))
        if remove_accuracy:
            del encoded[5]
        info = self.folder / "info.der"
        info.write_bytes(der(48, b"".join(encoded)))
        token = self.folder / "signed.der"
        self.command("cms", "-sign", "-binary", "-nodetach", "-cades", "-nosmimecap", "-md", "sha256",
            "-in", str(info), "-outform", "DER", "-out", str(token), "-signer", "tsa.pem",
            "-inkey", "tsa.key", "-certfile", "ca.pem", "-econtent_type", evidence.TST_INFO_OID)
        self.command("ts", "-reply", "-token_in", "-in", str(token), "-out", str(self.receipt / "response.tsr"))

    def test_real_signed_receipt_binds_bytes_nonce_policy_and_signer(self):
        result = self.verify()
        self.assertEqual(result["verification"], "passed")
        self.assertEqual(result["imprint"], hashlib.sha256(self.payload).hexdigest())
        self.assertEqual(result["policy_oid"], POLICY)
        self.assertEqual(result["nonce"], str(evidence._query(self.query)["nonce"]))
        certificate = ssl.PEM_cert_to_DER_cert((self.root / "tsa.pem").read_text())
        self.assertEqual(result["signer_sha256"], hashlib.sha256(certificate).hexdigest())
        self.assertFalse(result["revocation_checked"])
        self.assertEqual(dt.datetime.fromisoformat(result["gen_time"]).utcoffset(), dt.timedelta(0))
        pinned = {**self.trust, "signer_sha256": result["signer_sha256"]}
        self.assertEqual(self.verify(pinned), result)

    def test_payload_nonce_policy_and_signer_mismatches_fail(self):
        with self.assertRaisesRegex(ValueError, "imprint"):
            evidence.verify_receipt(self.payload + b"changed", self.receipt, self.trust)
        for change, message in (({"policy_oid": "1.2.3.4.2"}, "policy"),
                                ({"signer_sha256": "0" * 64}, "signer")):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, message):
                self.verify({**self.trust, **change})
        self.command("ts", "-query", "-digest", hashlib.sha256(self.payload).hexdigest(), "-sha256",
                     "-cert", "-tspolicy", POLICY, "-out", str(self.receipt / "request.tsq"))
        with self.assertRaisesRegex(ValueError, "nonce"):
            self.verify()

    def test_wrong_ca_hash_and_untrusted_chain_fail(self):
        with self.assertRaisesRegex(ValueError, "CA bundle hash"):
            self.verify({**self.trust, "ca_sha256": "0" * 64})
        foreign = self.root / "foreign.pem"
        with self.assertRaisesRegex(ValueError, "OpenSSL"):
            self.verify({**self.trust, "ca_file": str(foreign),
                         "ca_sha256": hashlib.sha256(foreign.read_bytes()).hexdigest()})

    def test_modified_signature_and_unsigned_time_fail(self):
        changed = bytearray(self.response)
        changed[-1] ^= 1
        (self.receipt / "response.tsr").write_bytes(changed)
        with self.assertRaisesRegex(ValueError, "OpenSSL"):
            self.verify()
        actual = evidence._signed_info(self.response)["gen_time"]
        before = actual.strftime("%Y%m%d%H%M%SZ").encode()
        after = (actual - dt.timedelta(days=1)).strftime("%Y%m%d%H%M%SZ").encode()
        (self.receipt / "response.tsr").write_bytes(self.response.replace(before, after, 1))
        with self.assertRaisesRegex(ValueError, "OpenSSL"):
            self.verify()

    def test_signed_future_time_and_late_cutoff_are_rejected(self):
        result = self.verify()
        moment = dt.datetime.fromisoformat(result["gen_time"])
        with self.assertRaisesRegex(ValueError, "later than cutoff"):
            self.verify(not_after=(moment - dt.timedelta(seconds=1)).isoformat())
        with self.assertRaisesRegex(ValueError, "later than cutoff"):
            self.verify(not_after=(moment + dt.timedelta(milliseconds=500)).isoformat())
        self.signed_at(moment + dt.timedelta(hours=1))
        with self.assertRaisesRegex(ValueError, "future"):
            self.verify()

    def test_certificate_must_be_valid_at_signed_time(self):
        moment = dt.datetime.fromisoformat(self.verify()["gen_time"])
        self.signed_at(moment - dt.timedelta(days=3))
        with self.assertRaisesRegex(ValueError, "OpenSSL"):
            self.verify()

    def test_missing_accuracy_requires_pinned_policy_bound(self):
        moment = dt.datetime.fromisoformat(self.verify()["gen_time"])
        self.signed_at(moment, remove_accuracy=True)
        with self.assertRaisesRegex(ValueError, "accuracy absent"):
            self.verify()
        result = self.verify({**self.trust, "policy_accuracy_seconds": 2})
        self.assertEqual(result["accuracy_seconds"], 2)
        self.assertEqual(dt.datetime.fromisoformat(result["upper_time_bound"]), moment + dt.timedelta(seconds=2))

    def test_anchor_posts_only_digest_query_and_keeps_only_der_files(self):
        observed = []
        def post(url, query, timeout):
            observed.append((url, query, timeout))
            query_path, response_path = self.folder / "request.tsq", self.folder / "reply.tsr"
            query_path.write_bytes(query)
            self.command("ts", "-reply", "-config", "tsa.conf", "-queryfile", str(query_path),
                         "-out", str(response_path))
            return response_path.read_bytes()
        destination = self.folder / "anchored"
        transport = {**self.trust, "tsa_url": "http://timestamp.invalid/tsr"}
        with patch.object(evidence, "_post", side_effect=post):
            result = evidence.anchor(self.payload, destination, transport)
        self.assertEqual(result["verification"], "passed")
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0][0], transport["tsa_url"])
        self.assertEqual(evidence._query(observed[0][1])["imprint"], hashlib.sha256(self.payload).hexdigest())
        self.assertNotIn(b"never transmitted", observed[0][1])
        self.assertEqual({p.name for p in destination.iterdir()}, {"request.tsq", "response.tsr"})
        with patch.object(evidence, "_post", side_effect=AssertionError("Must reuse receipt")):
            repeated = evidence.anchor(self.payload, destination, transport)
        self.assertTrue(repeated["reused_receipt"])
        self.assertEqual(repeated["response_sha256"], result["response_sha256"])
        with self.assertRaisesRegex(ValueError, "imprint"):
            evidence.anchor(self.payload + b"changed", destination, transport)

    def test_response_before_rename_recovers_without_network(self):
        source = self.receipt / "response.tsr"
        pending = self.receipt / (".response.tsr." + "a" * 32 + ".tmp")
        source.rename(pending)
        with patch.object(evidence, "_post", side_effect=AssertionError("No second TSA request")):
            result = evidence.anchor(self.payload, self.receipt, self.trust)
        self.assertTrue(result["reused_receipt"])
        self.assertFalse(pending.exists())
        self.assertEqual(source.read_bytes(), self.response)

    def test_unknown_delivery_retries_the_exact_query(self):
        (self.receipt / "response.tsr").unlink()
        sent = []
        def deliver(url, query, timeout):
            sent.append(query)
            if len(sent) == 1:
                raise evidence.EvidenceTransportError("delivery_unknown")
            return self.response
        with patch.object(evidence, "_post", side_effect=deliver):
            with self.assertRaises(OSError):
                evidence.anchor(self.payload, self.receipt, self.trust)
            result = evidence.anchor(self.payload, self.receipt, self.trust)
        self.assertEqual(sent, [self.query, self.query])
        self.assertEqual(result["verification"], "passed")

    def test_invalid_response_is_preserved_and_fixed_query_can_retry(self):
        (self.receipt / "response.tsr").unlink()
        sent = []
        def deliver(url, query, timeout):
            sent.append(query)
            return b"invalid timestamp bytes" if len(sent) == 1 else self.response
        with patch.object(evidence, "_post", side_effect=deliver):
            with self.assertRaises(evidence.EvidenceError):
                evidence.anchor(self.payload, self.receipt, self.trust)
            self.assertFalse((self.receipt / "response.tsr").exists())
            result = evidence.anchor(self.payload, self.receipt, self.trust)
        attempts = list(self.receipt.with_name(self.receipt.name + "-attempts").glob("*.tsr"))
        self.assertEqual(len(attempts), 2)
        self.assertIn(b"invalid timestamp bytes", [path.read_bytes() for path in attempts])
        self.assertEqual(sent, [self.query, self.query])
        self.assertEqual(result["verification"], "passed")

    def test_expired_owner_cannot_adopt_a_valid_response(self):
        (self.receipt / "response.tsr").unlink()
        calls = []
        def guard():
            calls.append(1)
            if len(calls) == 3:
                raise RuntimeError("lost execution ownership")
        with patch.object(evidence, "_post", return_value=self.response):
            with self.assertRaisesRegex(RuntimeError, "lost execution ownership"):
                evidence.anchor(self.payload, self.receipt, self.trust, guard=guard)
        self.assertFalse((self.receipt / "response.tsr").exists())

    def test_signed_receipt_recovers_after_deadline_but_new_delivery_does_not(self):
        value = self.verify()
        cutoff = dt.datetime.fromisoformat(value["upper_time_bound"])
        with patch.object(evidence, "_now", return_value=cutoff + dt.timedelta(seconds=10)), \
             patch.object(evidence, "_post", side_effect=AssertionError("No new request after cutoff")):
            result = evidence.anchor(self.payload, self.receipt, self.trust, not_after=cutoff)
            self.assertTrue(result["reused_receipt"])
            (self.receipt / "response.tsr").unlink()
            with self.assertRaisesRegex(ValueError, "cutoff has passed"):
                evidence.anchor(self.payload, self.receipt, self.trust, not_after=cutoff)

    def test_empty_interrupted_query_write_is_recovered(self):
        destination = self.folder / "interrupted"
        destination.mkdir()
        (destination / (".request.tsq." + "b" * 32 + ".tmp")).write_bytes(b"")
        def sign(url, query, timeout):
            request = self.folder / "recovered.tsq"
            response = self.folder / "recovered.tsr"
            request.write_bytes(query)
            self.command("ts", "-reply", "-config", "tsa.conf", "-queryfile", str(request), "-out", str(response))
            return response.read_bytes()
        with patch.object(evidence, "_post", side_effect=sign):
            result = evidence.anchor(self.payload, destination, self.trust)
        self.assertEqual(result["verification"], "passed")
        self.assertEqual({p.name for p in destination.iterdir()}, {"request.tsq", "response.tsr"})

    def test_bad_endpoint_config_and_untrusted_json_metadata_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "HTTP"):
            self.verify({**self.trust, "tsa_url": "ftp://timestamp.invalid/tsr"})
        with self.assertRaisesRegex(ValueError, "nonnegative"):
            self.verify({**self.trust, "max_clock_skew_seconds": True})
        with self.assertRaisesRegex(ValueError, "ca_file required"):
            self.verify({**self.trust, "ca_file": None})
        (self.receipt / "receipt.json").write_text(json.dumps({"gen_time": "2000-01-01T00:00:00Z"}))
        with self.assertRaisesRegex(ValueError, "only request"):
            self.verify()

    def test_oversized_linked_and_traversing_files_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "traversal"):
            evidence.verify_receipt(self.payload, self.folder / ".." / self.folder.name / "timestamp", self.trust)
        target = self.folder / "outside.tsr"
        target.write_bytes(self.response)
        receipt = self.receipt / "response.tsr"
        receipt.unlink()
        os.link(target, receipt)
        with self.assertRaisesRegex(ValueError, "regular file"):
            self.verify()
        receipt.unlink()
        receipt.write_bytes(b"x" * (evidence.MAX_RECEIPT_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "size limit"):
            self.verify()

    def test_canonical_json_has_stable_string_keys_and_no_nonfinite_values(self):
        self.assertEqual(evidence.canonical_bytes({"b": 2, "a": 1}), b'{"a":1,"b":2}')
        self.assertEqual(evidence.digest({"a": 1}), hashlib.sha256(b'{"a":1}').hexdigest())
        for value in ({1: "ambiguous"}, {"a": float("nan")}, {"a": float("inf")}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                evidence.canonical_bytes(value)

    def test_malformed_der_and_extreme_integer_fields_fail_closed(self):
        for payload in (b"", b"\x30\x80", b"\x30\x81\x01\x00", b"\x30\x00unexpected"):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                evidence._signed_info(payload)
        with self.assertRaisesRegex(ValueError, "integer"):
            evidence._integer((2, b"\x01" * 22, b""))
        with self.assertRaisesRegex(ValueError, "OID"):
            evidence._oid((6, b"\x01" * 257, b""))


if __name__ == "__main__":
    unittest.main()
