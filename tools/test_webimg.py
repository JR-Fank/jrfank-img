"""清晰度邊界、沿用圖片與出錯診斷；只使用暫存圖片，不連 GitHub/R2。"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import webimg


class WebimgTest(unittest.TestCase):
    def test_low_res_boundaries_and_reuse(self):
        with tempfile.TemporaryDirectory(prefix="webimg-test-") as tmp:
            root = Path(tmp)
            (root / "tools").mkdir()
            shutil.copy2(webimg.ROOT / "tools/webimg.py", root / "tools/webimg.py")
            originals = root / "originals"
            dimensions = {"below": "1599x16", "at": "1600x16", "portrait": "16x1601"}
            for key, size in dimensions.items():
                path = originals / f"sample/{key}.png"
                path.parent.mkdir(parents=True, exist_ok=True)
                subprocess.run(["magick", "-size", size, "xc:red", str(path)], check=True, capture_output=True)
            env = {**os.environ, "JRFANK_ORIGINALS": str(originals)}
            command = [sys.executable, str(root / "tools/webimg.py"), "build"]

            def build(*args):
                subprocess.run([*command, *args], cwd=root, env=env, check=True, capture_output=True, text=True)

            def manifest():
                return json.loads((root / "manifest.json").read_text())

            expected = {"sample/below": True, "sample/at": False, "sample/portrait": False}
            build()
            self.assertEqual({k: v["lowRes"] for k, v in manifest()["photos"].items()}, expected)
            outputs = {p.relative_to(root): (webimg.sha256(p), p.stat().st_mtime_ns) for p in root.rglob("*.webp")}

            # 舊標記在一般重跑和競爭恢復兩條路徑都更新，不重編碼圖檔。
            for reuse in (False, True):
                stale = manifest()
                for key, info in stale["photos"].items():
                    info["lowRes"] = not expected[key]
                (root / "manifest.json").write_text(json.dumps(stale))
                cache = root / "cache.json"
                cache.write_text(json.dumps(stale))
                build(*(["--reuse-manifest", str(cache)] if reuse else []))
                self.assertEqual({k: v["lowRes"] for k, v in manifest()["photos"].items()}, expected)
                self.assertEqual({p.relative_to(root): (webimg.sha256(p), p.stat().st_mtime_ns)
                                  for p in root.rglob("*.webp")}, outputs)

    def test_magick_error_retains_reason_without_sensitive_values(self):
        path = "/tmp/private folder/image.png"
        secret = "synthetic-token-for-test"
        stderr = f"magick: no decode delegate for this image format '{path}' @ error/constitute.c/ReadImage/587. {secret} https://example.invalid/?token={secret} authorization=unknown-token"
        error = subprocess.CalledProcessError(1, ["magick"], stderr=stderr)
        with patch.dict(os.environ, {"TEST_SECRET_KEY": secret}), patch.object(webimg.subprocess, "run", side_effect=error):
            with self.assertRaises(RuntimeError) as caught:
                webimg.magick("identify", f"{path}[0]")
        message = str(caught.exception)
        self.assertIn("no decode delegate", message)
        self.assertNotIn(path, message)
        self.assertNotIn(secret, message)
        self.assertNotIn("example.invalid", message)
        self.assertNotIn("unknown-token", message)

    def test_identify_error_includes_actual_diagnostic(self):
        with tempfile.TemporaryDirectory(prefix="webimg-bad-image-") as tmp:
            root = Path(tmp)
            (root / "tools").mkdir()
            shutil.copy2(webimg.ROOT / "tools/webimg.py", root / "tools/webimg.py")
            path = root / "originals/sample/invalid.png"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"not an image")
            env = {**os.environ, "JRFANK_ORIGINALS": str(root / "originals")}
            result = subprocess.run([sys.executable, str(root / "tools/webimg.py"), "build", "sample/invalid"],
                                    cwd=root, env=env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            message = result.stderr
            self.assertIn("sample/invalid", message)
            self.assertIn("ImageMagick 失敗", message)
            self.assertNotIn("沒有診斷輸出", message)
            self.assertNotIn(str(path), message)
            self.assertNotIn("Traceback", message)


if __name__ == "__main__":
    unittest.main()
