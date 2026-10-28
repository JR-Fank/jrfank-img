#!/usr/bin/env python3
"""Actions 專用：提交本次下載的照片；競爭時在最新 main 上重建條目，最多重試三次。"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import webimg


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=webimg.ROOT, check=check)


def revision(ref: str) -> str:
    return subprocess.check_output(["git", "rev-parse", ref], cwd=webimg.ROOT, text=True).strip()


def publish() -> None:
    # Actions 的原圖目錄只裝本次下載的照片，包括資料夾／all 展開後的集合；出圖失敗的（太長、太大的影片）不提交。
    keys = webimg.batch_keys()
    if not keys:
        sys.exit("沒有本次照片，停止提交")
    manifest = webimg.load_manifest()
    cached = {"recipe": webimg.RECIPE, "photos": {}, "reviews": {}}
    for key in keys:
        cached[webimg.section(key)][key] = manifest[webimg.section(key)][key]
    files = sorted(webimg.referenced(cached))
    message = f"photos: build {os.environ.get('PHOTOS', ' '.join(keys))[:200]} (run {os.environ.get('GITHUB_RUN_ID', 'local')})"

    # reset --hard 會移除這次提交的圖；先備份到工作目錄外，重試只複製回來，絕不重新編碼。
    with tempfile.TemporaryDirectory(prefix="publish-photos-") as tmp:
        backup = Path(tmp)
        cache_path = backup / "manifest.json"
        cache_path.write_text(json.dumps(cached))
        for file in files:
            dst = backup / file
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(webimg.ROOT / file, dst)

        for attempt in range(4):  # 首次 + 最多三次重試
            git("add", "--", "manifest.json", *files)
            diff = git("diff", "--cached", "--quiet", check=False)
            if diff.returncode == 0:
                print("沒有變化", flush=True)
                return
            if diff.returncode != 1:
                diff.check_returncode()
            git("commit", "-q", "-m", message)
            git("fetch", "-q", "origin", "+refs/heads/main:refs/remotes/origin/main")
            latest = revision("origin/main")
            rebase = git("rebase", "origin/main", check=False)
            if rebase.returncode:
                # 不是衝突（例如設定或權限錯誤）時 abort 也會失敗，直接停止。
                git("rebase", "--abort")
            else:
                webimg.check([])
                push = git("push", "origin", "HEAD:refs/heads/main", check=False)
                if push.returncode == 0:
                    print(f"推送成功（第 {attempt + 1} 次嘗試）", flush=True)
                    return
                git("fetch", "-q", "origin", "+refs/heads/main:refs/remotes/origin/main")
                if revision("origin/main") == latest:
                    # 遠端沒有前進，並非競爭；權限／網路錯誤直接回報。
                    push.check_returncode()
            if attempt == 3:
                sys.exit("三次重試仍未能推送，停止")
            print(f"main 已更新，3 秒後重建本次條目並重試（{attempt + 1}/3）", flush=True)
            time.sleep(3)
            git("fetch", "-q", "origin", "+refs/heads/main:refs/remotes/origin/main")
            git("reset", "--hard", "origin/main")
            for file in files:
                dst = webimg.ROOT / file
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(backup / file, dst)
            webimg.build([*keys, "--reuse-manifest", str(cache_path)])
            webimg.check([])


if __name__ == "__main__":
    publish()
