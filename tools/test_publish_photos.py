"""兩個暫存 clone 競爭同一個 manifest；只用小型本機圖片，不連 GitHub/R2。"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import webimg


def run(cwd, *args, env=None):
    return subprocess.run(args, cwd=cwd, env=env, check=True, text=True, capture_output=True).stdout


class PublishRaceTest(unittest.TestCase):
    def simulate(self, push_race=False):
        with tempfile.TemporaryDirectory(prefix="build-race-test-") as tmp:
            root = Path(tmp)
            seed = root / "seed"
            seed.mkdir()
            run(seed, "git", "init", "-b", "main")
            run(seed, "git", "config", "user.name", "Race test")
            run(seed, "git", "config", "user.email", "race@example.invalid")
            (seed / "tools").mkdir()
            (seed / ".gitignore").write_text("__pycache__/\n")
            for name in ["webimg.py", "publish-photos.py"]:
                shutil.copy2(webimg.ROOT / "tools" / name, seed / "tools" / name)
            (seed / "manifest.json").write_text(json.dumps({"version": 2, "recipe": webimg.RECIPE, "photos": {}, "reviews": {}}))
            run(seed, "git", "add", ".")
            run(seed, "git", "commit", "-qm", "seed")
            origin = root / "origin.git"
            run(root, "git", "clone", "--bare", str(seed), str(origin))
            clones, envs = [], []
            keys = ["race/a", "race/b"] if not push_race else ["reviews/race/a", "reviews/race/b"]
            for i, key in enumerate(keys):
                clone = root / f"clone-{i}"
                run(root, "git", "clone", str(origin), str(clone))
                run(clone, "git", "config", "user.name", "Race test")
                run(clone, "git", "config", "user.email", "race@example.invalid")
                originals = root / f"originals-{i}"
                src = originals / f"{key}.png"
                src.parent.mkdir(parents=True)
                run(root, "magick", "-size", "24x16", f"xc:{'red' if i == 0 else 'blue'}", str(src))
                env = {**os.environ, "JRFANK_ORIGINALS": str(originals), "PHOTOS": f"{key}.png"}
                run(clone, sys.executable, "tools/webimg.py", "build", key, env=env)
                clones.append(clone)
                envs.append(env)

            # 出圖之後監看 magick：恢復過程若重新編碼就直接失敗。
            bin_dir = root / "bin"
            bin_dir.mkdir()
            magick_log = root / "magick.log"
            wrapper = bin_dir / "magick"
            wrapper.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{magick_log}"\ncase "$*" in *mpr:o*) echo "unexpected re-encode" >&2; exit 91;; esac\nexec "{shutil.which("magick")}" "$@"\n')
            wrapper.chmod(0o755)
            envs[1]["PATH"] = f"{bin_dir}:{os.environ['PATH']}"
            before = {str(p.relative_to(clones[1])): (webimg.sha256(p), p.stat().st_mtime_ns) for p in clones[1].rglob("*.webp")}

            if push_race:
                # B 已 fetch/rebase 後，pre-push 讓 A 先更新遠端，逼出 non-fast-forward。
                marker = root / "pushed-a"
                hook = clones[1] / ".git/hooks/pre-push"
                hook.write_text(f'#!/bin/sh\nif [ ! -f "{marker}" ]; then\n  cd "{clones[0]}" || exit 1\n  JRFANK_ORIGINALS="{envs[0]["JRFANK_ORIGINALS"]}" "{sys.executable}" tools/publish-photos.py || exit 1\n  touch "{marker}"\nfi\n')
                hook.chmod(0o755)
                output = run(clones[1], sys.executable, "tools/publish-photos.py", env=envs[1])
            else:
                # B 在舊基底準備提交時暫停，讓 A 推送；B 隨後 fetch 必須遇到真實 rebase 衝突。
                ready, release = root / "ready", root / "release"
                hook = clones[1] / ".git/hooks/pre-commit"
                hook.write_text(f'#!/bin/sh\ntouch "{ready}"\nwhile [ ! -f "{release}" ]; do sleep 0.1; done\n')
                hook.chmod(0o755)
                with (root / "b.log").open("w+") as log:
                    proc = subprocess.Popen([sys.executable, "tools/publish-photos.py"], cwd=clones[1], env=envs[1], stdout=log, stderr=subprocess.STDOUT)
                    try:
                        deadline = time.monotonic() + 20
                        while not ready.exists():
                            if proc.poll() is not None or time.monotonic() > deadline:
                                self.fail("B 未到達提交屏障")
                            time.sleep(0.05)
                        run(clones[0], sys.executable, "tools/publish-photos.py", env=envs[0])
                        release.touch()
                        self.assertEqual(proc.wait(timeout=30), 0)
                    finally:
                        if proc.poll() is None:
                            proc.kill()
                            proc.wait()
                    log.seek(0)
                    output = log.read()
                self.assertIn("CONFLICT (content): Merge conflict in manifest.json", output)

            self.assertIn("重試（1/3）", output)
            self.assertIn("推送成功（第 2 次嘗試）", output)
            final = json.loads(run(root, "git", f"--git-dir={origin}", "show", "main:manifest.json"))
            area = "reviews" if push_race else "photos"
            self.assertEqual(set(final[area]), set(keys))
            after = {str(p.relative_to(clones[1])): (webimg.sha256(p), p.stat().st_mtime_ns) for p in clones[1].rglob("*.webp") if str(p.relative_to(clones[1])) in before}
            self.assertEqual(before, after)
            self.assertNotIn("mpr:o", magick_log.read_text())
            self.assertFalse((clones[1] / ".git/rebase-merge").exists())
            self.assertFalse((clones[1] / ".git/rebase-apply").exists())
            self.assertEqual(run(clones[1], "git", "status", "--porcelain"), "")
            check = run(clones[1], sys.executable, "tools/webimg.py", "check", env=envs[1])
            self.assertIn("不符 0；沒引用的舊檔 0", check)
            self.assertIn("沒有變化", run(clones[1], sys.executable, "tools/publish-photos.py", env=envs[1]))
            print(f"{'push rejection / reviews' if push_race else 'rebase conflict / photos'}: recovered on attempt 2; both keys present; WebP hashes/mtime unchanged; check passed", flush=True)

    def test_manifest_conflict(self):
        self.simulate()

    def test_push_race(self):
        self.simulate(push_race=True)


if __name__ == "__main__":
    unittest.main()
