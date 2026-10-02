"""Raw-bound HTML/PDF and extraction failure contracts."""
import copy
import hashlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/investment/scripts"))
import source_documents as docs
import source_fetch
from artifacts import Artifacts
from contracts import fingerprint
from state_store import Store
from test_news import capture, URL


class SourceDocumentTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name), "fixture")
        self.store.begin("document", {"fixture": True})
        self.enterContext(self.store.lease("document"))
        self.artifacts = Artifacts(self.store.base)

    def capture(self, text):
        value = capture()
        raw = text.encode("utf8")
        value.update(text=text, raw_bytes=raw, bytes=len(raw), raw_sha256=hashlib.sha256(raw).hexdigest(),
                     text_sha256=hashlib.sha256(raw).hexdigest())
        with patch.object(source_fetch, "fetch", return_value=value):
            return docs.capture_document(URL, "cn_state_council", self.artifacts, store=self.store)

    def test_html_preserves_header_product_context_and_cell_locator(self):
        ref = self.capture("<h1>Product A 123456</h1><table><tr><th>Amount</th><th>Rate</th></tr>"
                           "<tr><td>below 100</td><td>1.50%</td></tr></table>")
        row = docs.resolve_span(ref, "html/table/0/row/1", self.artifacts)
        self.assertEqual(row["headers"], ["Amount", "Rate"])
        self.assertEqual(row["cells"], ["below 100", "1.50%"])
        self.assertEqual(docs.resolve_span(ref, "html/table/0/row/1/cell/1", self.artifacts)["text"], "1.50%")
        self.assertTrue(row["context_blocks"])

    def test_resealed_extracted_text_cannot_replace_raw_source(self):
        ref = self.capture("<p>fee 1.50%</p>")
        document = self.artifacts.read_json(ref)
        document["blocks"][0]["text"] = "fee 0%"
        document["text_sha256"] = fingerprint(document["blocks"])
        document["document_id"] = fingerprint({k: v for k, v in document.items() if k not in ("document_id", "raw_ref", "registry_ref")})
        with self.assertRaisesRegex(ValueError, "extraction differs"):
            docs.read_extracted_document(self.artifacts.put_json(document), self.artifacts)

    def test_standalone_archive_retains_semantic_identity(self):
        ref = self.capture("<p>Original source paragraph</p>")
        destination = Artifacts(self.store.base / "separate-archive")
        archived = docs.archive_document(ref, self.artifacts, destination)
        before, after = docs.read_extracted_document(ref, self.artifacts), docs.read_extracted_document(archived, destination)
        self.assertEqual(before["document_id"], after["document_id"])
        self.assertEqual(before["blocks"], after["blocks"])

    def test_registry_expansion_keeps_original_document_provenance(self):
        ref = self.capture("<p>Original source quotation</p>")
        before = docs.read_extracted_document(ref, self.artifacts)
        changed = copy.deepcopy(source_fetch.load_registry())
        changed["future_registry_expansion"] = {"synthetic_test_only": True}
        with patch.object(source_fetch, "load_registry", return_value=changed):
            after = docs.read_extracted_document(ref, self.artifacts)
            destination = Artifacts(self.store.base / "expanded-registry-archive")
            archived = docs.archive_document(ref, self.artifacts, destination)
            self.assertEqual(docs.read_extracted_document(archived, destination)["capture"], before["capture"])
        self.assertEqual(after, before)
        self.assertEqual(after["retrieved_at"], before["retrieved_at"])

    def test_registry_snapshot_substitution_is_rejected(self):
        ref = self.capture("<p>Original source quotation</p>")
        document = copy.deepcopy(self.artifacts.read_json(ref))
        registry = copy.deepcopy(self.artifacts.read_json(document["registry_ref"]))
        registry["tampered"] = True
        document["registry_ref"] = self.artifacts.put_json(registry)
        with self.assertRaisesRegex(ValueError, "registry snapshot changed"):
            docs.read_extracted_document(self.artifacts.put_json(document), self.artifacts)

    @staticmethod
    def pdf(text=None):
        from pypdf import PdfWriter
        from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
        writer = PdfWriter()
        page = writer.add_blank_page(width=200, height=100)
        if text:
            font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                                     NameObject("/BaseFont"): NameObject("/Helvetica")})
            page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"):
                DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
            stream = DecodedStreamObject()
            stream.set_data(("BT /F1 12 Tf 10 40 Td ("+text+") Tj ET").encode("ascii"))
            page[NameObject("/Contents")] = writer._add_object(stream)
        out = io.BytesIO()
        writer.write(out)
        return out.getvalue()

    def test_native_pdf_is_text_with_page_locator_not_ocr_claim(self):
        document = docs.extract_document(self.pdf("Product 123456 fee 1.50%"), "application/pdf")
        self.assertEqual(document["blocks"][0]["locator"], "pdf/page/1")
        self.assertIn("1.50%", document["blocks"][0]["text"])
        self.assertEqual(document["extraction_scope"], "machine_readable_text_only_no_OCR_accuracy_claim")

    def test_image_only_pdf_requires_source_review(self):
        with self.assertRaises(docs.DocumentNeedsReview) as caught:
            docs.extract_document(self.pdf(), "application/pdf")
        self.assertEqual(caught.exception.code, "needs_research")

    def test_malformed_pdf_fails_as_evidence_gap(self):
        with self.assertRaises(docs.DocumentNeedsReview):
            docs.extract_document(b"%PDF-1.7 invalid", "application/pdf")

    def test_real_tt_help_div_and_inline_span_pattern_keeps_answer_body(self):
        raw = ('<h2>申购基金，按哪一天的净值计算份额？</h2><div class="answer_content">'
               '工作日<span>15</span>：<span>00</span>前的交易按当日净值计算，'
               '<span>15</span>点之后按下一个工作日的净值计算。</div>').encode("utf8")
        document = docs.extract_document(raw, "text/html", "utf8")
        answer = next(row for row in document["blocks"] if row["kind"] == "container_text")
        self.assertIn("工作日15:00前的交易按当日净值计算", answer["text"])
        self.assertEqual(answer["container_path"][-1]["classes"], ["answer_content"])
        self.assertEqual(document["blocks"][0]["locator"], "html/block/0")
        self.assertEqual(document["extractor"]["id"], "source-document-html-4.1")

    def test_free_container_text_does_not_duplicate_paragraph_or_table_text(self):
        document = docs.extract_document(b"<div>before<p>paragraph</p>after<table><tr><td>cell</td></tr></table>end</div>",
                                         "text/html", "utf8")
        free = [row["text"] for row in document["blocks"] if row["kind"] == "container_text"]
        self.assertEqual(free, ["before", "after", "end"])


if __name__ == "__main__":
    unittest.main()
