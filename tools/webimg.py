#!/usr/bin/env python3
"""jrfank.cc 照片網頁檔產生器（只用標準庫 + ImageMagick 7 的 magick 指令；macOS 的 HDR HEIC 另用系統 ColorSync/sips，並用系統的 sRGB 描述檔）。

原圖不進這個倉庫：本機在 ~/Code/jrfank-site-assets/photo-originals/<系列>/<照片 id>.<副檔名>（R2 私有 bucket 的快取），
GitHub Actions 從 R2 下載到暫存目錄（用環境變數 JRFANK_ORIGINALS 指過去）。
評測文章的圖片在 reviews/<文章 slug>/<照片 id>.<副檔名>，manifest 裡另放一區 reviews（後台只看 photos，文章圖不會跑進「未分配」）。
每張原圖做成：

  - 長邊 600 / 1200 / 1800 / 2400 / 4000 五檔 WebP（quality 88、sharp YUV、method 6）
  - Lanczos 縮小後輕微銳化（-unsharp 0x0.6+0.5+0.008）；原圖不夠大就停在原圖尺寸，不放大，多出來的檔不做
  - 有內嵌色彩描述檔（Display P3 等）就轉成 sRGB；沒有的當作 sRGB；去掉全部 metadata（EXIF、GPS、XMP、ICC）
  - 按 EXIF 方向轉正
  - 透明背景（PNG 等，有 alpha 而且不是全不透明）保留透明，manifest 記 transparent: true（網站不在它底下墊占位）；
    有 alpha 但全不透明的拿掉 alpha。沒有 alpha 的原圖指令和以前完全一樣（所以配方字串沒變）
  - 檔名帶版本：<系列>/<照片 id>-<檔>.<版本>.webp，版本 = sha256(原圖 SHA-256 + 出圖配方) 前 10 碼。
    換原圖或改配方，網址就變，網站可以放心設長期快取
  - 占位主色（整張平均色）和極小模糊預覽（長邊 16px 的 WebP data URI，網站負責放大模糊）

技術資料全部寫進 manifest.json（各檔實際寬高、位元組、SHA-256、占位、EXIF、原圖尺寸），這是唯一來源；
網站倉庫用 `pnpm sync:photos` 把它原樣複製成 content/photos.json。EXIF 讀錯的欄位在 exif-overrides.json 改。

用法：
  python3 tools/webimg.py build                    # 全部（原圖和配方都沒變的跳過）
  python3 tools/webimg.py build hk/hk-8 --force    # 指定幾張；--force 重做
  python3 tools/webimg.py build reviews/orico-test-mac/orico-test-mac-3
  python3 tools/webimg.py check                    # 核對 manifest 和檔案（存在、大小、SHA-256、沒有多餘的檔）
  python3 tools/webimg.py prune                    # 刪掉 manifest 沒引用的舊版本檔（網站換到新版本、部署完再跑）

影片（.mov／.mp4／.m4v，出片細節見 tools/webvid.py）：
  - 同一個 id 同時有圖片原檔和影片原檔 = Live Photo：靜態圖照舊出 WebP，影片出兩檔無聲 MP4，manifest 加 live 欄位
  - 只有影片原檔 = 影片：第一格畫面當封面走同一套 WebP 五檔，影片出兩檔 MP4（保留聲音），manifest 加 video 欄位
  - 單一影片失敗（太長、太大、讀不了）只讓那一張失敗，其他照常處理；結束時退出碼 3
"""
from __future__ import annotations

import argparse
import base64
import importlib.util
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ORIGINALS = Path(os.environ.get("JRFANK_ORIGINALS", Path.home() / "Code/jrfank-site-assets/photo-originals"))
MANIFEST = ROOT / "manifest.json"
OVERRIDES = ROOT / "exif-overrides.json"
SRGB = "/System/Library/ColorSync/Profiles/sRGB Profile.icc"
EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".heic", ".heif", ".webp"}
VIDEO_EXTS = {".mov", ".mp4", ".m4v"}  # 和 webvid.VIDEO_EXTS 一樣（這裡不載入 webvid）
MAX_OUTPUT_BYTES = 24_000_000  # check 的單檔上限（EdgeOne 單檔上限 25 MB）

SIZES = (600, 1200, 1800, 2400, 4000)
QUALITY = 88  # 品質比對見 README「品質」
UNSHARP = "0x0.6+0.5+0.008"
LQIP = 16
# 改了上面任何一項（或 build_one 的指令）就改這個字串：所有照片的版本跟著變，網址也變。
# （2026-10-13 加透明處理時沒改：只影響有 alpha 的原圖，當時出過圖的原圖都沒有 alpha，強制重出逐位元組相同）
# 2026-10-19: macOS ColorSync normalizes HEIC files tagged Rec. 2100 PQ/HLG
# before ImageMagick resizes them.  That source conversion changes output bytes,
# so it is part of the version recipe even though ordinary JPEG/P3 output stays
# on the existing ImageMagick path.
# v2 與這個 v3 只在 HEIC/HEIF 的 HDR 輸入正規化不同。這兩個字串是既有版本的精確身分，
# 不要隨下一次配方更新一起改；新配方要另設 RECIPE，讓 legacy 相容規則自然失效。
LEGACY_V2_RECIPE = "v2 sizes=600,1200,1800,2400,4000 lanczos unsharp=0x0.6+0.5+0.008 webp q=88 m6 sharpyuv strip srgb"
V2_COMPATIBLE_RECIPE = "v3 sizes=600,1200,1800,2400,4000 lanczos unsharp=0x0.6+0.5+0.008 webp q=88 m6 sharpyuv strip srgb hdr-pq-colorsync"
RECIPE = V2_COMPATIBLE_RECIPE
V2_COMPATIBLE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}
LOW_RES = 1600  # 長邊不到這個數，manifest 的 lowRes 為 true（後台改看原圖尺寸，只對數位照片提醒）
# 照片鍵：<系列>/<id>（系列不能叫 reviews）；評測文章圖只能是三段 reviews/<文章 slug>/<id>（manifest 的 reviews 區）
REVIEWS = "reviews/"
KEY_RE = re.compile(r"^(?:reviews/[a-z0-9-]+/[a-z0-9-]+|(?!reviews/)[a-z0-9-]+/[a-z0-9-]+)$")


_WEBVID_LOCK = threading.Lock()


def webvid():
    """影片才需要 ffmpeg：用到時才載入 tools/webvid.py（build 是多執行緒，載入要上鎖）"""
    with _WEBVID_LOCK:
        mod = sys.modules.get("webvid")
        if mod is None:
            spec = importlib.util.spec_from_file_location("webvid", Path(__file__).with_name("webvid.py"))
            assert spec and spec.loader
            mod = importlib.util.module_from_spec(spec)
            # 要在執行模組之前登記：webvid 的 @dataclass（加上 from __future__ import annotations）會去 sys.modules 找自己的模組，
            # 沒登記就 AttributeError（Actions 的 Python 3.14 第一次真的出影片時發生；測試先 import 過 webvid 所以沒抓到）
            sys.modules["webvid"] = mod
            try:
                spec.loader.exec_module(mod)
            except BaseException:
                sys.modules.pop("webvid", None)
                raise
        return mod


def section(key: str) -> str:
    return "reviews" if key.startswith(REVIEWS) else "photos"


def magick(*args: str) -> str:
    return subprocess.run(["magick", *args], check=True, capture_output=True, text=True).stdout


def sips_property(src: Path, key: str) -> str:
    """Read one macOS ImageIO property without touching the source file."""
    try:
        out = subprocess.run(
            ["sips", "-g", key, str(src)], check=True, capture_output=True, text=True
        ).stdout
    except FileNotFoundError:
        return ""
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"sips 讀不到 {src.name} 的 {key} 屬性") from exc
    prefix = f"{key}:"
    for line in out.splitlines():
        line = line.strip()
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
    return ""


def is_hdr_heic(src: Path) -> bool:
    """Detect HEIC NCLX HDR tags that ImageMagick may expose as plain sRGB."""
    if src.suffix.lower() not in {".heic", ".heif"}:
        return False
    profile = sips_property(src, "profile").casefold()
    return any(tag in profile for tag in ("2100", "pq", "hlg"))


def sips_to_srgb(src: Path, dst: Path) -> None:
    """ColorSync-match an HDR HEIC to the local sRGB profile as a PNG."""
    try:
        subprocess.run(
            ["sips", "-m", SRGB, "-s", "format", "png", str(src), "--out", str(dst)],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"HDR HEIC 需要 macOS sips 才能轉成 sRGB：{src}") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"HDR HEIC 轉 sRGB 失敗：{src}") from exc


def prepare_input(src: Path, tmp: Path) -> tuple[Path, bool]:
    """Return the source used by ImageMagick and whether ColorSync converted it."""
    if not is_hdr_heic(src):
        return src, False
    dst = tmp / "input-srgb.png"
    sips_to_srgb(src, dst)
    return dst, True


def oriented_dimensions(src: Path) -> tuple[int, int]:
    """Return dimensions after ImageMagick's actual auto-orient decode.

    Some HEIC decoders physically apply the EXIF orientation while retaining
    ``EXIF:Orientation`` in the metadata.  Swapping dimensions from that
    metadata would report the opposite aspect ratio even though ``-auto-orient``
    has no further rotation to perform.  Querying the same operation used by
    the render path keeps manifest dimensions and output pixels in agreement.
    """
    raw = magick(
        f"{src}[0]", "-auto-orient", "-format", "%w %h", "info:"
    )
    w, h = map(int, raw.split())
    return w, h


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sources(only: list[str]) -> list[tuple[str, Path | None, Path | None]]:
    """[(鍵, 圖片原檔, 影片原檔)]：兩個都有 = Live Photo；只有影片 = 影片"""
    found: dict[str, dict[str, list[Path]]] = {}
    for p in sorted([*ORIGINALS.glob("*/*"), *ORIGINALS.glob(f"{REVIEWS}*/*")]):
        ext = p.suffix.lower()
        if not p.is_file() or ext not in EXTS | VIDEO_EXTS:
            continue
        key = p.relative_to(ORIGINALS).with_suffix("").as_posix()
        if not KEY_RE.match(key):
            sys.exit(f"原圖路徑不對：{p.relative_to(ORIGINALS)}（要是 <系列>/<id>.<副檔名>〔系列不能叫 reviews〕或 reviews/<文章>/<id>.<副檔名>，小寫英數和 -）")
        if not only or key in only:
            found.setdefault(key, {"image": [], "video": []})["video" if ext in VIDEO_EXTS else "image"].append(p)
    bad = [k for k in only if not KEY_RE.match(k)]
    if bad:
        sys.exit(f"照片鍵不對：{', '.join(bad)}（<系列>/<id> 或 reviews/<文章>/<id>）")
    dup = sorted(k for k, v in found.items() if len(v["image"]) > 1 or len(v["video"]) > 1)
    if dup:
        sys.exit(f"同一個照片 id 有兩個原圖（或兩個影片）：{', '.join(dup)}")
    missing = set(only) - set(found)
    if missing:
        sys.exit(f"找不到原圖：{', '.join(sorted(missing))}（在 {ORIGINALS}）")
    return [(k, (v["image"] or [None])[0], (v["video"] or [None])[0]) for k, v in sorted(found.items())]


def originals(only: list[str]) -> list[tuple[str, Path]]:
    """[(鍵, 主原檔)]：有圖片原檔就是圖片，否則是影片"""
    return [(k, image or video) for k, image, video in sources(only)]


def batch_keys() -> list[str]:
    """本次下載的照片，扣掉出圖失敗的（build 把失敗的鍵寫進 JRFANK_BUILD_FAILURES 指的檔）"""
    path = os.environ.get("JRFANK_BUILD_FAILURES")
    failed = set(Path(path).read_text().split()) if path and Path(path).is_file() else set()
    return [k for k, _ in originals([]) if k not in failed]


def load_manifest() -> dict:
    m = {"version": 2, "recipe": RECIPE, "photos": {}}
    if MANIFEST.exists():
        old = json.loads(MANIFEST.read_text())
        if old.get("version") == 2:
            m = old
    m.setdefault("reviews", {})
    return m


def entries(m: dict) -> dict[str, dict]:
    """photos 和 reviews 兩區合在一起（鍵不會重複：reviews 的鍵都以 reviews/ 開頭）"""
    return {**m["photos"], **m["reviews"]}


def save_manifest(m: dict) -> None:
    m["recipe"] = RECIPE
    for s in ("photos", "reviews"):
        m[s] = dict(sorted(m[s].items()))
    MANIFEST.write_text(json.dumps(m, ensure_ascii=False, indent=1) + "\n")


def tiers_for(long_edge: int) -> list[int]:
    """要做的檔：比原圖小的全做，再加一個「停在原圖尺寸」的（原圖剛好是某檔大小就是那一檔）"""
    out = []
    for s in SIZES:
        out.append(s)
        if s >= long_edge:
            break
    return out


def recipe_version(src_sha: str, recipe: str) -> str:
    return hashlib.sha256(f"{src_sha}\n{recipe}".encode()).hexdigest()[:10]


def photo_version(src_sha: str) -> str:
    return recipe_version(src_sha, RECIPE)


def video_version(video_sha: str) -> str:
    """只有影片的條目：封面 WebP 和 MP4 共用，照片配方或影片配方改了都會變"""
    return hashlib.sha256(f"{video_sha}\n{RECIPE}\n{webvid().VIDEO_RECIPE}".encode()).hexdigest()[:10]


def live_version(video_sha: str) -> str:
    """Live Photo 的動態部分：只看影片和影片配方（靜態圖照舊用 photo_version）"""
    return hashlib.sha256(f"{video_sha}\n{webvid().VIDEO_RECIPE}\nlive".encode()).hexdigest()[:10]


def hi_video_version(video_sha: str, live: bool) -> str:
    """高解析度檔另用版本，補檔不會改寫已上線的 1280／1920。"""
    kind = "live" if live else RECIPE
    return hashlib.sha256(f"{video_sha}\n{webvid().HI_TIER_RECIPE}\n{kind}".encode()).hexdigest()[:10]


def motion_versions(video_sha: str, meta, live: bool) -> dict[int, str]:
    legacy = live_version(video_sha) if live else video_version(video_sha)
    high = hi_video_version(video_sha, live)
    return {
        tier: legacy if tier in webvid().LEGACY_TIERS else high
        for tier, _w, _h in webvid().tiers(meta)
    }


def build_one(key: str, src: Path, src_sha: str, *, frame: Path | None = None, version: str | None = None,
              exif: dict | None = None, converted: bool = False) -> dict:
    """出 WebP 五檔。frame：影片的封面畫面（src 是影片時由 build_video 傳入）"""
    version = version or photo_version(src_sha)
    pic = frame or src
    w0, h0 = oriented_dimensions(pic)
    icc = bool(magick("identify", "-format", "%[profile:icc]", f"{pic}[0]").strip())
    # 透明：有 alpha 而且不是全不透明（PNG 截圖、去背產品圖）。有 alpha 但全不透明的，alpha 拿掉
    alpha = magick("identify", "-format", "%A", f"{pic}[0]").strip() not in ("Undefined", "")
    transparent = alpha and magick("identify", "-format", "%[opaque]", f"{pic}[0]").strip() != "True"

    tiers = tiers_for(max(w0, h0))
    with tempfile.TemporaryDirectory() as tmp:
        input_src, hdr = prepare_input(pic, Path(tmp))
        args = [f"{input_src}[0]", "-auto-orient"]
        if alpha and not transparent:
            args += ["-alpha", "off"]
        if icc:
            args += ["-profile", SRGB]
        args += ["-colorspace", "sRGB", "-write", "mpr:o", "+delete"]
        # 透明的只銳化顏色，不銳化 alpha（邊緣不出光暈）；沒有 alpha 的指令和以前一樣
        sharpen = ["-channel", "RGB", "-unsharp", UNSHARP, "+channel"] if transparent else ["-unsharp", UNSHARP]
        for s in tiers:
            args += [
                "(", "mpr:o", "-filter", "Lanczos", "-resize", f"{s}x{s}>", *sharpen, "-strip",
                "-quality", str(QUALITY), "-define", "webp:method=6", "-define", "webp:use-sharp-yuv=true",
                "-write", f"{tmp}/{s}.webp", "+delete", ")",
            ]
        # 占位：平均色、16px 預覽（從最小一檔縮，已是 sRGB；透明的先鋪白底）
        args += ["null:"]
        magick(*args)
        small = f"{tmp}/{tiers[0]}.webp"
        flat = ["-background", "white", "-flatten"] if transparent else []
        color = magick(small, *flat, "-scale", "1x1!", "-format", "#%[hex:p{0,0}]", "info:").strip().lower()[:7]
        magick(small, *flat, "-resize", f"{LQIP}x{LQIP}>", "-strip", "-quality", "50", "-define", "webp:method=6", f"{tmp}/lqip.webp")
        blur = "data:image/webp;base64," + base64.b64encode(Path(f"{tmp}/lqip.webp").read_bytes()).decode()

        sizes = {}
        for s in tiers:
            t = Path(f"{tmp}/{s}.webp")
            w, h = map(int, magick("identify", "-format", "%w %h", str(t)).split())
            file = f"{key}-{s}.{version}.webp"
            dst = ROOT / file
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(t.read_bytes())
            sizes[str(s)] = {"file": file, "width": w, "height": h, "bytes": dst.stat().st_size, "sha256": sha256(dst)}
    big = sizes[str(tiers[-1])]
    return {
        "version": version,
        "width": big["width"],
        "height": big["height"],
        "sizes": sizes,
        "color": color,
        "blur": blur,
        "original": {"width": w0, "height": h0},
        "lowRes": max(w0, h0) < LOW_RES,
        **({"transparent": True} if transparent else {}),
        "exif": exif if exif is not None else exif_fields(src, key),
        "colorConverted": icc or hdr or converted,
        "source": src.name,
        "sourceSha256": src_sha,
    }


def up_to_date(info: dict | None, src: Path, src_sha: str, version: str | None = None) -> bool:
    if not info or info.get("sourceSha256") != src_sha:
        return False
    expected = version or photo_version(src_sha)
    matches_current = info.get("version") == expected
    matches_legacy_v2 = (
        version is None
        and RECIPE == V2_COMPATIBLE_RECIPE
        and src.suffix.lower() in V2_COMPATIBLE_EXTS
        and info.get("version") == recipe_version(src_sha, LEGACY_V2_RECIPE)
    )
    return (matches_current or matches_legacy_v2) and all(
        (ROOT / t["file"]).exists() for t in info["sizes"].values()
    )


def motion_up_to_date(part: dict | None, versions: dict[int, str]) -> bool:
    if not part or not isinstance(part.get("files"), dict):
        return False
    files = part["files"]
    omitted = {str(t) for t in part.get("omitted", [])}
    if omitted and part.get("omittedRecipe") != webvid().HI_TIER_RECIPE_ID:
        return False
    expected = {str(t) for t in versions}
    if set(files) | omitted != expected or set(files) & omitted:
        return False
    return all(
        t.get("file", "").endswith(f".{versions[int(tier)]}.mp4")
        and (ROOT / t["file"]).exists()
        for tier, t in files.items()
    )


def install(tmp: Path, part: dict) -> None:
    """MP4 先出在暫存目錄，整張成功才放進倉庫（失敗不留半套檔）"""
    for t in part["files"].values():
        dst = ROOT / t["file"]
        built = tmp / t["file"]
        if not built.exists():  # 沿用的舊檔已在 ROOT，不重寫
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(built.read_bytes())


def video_exif(meta, key: str) -> dict:
    return exif_fields(None, key, webvid().exif_tags(meta))


def build_video(key: str, src: Path, src_sha: str, meta, prev: dict | None = None) -> dict:
    """只有影片：第一格當封面出 WebP，影片補齊各檔（保留聲音）。"""
    wv = webvid()
    version = video_version(src_sha)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        video = wv.render(src, meta, tmp, key, motion_versions(src_sha, meta, live=False), audio=True,
                          existing=(prev or {}).get("video"), existing_root=ROOT)
        if up_to_date(prev, src, src_sha, version):
            info = {k: v for k, v in prev.items() if k != "video"}
            info["exif"] = video_exif(meta, key)
        else:
            wv.cover(src, meta, tmp / "cover.ppm")
            info = build_one(key, src, src_sha, frame=tmp / "cover.ppm", version=version,
                             exif=video_exif(meta, key), converted=meta.hdr)
        install(tmp, video)
    return {**info, "video": video}


def build_live(key: str, src: Path, src_sha: str, meta, prev: dict | None = None) -> dict:
    """Live Photo 的動態部分：補齊各檔無聲 MP4。"""
    wv = webvid()
    with tempfile.TemporaryDirectory() as tmp:
        live = wv.render(src, meta, Path(tmp), key, motion_versions(src_sha, meta, live=True), audio=False,
                         existing=prev, existing_root=ROOT)
        install(Path(tmp), live)
    return {**live, "source": src.name, "sourceSha256": src_sha}


def build(args: list[str]) -> None:
    parser = argparse.ArgumentParser(description="出圖並更新指定照片的 manifest")
    parser.add_argument("keys", nargs="*")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--reuse-manifest", type=Path, help="重試時沿用這次已出好的圖與條目，不重新編碼")
    opts = parser.parse_args(args)
    items = sources(opts.keys)
    manifest = load_manifest()
    cached = json.loads(opts.reuse_manifest.read_text()) if opts.reuse_manifest else None
    if cached and cached.get("recipe") != RECIPE:
        sys.exit("快取的出圖配方不同，不能沿用")

    def run_photo(key: str, src: Path, prev: dict | None) -> dict:
        src_sha = sha256(src)
        fresh = up_to_date(prev, src, src_sha) and not {"video", "live"} & set(prev)
        if cached is not None and not fresh:
            sys.exit(f"{key}：快取或已出好的圖不完整，停止重試")
        if cached is not None or (not opts.force and fresh):
            # EXIF 修正（exif-overrides.json）不影響圖檔，照樣更新
            return {**prev, "exif": exif_fields(src, key)}
        return build_one(key, src, src_sha)

    def run_video(key: str, src: Path, prev: dict | None) -> dict:
        src_sha = sha256(src)
        meta = webvid().probe(src)
        versions = motion_versions(src_sha, meta, live=False)
        fresh = up_to_date(prev, src, src_sha, video_version(src_sha)) and motion_up_to_date((prev or {}).get("video"), versions)
        if cached is not None and not fresh:
            sys.exit(f"{key}：快取或已出好的影片不完整，停止重試")
        if cached is not None or (not opts.force and fresh):
            return {**prev, "exif": video_exif(meta, key)}
        # 只在同一支原片补档时沿用旧 MP4；换原片或 --force 都完整重出。
        reusable = prev if not opts.force and (prev or {}).get("sourceSha256") == src_sha else None
        return build_video(key, src, src_sha, meta, reusable)

    def run_live(key: str, image: Path, video: Path, prev: dict | None) -> dict:
        img_sha, vid_sha = sha256(image), sha256(video)
        still = up_to_date(prev, image, img_sha) and "video" not in prev
        live_prev = (prev or {}).get("live")
        meta = webvid().probe(video)
        versions = motion_versions(vid_sha, meta, live=True)
        moving = bool(live_prev) and live_prev.get("sourceSha256") == vid_sha and motion_up_to_date(live_prev, versions)
        if cached is not None and not (still and moving):
            sys.exit(f"{key}：快取或已出好的 Live Photo 不完整，停止重試")
        reuse = cached is not None or not opts.force
        info = {k: v for k, v in prev.items() if k != "live"} if reuse and still else build_one(key, image, img_sha)
        info["exif"] = exif_fields(image, key)
        # Live 的影片原档身份在 live.sourceSha256；换片或 --force 不能继承旧档／跳档记录。
        reusable_motion = live_prev if not opts.force and (live_prev or {}).get("sourceSha256") == vid_sha else None
        live = live_prev if reuse and moving else build_live(key, video, vid_sha, meta, reusable_motion)
        return {**info, "live": live}

    def run(item: tuple[str, Path | None, Path | None]) -> tuple[str, dict | None, dict | None]:
        """單一工作單位的最外層隔離：任何 Exception 都只能讓這一張失敗。"""
        key, image, video = item
        try:
            prev = manifest[section(key)].get(key)
            if cached is not None:
                prev = cached.get(section(key), {}).get(key)
            if video is None:
                return key, run_photo(key, image, prev), None
            info = run_live(key, image, video, prev) if image else run_video(key, video, prev)
            return key, info, None
        except Exception as exc:
            is_video_error = video is not None and isinstance(exc, webvid().VideoError)
            if is_video_error:
                code, message = exc.code, str(exc)
            else:
                source = video or image
                detail = str(exc).strip()
                kind = type(exc).__name__
                suffix = f"：{detail}" if detail else ""
                message = f"{source.name if source else key}：處理這一張時發生未分類錯誤（{kind}{suffix}）"
                code = "unreadable" if video is not None else "processing"
            failure = {"key": key, "kind": "live" if image and video else "video" if video else "photo",
                       "code": code, "message": message}
            if is_video_error and exc.duration is not None:
                failure["duration"] = exc.duration
            return key, None, failure

    failures: list[dict] = []
    workers = max(1, (os.cpu_count() or 4) // 2)  # 每個 magick 自己也開多執行緒
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for key, info, error in pool.map(run, items):
            if error:
                failures.append(error)
                print(f"{key}: ✗ 失敗：{error['message']}")
                continue
            photos = manifest[section(key)]
            old = photos.get(key, {}).get("version")
            old_motion = (photos.get(key, {}).get("live") or {}).get("files")
            photos[key] = info
            flag = "（已轉 sRGB）" if info["colorConverted"] else ""
            low = "（清晰度不足）" if info["lowRes"] and section(key) == "photos" else ""
            alpha = "（透明）" if info.get("transparent") else ""
            state = "沒變" if old == info["version"] else f"新版本 {info['version']}"
            kind = ""
            if "video" in info:
                kind = f"（影片 {info['video']['duration']:g} 秒、{len(info['video']['files'])} 檔 MP4{'、有聲音' if info['video']['audio'] else ''}）"
            elif "live" in info:
                moved = "沒變" if old_motion == info["live"]["files"] else "新出"
                kind = f"（Live Photo {info['live']['duration']:g} 秒、{len(info['live']['files'])} 檔 MP4 {moved}）"
            print(f"{key}: {info['width']}×{info['height']} {len(info['sizes'])} 檔 {state}{flag}{low}{alpha}{kind}")
    save_manifest(manifest)
    record = os.environ.get("JRFANK_BUILD_FAILURES")
    if record:
        Path(record).write_text("".join(f"{f['key']}\n" for f in failures))
    write_failure_report(failures)
    if failures:
        print(f"\n{len(failures)} 張失敗（其他照片已照常處理、manifest 已更新）：")
        for f in failures:
            print(f"  ✗ {f['key']}：{f['message']}")
        sys.exit(3)


def write_failure_report(failures: list[dict]) -> None:
    """設了 JRFANK_BUILD_FAILURES_JSON 就把失敗的鍵和原因寫成 JSON（Actions 存成 photo-build-failures artifact，後台讀它在那一張上顯示原因）。
    鍵是照片鍵（<系列>/<id>，不含副檔名）；code 見 webvid.VideoError。沒有失敗也寫（failures 是空的）"""
    out = os.environ.get("JRFANK_BUILD_FAILURES_JSON")
    if not out:
        return
    report = {"schema": 1, "failures": sorted(failures, key=lambda f: f["key"])}
    Path(out).write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n")


def outputs(info: dict) -> list[tuple[str, dict]]:
    """一張的所有輸出檔：[(標籤, {file, bytes, sha256, ...})]，WebP 和 MP4（影片、Live Photo）"""
    out = list(info["sizes"].items())
    for part in ("video", "live"):
        if part in info:
            out += [(f"{part} {s}", t) for s, t in info[part]["files"].items()]
    return out


def referenced(manifest: dict) -> set[str]:
    return {t["file"] for p in entries(manifest).values() for _, t in outputs(p)}


def stray_files(manifest: dict) -> list[Path]:
    refs = referenced(manifest)
    out = []
    globs = [g for ext in ("webp", "mp4") for g in (*ROOT.glob(f"*/*.{ext}"), *ROOT.glob(f"{REVIEWS}*/*.{ext}"))]
    for p in sorted(globs):
        if p.relative_to(ROOT).as_posix() not in refs:
            out.append(p)
    return out


def check(_: list[str]) -> None:
    manifest = load_manifest()
    bad = []
    for key, info in entries(manifest).items():
        for s, t in outputs(info):
            f = ROOT / t["file"]
            if not f.exists():
                bad.append(f"{key} {s}：沒有 {t['file']}")
            elif f.stat().st_size != t["bytes"] or sha256(f) != t["sha256"]:
                bad.append(f"{key} {s}：{t['file']} 和 manifest 不符")
            elif t["bytes"] > MAX_OUTPUT_BYTES:
                bad.append(f"{key} {s}：{t['file']} 有 {t['bytes'] / 1e6:.1f} MB，超過單檔 {MAX_OUTPUT_BYTES // 1_000_000} MB 上限")
    stray = stray_files(manifest)
    count = {s: (len(manifest[s]), sum(len(p["sizes"]) for p in manifest[s].values())) for s in ("photos", "reviews")}
    mp4 = sum(len(outputs(p)) - len(p["sizes"]) for p in entries(manifest).values())
    print(
        f"manifest 照片 {count['photos'][0]} 張 {count['photos'][1]} 檔、文章圖 {count['reviews'][0]} 張 {count['reviews'][1]} 檔"
        f"{f'、MP4 {mp4} 檔' if mp4 else ''}；不符 {len(bad)}；沒引用的舊檔 {len(stray)}"
    )
    for b in bad:
        print("  ✗", b)
    for p in stray:
        print("  · 沒引用：", p.relative_to(ROOT))
    if bad:
        sys.exit(1)


def prune(_: list[str]) -> None:
    for p in stray_files(load_manifest()):
        p.unlink()
        print("刪除", p.relative_to(ROOT))


# ---------- EXIF → manifest 的 exif 欄位（網站照這個顯示） ----------

FIELDS = "Make|Model|LensMake|LensModel|FocalLength|FNumber|ExposureTime|PhotographicSensitivity|DateTimeOriginal"


def read_exif(src: Path) -> dict[str, str]:
    fmt = "\\n".join(f"{k}=%[EXIF:{k}]" for k in FIELDS.split("|"))
    raw = magick("identify", "-format", fmt, f"{src}[0]")
    return {k: v.strip() for k, _, v in (line.partition("=") for line in raw.splitlines()) if v.strip()}


def frac(s: str) -> Fraction:
    return Fraction(s.split(",")[0])


def trim(x: float, nd: int) -> str:
    return f"{x:.{nd}f}".rstrip("0").rstrip(".")


SONY_ALPHA = {"ILCE-7": "α7", "ILCE-6300": "α6300"}


def camera_name(e: dict[str, str]) -> str | None:
    make, model = e.get("Make", "").strip(), e.get("Model", "").strip()
    if not model:
        return None
    if make.upper() == "APPLE" or model.lower().startswith(make.lower()):
        return model
    brand = {"FUJIFILM": "Fujifilm", "SONY": "Sony"}.get(make.upper(), make.title())
    return f"{brand} {SONY_ALPHA.get(model, model)}"


def lens_name(e: dict[str, str]) -> str | None:
    lens = e.get("LensModel", "").strip()
    if not lens:
        return None
    model = e.get("Model", "")
    if model and lens.startswith(model):  # iPhone：「iPhone 13 Pro back triple camera 5.7mm f/1.5」
        lens = lens[len(model):].strip()
        lens = lens[:1].upper() + lens[1:]
    lens = lens.split(" | ")[0]  # Sigma 的「| Contemporary 018」
    if lens.startswith("SIGMA "):
        lens = "Sigma " + lens[6:]
    m = re.match(r"^(XC|XF)(\d+)-(\d+)mmF[\d.\-]+", lens)  # 富士：XC50-230mmF4.5-6.7 OIS II → XC 50–230mm
    if m:
        return f"{m[1]} {m[2]}–{m[3]}mm"
    return re.sub(r"(\d+)(?:\.0)?-(\d+)(?:\.0)? ?mm", r"\1–\2mm", lens)


def exif_fields(src: Path | None, key: str, raw: dict[str, str] | None = None) -> dict:
    """讀得到才填，不猜。exif-overrides.json 可以改或刪（值寫 null）某張的欄位，例如相機時鐘沒設的日期
    raw：已經讀好的欄位（影片從 ffprobe 只給相機型號和拍攝日期）"""
    e = raw if raw is not None else read_exif(src)
    out: dict = {}
    if cam := camera_name(e):
        out["camera"] = cam
    if lens := lens_name(e):
        out["lens"] = lens
    if "FocalLength" in e:
        f = float(frac(e["FocalLength"]))
        out["focal"] = f"{trim(f, 2) if f < 10 else round(f)}mm"
    if "FNumber" in e:
        out["aperture"] = f"f/{trim(float(frac(e['FNumber'])), 1)}"
    if "ExposureTime" in e:
        t = frac(e["ExposureTime"])
        out["shutter"] = f"1/{round(1 / t)}s" if t < 1 else f"{trim(float(t), 1)}s"
    if "PhotographicSensitivity" in e:
        out["iso"] = int(e["PhotographicSensitivity"].split(",")[0])
    if "DateTimeOriginal" in e:
        out["date"] = e["DateTimeOriginal"][:10].replace(":", "-")
    overrides = json.loads(OVERRIDES.read_text()).get(key, {}) if OVERRIDES.exists() else {}
    for k, v in overrides.items():
        if k.startswith("_"):
            continue
        if v is None:
            out.pop(k, None)
        else:
            out[k] = v
    return out


if __name__ == "__main__":
    cmd, *rest = sys.argv[1:] or ["help"]
    {"build": build, "check": check, "prune": prune}.get(cmd, lambda _: print(__doc__))(rest)
