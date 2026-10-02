"""Transport provenance tests use synthetic HTTP responses, never live sites."""
import copy
import hashlib
import io
import sys
import ssl
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/investment/scripts"))
import source_fetch

URL = "https://official.example/docs/item.html"
REGISTRY = {"schema_version": 1, "sources": {"official": {
    "allowed_hosts": ["official.example"], "redirect_hosts": ["cdn.official.example"],
    "path_pattern": "^/docs/", "media_types": ["text/html"], "encoding": "utf-8"}}}


class Response:
    def __init__(self, url=URL, status=200, data=b"hello", location=None, charset="utf-8"):
        self.url, self.status, self.body = url, status, io.BytesIO(data)
        self.headers = Message()
        self.headers["Content-Type"] = "text/html" + ("; charset="+charset if charset else "")
        if location is not None:
            self.headers["Location"] = location
    def geturl(self): return self.url
    def read(self, size): return self.body.read(size)
    read1 = read
    def __enter__(self): return self
    def __exit__(self, *args): pass


class SourceFetchTests(unittest.TestCase):
    def test_announcement_header_is_used_and_bound_to_original_capture(self):
        url = "https://api.fund.eastmoney.com/f10/JJGG?fundcode=000001&pageIndex=1&pageSize=20&type=0"
        response = Response(url=url, data=b'{"ErrCode":0,"Data":[]}')
        response.headers.replace_header("Content-Type", "application/json; charset=utf-8")
        with patch.object(source_fetch, "_open", return_value=response) as opened:
            result = source_fetch.fetch(url, "eastmoney_announcements_api")
        self.assertEqual(opened.call_args.kwargs["headers"], {"Referer": "https://fundf10.eastmoney.com/"})
        self.assertEqual(result["request_headers"], opened.call_args.kwargs["headers"])
        self.assertTrue(source_fetch.validate_capture_provenance(result))
        result["request_headers"] = {"Referer": "https://example.com/"}
        with self.assertRaisesRegex(ValueError, "request header identity"):
            source_fetch.validate_capture_provenance(result)

    def fetch(self, replies, **kw):
        with patch.object(source_fetch, "_open", side_effect=replies):
            return source_fetch.fetch(URL, "official", registry=REGISTRY, **kw)

    def test_pinned_redirect_chain_and_lossless_utf8(self):
        final = "https://cdn.official.example/docs/item.html"
        value = self.fetch([Response(status=302, location=final), Response(final, data="正式披露正文".encode())])
        self.assertEqual(value["text"], "正式披露正文")
        self.assertEqual(value["final_url"], final)
        self.assertEqual(value["redirect_chain"][0]["next_url"], final)
        self.assertEqual(value["raw_sha256"], hashlib.sha256(value["raw_bytes"]).hexdigest())
        self.assertTrue(source_fetch.validate_capture_provenance(value, REGISTRY))

    def test_downgrade_unknown_host_and_silent_redirect_are_rejected(self):
        for target in ("http://official.example/docs/item.html", "https://evil.example/docs/item.html"):
            with self.subTest(target=target), self.assertRaises(ValueError):
                self.fetch([Response(status=302, location=target)])
        with self.assertRaisesRegex(ValueError, "unrecorded redirect"):
            self.fetch([Response("https://cdn.official.example/docs/item.html")])

    def test_redirect_loop_and_forged_final_url_fail(self):
        with self.assertRaisesRegex(ValueError, "cycle"):
            self.fetch([Response(status=302, location=URL)])
        value = self.fetch([Response()])
        value["final_url"] = "https://evil.example/docs/item.html"
        with self.assertRaisesRegex(ValueError, "Final source"):
            source_fetch.validate_capture_provenance(value, REGISTRY)

    def test_explicit_html_encoding_is_used_without_replacement(self):
        body = '<meta charset="gbk"><p>中文来源</p>'.encode("gbk")
        value = self.fetch([Response(data=body, charset=None)])
        self.assertIn("中文来源", value["text"])
        with self.assertRaisesRegex(ValueError, "losslessly"):
            self.fetch([Response(data=b"\xff\xfe", charset="utf-8")])

    def test_source_size_and_response_type_boundaries(self):
        with self.assertRaisesRegex(ValueError, "oversized"):
            self.fetch([Response(data=b"12345")], max_bytes=4)
        with self.assertRaisesRegex(ValueError, "Empty"):
            self.fetch([Response(data=b"")])
        response = Response()
        response.headers.replace_header("Content-Type", "application/json")
        with self.assertRaisesRegex(ValueError, "content type"):
            self.fetch([response])

    def test_native_tls_failure_uses_hostname_and_certificate_validating_fallback(self):
        from unittest.mock import Mock
        response = Response()
        opener = Mock()
        opener.open.return_value = response
        with patch.object(source_fetch.os, "name", "nt"), patch.object(source_fetch, "_curl_open", side_effect=source_fetch.SourceError("missing close notify")), \
                patch.object(source_fetch.urllib.request, "build_opener", return_value=opener) as build:
            result = source_fetch._open(URL, 5)
        context = build.call_args.args[1]._context
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertEqual(result.transport, "HTTPS_default_certificate_validation")
        self.assertEqual(result.transport_details["fallback_from"]["backend"], "Schannel")
        self.assertLessEqual(opener.open.call_args.kwargs["timeout"], 5)
        self.assertIsNone(build.call_args.args[0].redirect_request(None, None, 302, "", {}, "https://evil.example/"))

    def test_tls_fallback_cannot_extend_the_original_elapsed_budget(self):
        with patch.object(source_fetch.os, "name", "nt"), patch.object(source_fetch, "_curl_open", side_effect=source_fetch.SourceError("TLS failed")), \
                patch.object(source_fetch.time, "monotonic", side_effect=[0, 6]), \
                patch.object(source_fetch.urllib.request, "build_opener") as opener:
            with self.assertRaisesRegex(ValueError, "budget exhausted"):
                source_fetch._open(URL, 5)
            opener.assert_not_called()


if __name__ == "__main__": unittest.main()
