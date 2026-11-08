"""真实临时项目覆盖持久化、版本隔离、失效规则和本机写入边界。"""
import json
import pathlib
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import studio as app


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_root, self.old_probe = app.ROOT, app.probe
        app.ROOT = pathlib.Path(self.temp.name).resolve()
        app.probe = lambda p: {"duration": 9, "fps": 24}
        app._edl.clear()
        self.write(app.SEG, "| 段 | 节拍 | 秒 | 生成 | 过审 | 验收 |\n|---|---|---|---|---|---|\n| S1 | 镜1 | 3 | 候选1 | 待审 | 待审 |\n| S2 | 镜2 | 3 | | | |\n| S3 | 镜3 | 3 | | | |\n| S4 | 镜4 | 3 | | | |\n")
        for i in range(1, 5):
            self.write(f"素材库/镜{i}.mp4", f"source-{i}")
        self.write("final/a.mp4", "film-a")
        self.write("final/b.mp4", "film-b")
        self.clips = [dict(id=f"镜{i}", src=f"素材库/镜{i}.mp4", **{"in": 0, "out": 3}) for i in range(1, 5)]
        self.edl()

    def tearDown(self):
        app.ROOT, app.probe = self.old_root, self.old_probe
        app._edl.clear()
        self.temp.cleanup()

    def write(self, path, text):
        p = app.ROOT / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def edl(self):
        self.write(app.EDL, "V=" + repr(self.clips) + "\nA=[]\nSUBS=[]\n")
        app._edl.clear()

    def row(self, sid="S1"):
        return next(r for r in app.api_segments()["rows"] if r["id"] == sid)

    def mutate(self, action, **extra):
        return app.review_mutate({"segment": "S1", "action": action, **extra})

    def linked(self):
        self.mutate("associate", path="素材库/镜1.mp4")
        return self.row()["files"]["candidates"][0]

    def accept(self, final="final/a.mp4"):
        ctx = app.review_context("S1", final)
        self.mutate("bind", final=final, signature=ctx["signature"])
        self.mutate("accept", final=final, signature=ctx["signature"], status="approved")
        return ctx

    def test_pending_legacy_text_is_never_approval(self):
        self.assertEqual(self.row()["cells"]["过审"], "待审")
        self.assertIn("过审 0", app.api_segments()["status"])

    def test_association_selection_review_persist_independently(self):
        c = self.linked()
        self.mutate("select", path=c["path"], fingerprint=c["fingerprint"])
        self.assertEqual(self.row()["cells"]["过审"], "待审")
        self.mutate("review", path=c["path"], fingerprint=c["fingerprint"], status="approved")
        self.assertEqual(self.row()["cells"]["过审"], "通过")
        self.assertIn("过审 1", app.api_segments()["status"])
        data = json.loads((app.ROOT / app.REVIEW).read_text())
        self.assertEqual(data["segments"]["S1"]["selected"]["path"], c["path"])
        self.assertEqual(len(data["history"]), 3)
        self.assertTrue((app.ROOT / (app.REVIEW + ".bak")).exists())

    def test_rejection_requires_reason_and_remains_after_reload(self):
        c = self.linked()
        with self.assertRaises(ValueError):
            self.mutate("review", path=c["path"], fingerprint=c["fingerprint"], status="rejected")
        self.mutate("review", path=c["path"], fingerprint=c["fingerprint"], status="rejected", note="道具错误")
        c = self.row()["files"]["candidates"][0]
        self.assertEqual(c["review"], "退回")
        self.assertEqual(c["review_record"]["note"], "道具错误")

    def test_modified_candidate_invalidates_review_and_stale_submit(self):
        c = self.linked()
        self.mutate("select", path=c["path"], fingerprint=c["fingerprint"])
        self.mutate("review", path=c["path"], fingerprint=c["fingerprint"], status="approved")
        self.write(c["path"], "changed source")
        self.assertEqual(self.row()["cells"]["过审"], "需复核")
        with self.assertRaises(ValueError):
            self.mutate("review", path=c["path"], fingerprint=c["fingerprint"], status="approved")

    def test_missing_file_visible_and_can_be_unlinked(self):
        c = self.linked()
        (app.ROOT / c["path"]).unlink()
        self.assertTrue(self.row()["files"]["candidates"][0]["missing"])
        self.mutate("unlink", path=c["path"])
        self.assertEqual(self.row()["files"]["candidates"], [])

    def test_candidates_reject_traversal_hidden_and_external_symlink(self):
        for path in ("../outside.mp4", ".secrets/key.mp4", "final/a.mp4"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.mutate("associate", path=path)
        with tempfile.TemporaryDirectory() as other:
            external = pathlib.Path(other) / "external.mp4"
            external.write_text("private")
            (app.ROOT / "素材库/link.mp4").symlink_to(external)
            self.assertFalse(any(x["path"].endswith("link.mp4") for x in app.api_candidates()["items"]))
            with self.assertRaises(ValueError):
                self.mutate("associate", path="素材库/link.mp4")

    def test_acceptance_requires_binding_and_is_version_specific(self):
        ctx = app.review_context("S1", "final/a.mp4")
        with self.assertRaises(ValueError):
            self.mutate("accept", final=ctx["final"], signature=ctx["signature"], status="approved")
        self.accept()
        self.assertEqual(app.review_context("S1", "final/a.mp4")["status"], "通过")
        self.assertEqual(app.review_context("S1", "final/b.mp4")["status"], "待审")

    def test_neighbor_edit_invalidates_acceptance_preserves_history(self):
        self.accept()
        self.clips[1]["in"] = .25
        self.edl()
        ctx = app.review_context("S1", "final/a.mp4")
        self.assertEqual(ctx["status"], "需复核")
        self.assertFalse(ctx["confirmed"])
        self.assertEqual(len(app.review_read()["segments"]["S1"]["acceptances"]), 1)

    def test_unrelated_edit_does_not_invalidate_local_context(self):
        self.accept()
        self.clips[3]["grade"] = "warmer"
        self.edl()
        self.assertEqual(app.review_context("S1", "final/a.mp4")["status"], "通过")

    def test_overwritten_film_invalidates_and_rejects_stale_accept(self):
        old = self.accept()
        self.write("final/a.mp4", "new cut overwrote file")
        self.assertEqual(app.review_context("S1", "final/a.mp4")["status"], "需复核")
        with self.assertRaises(ValueError):
            self.mutate("accept", final=old["final"], signature=old["signature"], status="approved")

    def test_duplicate_timeline_mapping_is_not_guessed(self):
        self.clips[1]["id"] = "镜1"
        self.edl()
        ctx = app.review_context("S1", "final/a.mp4")
        self.assertIsNone(ctx["clip"])
        self.assertNotIn("signature", ctx)

    def test_corrupt_store_is_preserved(self):
        p = self.write(app.REVIEW, "{corrupt")
        with self.assertRaises(ValueError):
            self.mutate("associate", path="素材库/镜1.mp4")
        self.assertEqual(p.read_text(), "{corrupt")

    def test_http_origin_guard_and_validation(self):
        server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.H)
        old_port = app.PORT
        app.PORT = server.server_port
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        url = f"http://127.0.0.1:{app.PORT}"
        try:
            def request(origin, path="素材库/镜1.mp4"):
                headers = {"Content-Type": "application/json"}
                if origin:
                    headers["Origin"] = origin
                return urllib.request.urlopen(urllib.request.Request(url + "/api/review", data=json.dumps({"segment":"S1", "action":"associate", "path":path}).encode(), headers=headers), timeout=3)
            for origin in (None, "https://other.example"):
                with self.assertRaises(urllib.error.HTTPError) as error:
                    request(origin)
                self.assertEqual(error.exception.code, 403)
            with self.assertRaises(urllib.error.HTTPError) as error:
                request(url, "../outside.mp4")
            self.assertEqual(error.exception.code, 400)
            for endpoint in ('/api/edit', '/api/export', '/api/export/cancel', '/api/upscale', '/api/upscale/cancel', '/api/issues'):
                req = urllib.request.Request(url + endpoint, data=b'{}', headers={'Content-Type': 'application/json', 'Origin': 'https://other.example'})
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(req, timeout=3)
                self.assertEqual(error.exception.code, 403)
            payload = {'revision': 0, 'clips': [{'id': 'S1', 'src': '素材库/镜1.mp4', 'in': 0, 'out': 2, 'a': 1}]}
            req = urllib.request.Request(url + '/api/edit', data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json', 'Origin': url})
            with urllib.request.urlopen(req, timeout=3) as response:
                self.assertEqual(json.load(response)['revision'], 1)
            with urllib.request.urlopen(url + '/api/edit', timeout=3) as response:
                self.assertEqual(json.load(response)['clips'][0]['out'], 2)
            with request(url) as response:
                self.assertTrue(json.load(response)["ok"])
        finally:
            server.shutdown()
            server.server_close()
            worker.join()
            app.PORT = old_port


if __name__ == "__main__":
    unittest.main()
