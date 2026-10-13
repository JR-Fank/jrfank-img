#!/usr/bin/env python3
"""jrfank.cc 照片網頁檔產生器（只用標準庫 + ImageMagick 7 的 magick 指令；要在 macOS 跑，用系統的 sRGB 描述檔）。

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
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ORIGINALS = Path(os.environ.get("JRFANK_ORIGINALS", Path.home() / "Code/jrfank-site-assets/photo-originals"))
MANIFEST = ROOT / "manifest.json"
OVERRIDES = ROOT / "exif-overrides.json"
SRGB = "/System/Library/ColorSync/Profiles/sRGB Profile.icc"
EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".heic", ".heif", ".webp"}

SIZES = (600, 1200, 1800, 2400, 4000)
QUALITY = 88  # 品質比對見 README「品質」
UNSHARP = "0x0.6+0.5+0.008"
LQIP = 16
# 改了上面任何一項（或 build_one 的指令）就改這個字串：所有照片的版本跟著變，網址也變。
# （2026-10-13 加透明處理時沒改：只影響有 alpha 的原圖，當時出過圖的原圖都沒有 alpha，強制重出逐位元組相同）
RECIPE = f"v2 sizes={','.join(map(str, SIZES))} lanczos unsharp={UNSHARP} webp q={QUALITY} m6 sharpyuv strip srgb"
LOW_RES = 2400  # 長邊不到這個數，後台標「清晰度不足」
# 照片鍵：<系列>/<id>（系列不能叫 reviews）；評測文章圖只能是三段 reviews/<文章 slug>/<id>（manifest 的 reviews 區）
REVIEWS = "reviews/"
KEY_RE = re.compile(r"^(?:reviews/[a-z0-9-]+/[a-z0-9-]+|(?!reviews/)[a-z0-9-]+/[a-z0-9-]+)$")


def section(key: str) -> str:
    return "reviews" if key.startswith(REVIEWS) else "photos"


def magick(*args: str) -> str:
    return subprocess.run(["magick", *args], check=True, capture_output=True, text=True).stdout


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def originals(only: list[str]) -> list[tuple[str, Path]]:
    out = []
    for p in sorted([*ORIGINALS.glob("*/*"), *ORIGINALS.glob(f"{REVIEWS}*/*")]):
        if not p.is_file() or p.suffix.lower() not in EXTS:
            continue
        key = p.relative_to(ORIGINALS).with_suffix("").as_posix()
        if not KEY_RE.match(key):
            sys.exit(f"原圖路徑不對：{p.relative_to(ORIGINALS)}（要是 <系列>/<id>.<副檔名>〔系列不能叫 reviews〕或 reviews/<文章>/<id>.<副檔名>，小寫英數和 -）")
        if not only or key in only:
            out.append((key, p))
    bad = [k for k in only if not KEY_RE.match(k)]
    if bad:
        sys.exit(f"照片鍵不對：{', '.join(bad)}（<系列>/<id> 或 reviews/<文章>/<id>）")
    keys = [k for k, _ in out]
    dup = {k for k in keys if keys.count(k) > 1}
    if dup:
        sys.exit(f"同一個照片 id 有兩個原圖：{', '.join(sorted(dup))}")
    missing = set(only) - set(keys)
    if missing:
        sys.exit(f"找不到原圖：{', '.join(sorted(missing))}（在 {ORIGINALS}）")
    return out


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


def build_one(key: str, src: Path, src_sha: str) -> dict:
    version = hashlib.sha256(f"{src_sha}\n{RECIPE}".encode()).hexdigest()[:10]
    w0, h0 = map(int, magick("identify", "-format", "%w %h", f"{src}[0]").split())
    orient = magick("identify", "-format", "%[EXIF:Orientation]", f"{src}[0]").strip()
    if orient in {"5", "6", "7", "8"}:
        w0, h0 = h0, w0
    icc = bool(magick("identify", "-format", "%[profile:icc]", f"{src}[0]").strip())
    # 透明：有 alpha 而且不是全不透明（PNG 截圖、去背產品圖）。有 alpha 但全不透明的，alpha 拿掉
    alpha = magick("identify", "-format", "%A", f"{src}[0]").strip() not in ("Undefined", "")
    transparent = alpha and magick("identify", "-format", "%[opaque]", f"{src}[0]").strip() != "True"

    tiers = tiers_for(max(w0, h0))
    with tempfile.TemporaryDirectory() as tmp:
        args = [f"{src}[0]", "-auto-orient"]
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
        "exif": exif_fields(src, key),
        "colorConverted": icc,
        "source": src.name,
        "sourceSha256": src_sha,
    }


def up_to_date(info: dict | None, src_sha: str) -> bool:
    if not info or info.get("sourceSha256") != src_sha:
        return False
    version = hashlib.sha256(f"{src_sha}\n{RECIPE}".encode()).hexdigest()[:10]
    return info.get("version") == version and all((ROOT / t["file"]).exists() for t in info["sizes"].values())


def build(args: list[str]) -> None:
    force = "--force" in args
    only = [a for a in args if not a.startswith("--")]
    items = originals(only)
    manifest = load_manifest()

    def run(kv: tuple[str, Path]) -> tuple[str, dict | None]:
        key, src = kv
        src_sha = sha256(src)
        prev = manifest[section(key)].get(key)
        if not force and up_to_date(prev, src_sha):
            # EXIF 修正（exif-overrides.json）不影響圖檔，照樣更新
            return key, {**prev, "exif": exif_fields(src, key)}
        return key, build_one(key, src, src_sha)

    workers = max(1, (os.cpu_count() or 4) // 2)  # 每個 magick 自己也開多執行緒
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for key, info in pool.map(run, items):
            photos = manifest[section(key)]
            old = photos.get(key, {}).get("version")
            photos[key] = info
            flag = "（已轉 sRGB）" if info["colorConverted"] else ""
            low = "（清晰度不足）" if info["lowRes"] and section(key) == "photos" else ""
            alpha = "（透明）" if info.get("transparent") else ""
            state = "沒變" if old == info["version"] else f"新版本 {info['version']}"
            print(f"{key}: {info['width']}×{info['height']} {len(info['sizes'])} 檔 {state}{flag}{low}{alpha}")
    save_manifest(manifest)


def referenced(manifest: dict) -> set[str]:
    return {t["file"] for p in entries(manifest).values() for t in p["sizes"].values()}


def stray_files(manifest: dict) -> list[Path]:
    refs = referenced(manifest)
    out = []
    for p in sorted([*ROOT.glob("*/*.webp"), *ROOT.glob(f"{REVIEWS}*/*.webp")]):
        if p.relative_to(ROOT).as_posix() not in refs:
            out.append(p)
    return out


def check(_: list[str]) -> None:
    manifest = load_manifest()
    bad = []
    for key, info in entries(manifest).items():
        for s, t in info["sizes"].items():
            f = ROOT / t["file"]
            if not f.exists():
                bad.append(f"{key} {s}：沒有 {t['file']}")
            elif f.stat().st_size != t["bytes"] or sha256(f) != t["sha256"]:
                bad.append(f"{key} {s}：{t['file']} 和 manifest 不符")
    stray = stray_files(manifest)
    count = {s: (len(manifest[s]), sum(len(p["sizes"]) for p in manifest[s].values())) for s in ("photos", "reviews")}
    print(
        f"manifest 照片 {count['photos'][0]} 張 {count['photos'][1]} 檔、文章圖 {count['reviews'][0]} 張 {count['reviews'][1]} 檔；"
        f"不符 {len(bad)}；沒引用的舊檔 {len(stray)}"
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


def exif_fields(src: Path, key: str) -> dict:
    """讀得到才填，不猜。exif-overrides.json 可以改或刪（值寫 null）某張的欄位，例如相機時鐘沒設的日期"""
    e = read_exif(src)
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
