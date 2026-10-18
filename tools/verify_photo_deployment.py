#!/usr/bin/env python3
"""從正式圖片站驗證本次部署，成功後寫入供後台核驗的證據。"""

import argparse
import hashlib
import http.client
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import webimg

ORIGIN = "https://pic.jrfank.cc"
MAX_MANIFEST_BYTES = 16 * 1024 * 1024


class NotReady(Exception):
    pass


class HTTPFailure(NotReady):
    def __init__(self, status):
        super().__init__(f"圖片站 HTTP {status}")
        self.status = status


def git(root, *args):
    return subprocess.check_output(["git", *args], cwd=root)


def valid_file(name):
    if not isinstance(name, str) or not name.endswith(".webp") or not name:
        raise ValueError("圖片檔名不對")
    path = PurePosixPath(name)
    if path.is_absolute() or str(path) != name or any(part in (".", "..") for part in path.parts):
        raise ValueError("圖片路徑不對")
    return name


def photo_versions(manifest):
    if not isinstance(manifest, dict):
        raise ValueError("manifest 格式不對")
    photos = manifest.get("photos")
    if not isinstance(photos, dict):
        raise ValueError("manifest 的 photos 格式不對")
    versions = {}
    for key, info in photos.items():
        if not isinstance(key, str) or not webimg.KEY_RE.fullmatch(key) or key.startswith(webimg.REVIEWS):
            raise ValueError("manifest 的照片鍵不對")
        version = info.get("version") if isinstance(info, dict) else None
        if not isinstance(version, str) or not re.fullmatch(r"[0-9a-f]{10}", version):
            raise ValueError(f"{key}：照片版本不對")
        versions[key] = version
    return versions


def prepare(root, base, keys):
    """核對已推送的 manifest，找出本次處理與建置期間新增的所有 WebP。"""
    root = Path(root)
    if not re.fullmatch(r"[0-9a-f]{40}", base):
        raise ValueError("建置起點 SHA 不對")
    source_sha = git(root, "rev-parse", "HEAD").decode().strip()
    if not re.fullmatch(r"[0-9a-f]{40}", source_sha):
        raise ValueError("推送後 SHA 不對")
    subprocess.run(["git", "merge-base", "--is-ancestor", base, source_sha], cwd=root, check=True)
    raw = (root / "manifest.json").read_bytes()
    if raw != git(root, "show", f"{source_sha}:manifest.json"):
        raise ValueError("工作目錄 manifest 與推送後提交不一致")
    manifest = json.loads(raw)
    versions = photo_versions(manifest)
    if not keys:
        raise ValueError("沒有本次處理的照片")
    assets = {}
    targets = {}
    for key in keys:
        section = webimg.section(key)
        info = manifest.get(section, {}).get(key)
        if not isinstance(info, dict) or not isinstance(info.get("sizes"), dict) or not info["sizes"]:
            raise ValueError(f"{key}：找不到本次照片的 manifest 資料")
        targets[key] = (section, info)
        for item in info["sizes"].values():
            name = valid_file(item["file"])
            details = (item["bytes"], item["sha256"])
            if name in assets and assets[name] != details:
                raise ValueError(f"{name}：manifest 的檔案資料不一致")
            assets[name] = details

    added = git(root, "diff", "--name-only", "-z", "--no-renames", "--diff-filter=A", base, source_sha)
    for entry in added.split(b"\0"):
        if not entry:
            continue
        name = entry.decode("utf-8")
        if name.endswith(".webp") and name not in assets:
            valid_file(name)
            path = root / name
            assets[name] = (path.stat().st_size, webimg.sha256(path))

    for name, (size, digest) in assets.items():
        path = root / name
        if not isinstance(size, int) or size <= 0 or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError(f"{name}：manifest 的大小或雜湊不對")
        if not path.is_file() or path.stat().st_size != size or webimg.sha256(path) != digest:
            raise ValueError(f"{name}：本機 WebP 與 manifest 不符")
    return {"manifestSha256": hashlib.sha256(raw).hexdigest(), "sourceSha": source_sha,
            "photos": versions, "targets": targets, "assets": assets}


def request(origin, path, method, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise NotReady("超過等待時間")
    url = f"{origin}/{urllib.parse.quote(path, safe='/')}?deploy-check={uuid.uuid4().hex}"
    req = urllib.request.Request(url, method=method, headers={"Cache-Control": "no-cache", "Accept-Encoding": "identity",
                                                             "User-Agent": "jrfank-photo-deploy-check/1"})
    try:
        response = urllib.request.urlopen(req, timeout=min(10, remaining))
        expected = urllib.parse.urlsplit(origin)
        final = urllib.parse.urlsplit(response.geturl())
        if (final.scheme, final.netloc) != (expected.scheme, expected.netloc):
            response.close()
            raise NotReady("圖片站轉址到其他 origin")
        return response
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()
        raise HTTPFailure(status) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise NotReady(f"圖片站請求失敗：{type(exc).__name__}") from None


def live_manifest(origin, deadline):
    with request(origin, "manifest.json", "GET", deadline) as response:
        if response.status != 200:
            raise NotReady(f"manifest HTTP {response.status}")
        raw = response.read(MAX_MANIFEST_BYTES + 1)
    if len(raw) > MAX_MANIFEST_BYTES:
        raise NotReady("線上 manifest 過大")
    try:
        manifest = json.loads(raw)
        versions = photo_versions(manifest)
    except (ValueError, TypeError, UnicodeDecodeError, AttributeError) as exc:
        raise NotReady(f"線上 manifest 無法讀取：{type(exc).__name__}") from None
    return manifest, versions


def check_manifest(snapshot, manifest, versions):
    if versions != snapshot["photos"]:
        raise NotReady("線上 photos 版本與本次 manifest 不一致")
    for key, (section, info) in snapshot["targets"].items():
        remote = manifest.get(section, {}).get(key)
        if not isinstance(remote, dict) or remote.get("version") != info.get("version") or remote.get("sizes") != info["sizes"]:
            raise NotReady(f"{key}：線上圖片資料尚未一致")


def check_asset(origin, name, size, digest, deadline):
    try:
        with request(origin, name, "HEAD", deadline) as response:
            if response.status != 200:
                raise NotReady(f"{name}：HTTP {response.status}")
            length = response.headers.get("Content-Length")
            kind = response.headers.get_content_type()
            if length is not None:
                try:
                    if int(length) != size:
                        raise NotReady(f"{name}：線上大小不符")
                except ValueError:
                    raise NotReady(f"{name}：線上大小無法讀取") from None
            if length is not None and kind == "image/webp":
                return
    except HTTPFailure as exc:
        # 有些伺服器不支援 HEAD；只在 405 時以 GET 驗證完整內容。
        if exc.status != 405:
            raise
    # HEAD 沒有大小或 WebP 類型時，讀取內容核對 SHA-256。
    with request(origin, name, "GET", deadline) as response:
        if response.status != 200:
            raise NotReady(f"{name}：HTTP {response.status}")
        data = response.read(size + 1)
    if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
        raise NotReady(f"{name}：線上內容不符")


def check_once(snapshot, origin, deadline):
    manifest, versions = live_manifest(origin, deadline)
    check_manifest(snapshot, manifest, versions)
    pool = ThreadPoolExecutor(max_workers=8)
    futures = [pool.submit(check_asset, origin, name, size, digest, deadline)
               for name, (size, digest) in snapshot["assets"].items()]
    try:
        for future in as_completed(futures):
            future.result()
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    manifest, versions = live_manifest(origin, deadline)
    check_manifest(snapshot, manifest, versions)


def wait_and_write(snapshot, origin, output, timeout=900, interval=15):
    output = Path(output)
    output.unlink(missing_ok=True)
    deadline = time.monotonic() + timeout
    while True:
        started = time.monotonic()
        try:
            check_once(snapshot, origin, deadline)
            if time.monotonic() >= deadline:
                raise NotReady("超過等待時間")
            proof = {"schema": 1, "checkedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                     "origin": origin, "sourceSha": snapshot["sourceSha"],
                     "manifestSha256": snapshot["manifestSha256"], "photos": snapshot["photos"]}
            output.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=output.parent, delete=False) as tmp:
                try:
                    json.dump(proof, tmp, ensure_ascii=False, sort_keys=True, indent=2)
                    tmp.write("\n")
                    temp_path = Path(tmp.name)
                except BaseException:
                    Path(tmp.name).unlink(missing_ok=True)
                    raise
            try:
                os.replace(temp_path, output)
            except OSError:
                temp_path.unlink(missing_ok=True)
                raise
            print(f"圖片部署已確認：{len(proof['photos'])} 張作品、{len(snapshot['assets'])} 個 WebP", flush=True)
            return proof
        except (NotReady, OSError, http.client.HTTPException) as exc:
            detail = str(exc) if isinstance(exc, NotReady) else type(exc).__name__
            print(f"圖片部署尚未就緒：{detail}", flush=True)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("15 分鐘內未能確認圖片部署")
        time.sleep(min(remaining, max(0, interval - (time.monotonic() - started))))


def main():
    parser = argparse.ArgumentParser(description="確認正式圖片站部署並產生證據")
    parser.add_argument("--base", required=True, help="checkout 時 main 的 commit SHA")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.unlink(missing_ok=True)
    keys = [key for key, _ in webimg.originals([])]
    snapshot = prepare(webimg.ROOT, args.base, keys)
    wait_and_write(snapshot, ORIGIN, args.output)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, TimeoutError, OSError, subprocess.CalledProcessError) as exc:
        sys.exit(f"圖片部署確認失敗：{exc}")
