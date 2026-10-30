"""用本機 HTTP 與暫存 Git 倉庫驗證部署確認，不連 GitHub 或正式圖片站。"""

import contextlib
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import verify_photo_deployment as deploy


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def serve(self, head=False):
        state = self.server.state
        path = urllib.parse.urlsplit(self.path).path.lstrip("/")
        if path == "manifest.json":
            if state["mode"] == "error":
                self.send_error(503)
                return
            state["manifest_requests"] += 1
            old = state["mode"] == "undeployed" or (state["mode"] == "rollback" and state["manifest_requests"] % 2 == 0)
            body = json.dumps(state["old" if old else "current"]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if not head:
                self.wfile.write(body)
            return

        state["asset_requests"].append(path)
        if state["mode"] == "partial" and path == state["missing"]:
            self.send_error(404)
            return
        if head and state["mode"] == "no_head":
            self.send_error(405)
            return
        body = state.get("remote_files", state["files"]).get(path)
        if body is None:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/webp")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def do_GET(self):
        self.serve()

    def do_HEAD(self):
        self.serve(head=True)


class DeploymentTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.origin = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="photo-deploy-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        old = {"version": 2, "photos": {"sample/a": {"version": "aaaaaaaaaa"}}, "reviews": {}}
        (self.root / "manifest.json").write_text(json.dumps(old))
        self.git("add", "manifest.json")
        self.git("commit", "-qm", "old")
        self.base = self.git("rev-parse", "HEAD").decode().strip()
        files = {"sample/a-600.bbbbbbbbbb.webp": b"webp fixture one",
                 "sample/b-600.cccccccccc.webp": b"webp fixture two"}
        current = {"version": 2, "photos": {}, "reviews": {}}
        for name, body in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
            key = "sample/a" if "/a-" in name else "sample/b"
            version = "bbbbbbbbbb" if key.endswith("/a") else "cccccccccc"
            current["photos"][key] = {"version": version, "sizes": {"600": {"file": name,
                "width": 600, "height": 400, "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()}}}
        (self.root / "manifest.json").write_text(json.dumps(current))
        self.git("add", ".")
        self.git("commit", "-qm", "new")
        self.output = self.root / "proof/deployment-proof.json"
        self.server.state = {"mode": "success", "old": old, "current": current, "files": files,
                             "remote_files": files, "missing": "sample/b-600.cccccccccc.webp",
                             "manifest_requests": 0, "asset_requests": []}
        # b 不在本次 keys，但它是建置期間新增的 WebP，仍須驗證。
        self.snapshot = deploy.prepare(self.root, self.base, ["sample/a"])

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.root, stderr=subprocess.DEVNULL)

    def wait(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return deploy.wait_and_write(self.snapshot, self.origin, self.output, timeout=0.3, interval=0.03)

    def test_undeployed_manifest_never_writes_proof(self):
        self.server.state["mode"] = "undeployed"
        with self.assertRaises(TimeoutError):
            self.wait()
        self.assertFalse(self.output.exists())

    def test_partial_deployment_checks_new_files_outside_target(self):
        self.server.state["mode"] = "partial"
        with self.assertRaises(TimeoutError):
            self.wait()
        self.assertIn(self.server.state["missing"], self.server.state["asset_requests"])
        self.assertFalse(self.output.exists())

    def test_manifest_rollback_during_check_never_writes_proof(self):
        self.server.state["mode"] = "rollback"
        with self.assertRaises(TimeoutError):
            self.wait()
        self.assertGreaterEqual(self.server.state["manifest_requests"], 2)
        self.assertFalse(self.output.exists())

    def test_http_error_never_writes_proof(self):
        self.server.state["mode"] = "error"
        with self.assertRaises(TimeoutError):
            self.wait()
        self.assertFalse(self.output.exists())

    def test_same_size_wrong_hash_never_writes_proof(self):
        remote = dict(self.server.state["files"])
        name = "sample/a-600.bbbbbbbbbb.webp"
        remote[name] = b"webp fixture bad"  # same size as the local body; HEAD alone must not pass
        self.server.state["remote_files"] = remote
        with self.assertRaises(TimeoutError):
            self.wait()
        self.assertFalse(self.output.exists())

    def test_success_writes_exact_committed_snapshot(self):
        proof = self.wait()
        self.assertEqual(json.loads(self.output.read_text()), proof)
        self.assertEqual(proof["schema"], 1)
        self.assertEqual(proof["origin"], self.origin)
        self.assertEqual(proof["sourceSha"], self.git("rev-parse", "HEAD").decode().strip())
        self.assertEqual(proof["manifestSha256"], hashlib.sha256(self.git("show", "HEAD:manifest.json")).hexdigest())
        self.assertEqual(proof["photos"], {"sample/a": "bbbbbbbbbb", "sample/b": "cccccccccc"})
        self.assertTrue(proof["checkedAt"].endswith("Z"))
        self.assertGreaterEqual(self.server.state["manifest_requests"], 2)

    def test_head_unsupported_falls_back_to_verified_get(self):
        self.server.state["mode"] = "no_head"
        self.assertEqual(self.wait()["schema"], 1)
        self.assertGreaterEqual(len(self.server.state["asset_requests"]), 4)

    def test_uncommitted_manifest_cannot_produce_snapshot(self):
        (self.root / "manifest.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "不一致"):
            deploy.prepare(self.root, self.base, ["sample/a"])
        self.assertFalse(self.output.exists())

    def test_cli_verifies_all_downloaded_keys(self):
        # BUILD_ALL (all or a folder input) leaves no explicit keys on the build
        # command; the verifier enumerates every downloaded original instead.
        with mock.patch.object(deploy.webimg, "ROOT", self.root), \
             mock.patch.object(deploy.webimg, "originals", return_value=[
                 ("sample/a", self.root / "sample-a.jpeg"),
                 ("sample/b", self.root / "sample-b.jpeg"),
             ]), \
             mock.patch.object(deploy, "ORIGIN", self.origin), \
             mock.patch.object(sys, "argv", [
                 "verify_photo_deployment.py", "--base", self.base, "--output", str(self.output)
             ]):
            deploy.main()
        proof = json.loads(self.output.read_text())
        self.assertEqual(proof["photos"], {"sample/a": "bbbbbbbbbb", "sample/b": "cccccccccc"})
        self.assertGreaterEqual(len(self.server.state["asset_requests"]), 4)

    def run_check_only(self, keys, timeout="5"):
        argv = ["verify_photo_deployment.py", "--keys", keys, "--timeout", timeout, "--output", str(self.output)]
        with mock.patch.object(deploy.webimg, "ROOT", self.root), \
             mock.patch.object(deploy, "ORIGIN", self.origin), \
             mock.patch.object(sys, "argv", argv), \
             contextlib.redirect_stdout(io.StringIO()):
            deploy.main()

    def test_check_only_verifies_current_head_without_build(self):
        # 只檢查（verify-photos）：沒有 --base 就用目前的 HEAD，不需要本次下載的原圖
        with mock.patch.object(deploy.webimg, "originals", side_effect=AssertionError("不該讀原圖")):
            self.run_check_only("sample/b")
        proof = json.loads(self.output.read_text())
        head = self.git("rev-parse", "HEAD").decode().strip()
        self.assertEqual(proof["sourceSha"], head)
        self.assertEqual(proof["photos"], {"sample/a": "bbbbbbbbbb", "sample/b": "cccccccccc"})
        self.assertIn("sample/b-600.cccccccccc.webp", self.server.state["asset_requests"])
        self.assertNotIn("sample/a-600.bbbbbbbbbb.webp", self.server.state["asset_requests"])

    def test_check_only_waits_when_site_not_updated(self):
        self.server.state["mode"] = "undeployed"
        with self.assertRaises(TimeoutError):
            self.run_check_only("sample/a", timeout="0.3")
        self.assertFalse(self.output.exists())

    def test_check_only_missing_file_never_writes_proof(self):
        self.server.state["mode"] = "partial"
        with self.assertRaises(TimeoutError):
            self.run_check_only("sample/b", timeout="0.3")
        self.assertFalse(self.output.exists())

    def test_check_only_rejects_bad_or_unknown_keys(self):
        for keys in ("", "reviews/x/y", "Sample/A", "sample/zz"):
            with self.subTest(keys=keys), self.assertRaises(ValueError):
                self.run_check_only(keys)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
