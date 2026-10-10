#!/usr/bin/env python3
"""jrfank.cc 照片網頁檔產生器（只用標準庫 + ImageMagick 7 的 magick 指令）。

原圖放在 ~/Code/jrfank-site-assets/photo-originals/<系列>/<照片 id>.<副檔名>（不進任何網站倉庫），
這個工具把每張原圖做成三檔 WebP，放到本倉庫 <系列>/<照片 id>-<600|1200|2400>.webp：

  - 長邊 600 / 1200 / 2400 px；原圖不夠大就停在原圖尺寸，不放大
  - 有內嵌色彩描述檔（Display P3 等）就轉成 sRGB；沒有的當作 sRGB
  - 去掉全部 metadata（EXIF、GPS、XMP、ICC），網頁檔一律 sRGB
  - 按 EXIF 方向轉正

用法：
  python3 tools/webimg.py build            # 全部（已是最新的跳過）
  python3 tools/webimg.py build hk/hk-8    # 只做一張；--force 重做
  python3 tools/webimg.py exif hk/hk-8     # 印出 content/series/*.json 用的照片欄位（EXIF 讀得到才填）

build 之後會更新 manifest.json（每張的實際尺寸、位元組數、原圖 SHA-256）。
原圖目錄可用環境變數 JRFANK_ORIGINALS 改。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ORIGINALS = Path(os.environ.get("JRFANK_ORIGINALS", Path.home() / "Code/jrfank-site-assets/photo-originals"))
SRGB = "/System/Library/ColorSync/Profiles/sRGB Profile.icc"
SIZES = (600, 1200, 2400)
QUALITY = {600: 78, 1200: 80, 2400: 82}
EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".heic", ".webp"}


def magick(*args: str) -> str:
    return subprocess.run(["magick", *args], check=True, capture_output=True, text=True).stdout


def originals(only: list[str]) -> list[tuple[str, Path]]:
    out = []
    for p in sorted(ORIGINALS.glob("*/*")):
        if p.suffix.lower() not in EXTS:
            continue
        key = f"{p.parent.name}/{p.stem}"
        if not only or key in only:
            out.append((key, p))
    missing = set(only) - {k for k, _ in out}
    if missing:
        sys.exit(f"找不到原圖：{', '.join(sorted(missing))}（在 {ORIGINALS}）")
    return out


def has_icc(src: Path) -> bool:
    return bool(magick("identify", "-format", "%[profile:icc]", f"{src}[0]").strip())


def build_one(key: str, src: Path, force: bool) -> dict:
    icc = has_icc(src)
    outs = {}
    for size in SIZES:
        dst = ROOT / f"{key}-{size}.webp"
        dst.parent.mkdir(parents=True, exist_ok=True)
        if force or not dst.exists() or dst.stat().st_mtime < src.stat().st_mtime:
            args = [f"{src}[0]", "-auto-orient"]
            if icc:
                args += ["-profile", SRGB]
            args += [
                "-resize", f"{size}x{size}>",
                "-strip",
                "-quality", str(QUALITY[size]),
                "-define", "webp:method=6",
                str(dst),
            ]
            magick(*args)
        w, h = map(int, magick("identify", "-format", "%w %h", str(dst)).split())
        outs[str(size)] = [w, h, dst.stat().st_size]
    big = outs[str(SIZES[-1])]
    return {
        "width": big[0],
        "height": big[1],
        "sizes": outs,
        "colorConverted": icc,
        "source": src.name,
        "sourceSha256": hashlib.sha256(src.read_bytes()).hexdigest(),
    }


def build(args: list[str]) -> None:
    force = "--force" in args
    only = [a for a in args if not a.startswith("--")]
    items = originals(only)
    manifest_path = ROOT / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    with ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as pool:
        results = list(pool.map(lambda kv: (kv[0], build_one(kv[0], kv[1], force)), items))
    for key, info in results:
        manifest[key] = info
        print(f"{key}: {info['width']}×{info['height']}" + ("（已轉 sRGB）" if info["colorConverted"] else ""))
    manifest_path.write_text(json.dumps(dict(sorted(manifest.items())), ensure_ascii=False, indent=1) + "\n")


# ---------- EXIF → content/series/*.json 的欄位 ----------

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


def exif_fields(src: Path) -> dict:
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
    return out


def exif(args: list[str]) -> None:
    manifest = json.loads((ROOT / "manifest.json").read_text())
    res = {}
    for key, src in originals(args):
        info = manifest.get(key, {})
        w, h = map(int, magick("identify", "-format", "%w %h", f"{src}[0]").split())
        res[key] = {
            "file": key,
            "width": info.get("width"),
            "height": info.get("height"),
            "original": f"{w} × {h}",
            **exif_fields(src),
        }
    print(json.dumps(res, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    cmd, *rest = sys.argv[1:] or ["help"]
    {"build": build, "exif": exif}.get(cmd, lambda _: print(__doc__))(rest)
