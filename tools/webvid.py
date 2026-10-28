#!/usr/bin/env python3
"""影片與 Live Photo 動態部分的 MP4 出片（ffmpeg／ffprobe）。webimg.py 遇到影片原檔才載入這個檔。

  - 長邊 1280 / 1920 兩檔 MP4（原片較小就停在原片尺寸，不放大；寬高取偶數）
  - H.264 High、yuv420p、CRF 22、preset slow；maxrate 1280 檔 3M、1920 檔 6M（bufsize 兩倍）；+faststart
  - 依旋轉資訊轉正（ffmpeg autorotate），輸出不帶旋轉標記
  - -map_metadata -1 -map_chapters -1，只取一條畫面（和一條聲音）：GPS、裝置等中繼資料和 iPhone 的中繼資料軌都不進輸出
  - HDR（color_transfer 是 arib-std-b67 或 smpte2084）用 zscale + tonemap（hable）轉成 BT.709 SDR
  - 聲音：影片保留（AAC 128k 立體聲），Live Photo 一律去掉
  - 超過 15 秒拒絕；任一輸出超過 20 MB 就用更低碼率重做一次，仍超過就失敗
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from functools import cache
from pathlib import Path

VIDEO_EXTS = {".mov", ".mp4", ".m4v"}
TIERS = (1280, 1920)
CRF = 22
PRESET = "slow"
MAXRATE = {1280: 3, 1920: 6}  # Mbit/s；bufsize 兩倍
RETRY_CRF = 26  # 超過 20 MB 時重做一次：CRF 26、maxrate 減半
AUDIO_BITRATE = "128k"
MAX_SECONDS = 15
MAX_BYTES = 20_000_000
HDR_TRANSFERS = {"arib-std-b67", "smpte2084"}
THREADS = 4  # x264 的結果和執行緒數有關，固定下來，本機和 Actions 重出逐位元組相同
# 改了上面任何一項（或 ffmpeg 指令）就改這個字串：只有影片和 Live Photo 的版本跟著變，照片不受影響。
VIDEO_RECIPE = (
    f"video-v1 mp4 sizes={','.join(map(str, TIERS))} h264 high yuv420p crf={CRF} preset={PRESET} "
    f"maxrate={','.join(f'{t}:{r}M' for t, r in MAXRATE.items())} bufsize=2x faststart threads={THREADS} "
    f"strip hdr=zscale-hable-bt709 audio=aac{AUDIO_BITRATE} retry=crf{RETRY_CRF}-half cover=first-frame"
)


class VideoError(RuntimeError):
    """單一影片出片失敗（webimg.py 只讓這一張失敗，其他照常處理）。

    code 給後台看（build-failures.json）：too-long、too-large、hdr、unreadable、ffmpeg、tool；
    duration 只有 too-long 才有（秒）。"""

    def __init__(self, message: str, code: str = "ffmpeg", duration: float | None = None):
        super().__init__(message)
        self.code = code
        self.duration = duration


@cache
def binary(name: str) -> str:
    """優先用 Homebrew 的 ffmpeg-full（keg-only，有 zscale）；JRFANK_FFMPEG_DIR 可以指定別的目錄"""
    for d in (os.environ.get("JRFANK_FFMPEG_DIR"), "/opt/homebrew/opt/ffmpeg-full/bin", "/usr/local/opt/ffmpeg-full/bin"):
        if d and (Path(d) / name).is_file():
            return str(Path(d) / name)
    found = shutil.which(name)
    if not found:
        raise VideoError(f"找不到 {name}，影片無法出片（brew install ffmpeg-full）", "tool")
    return found


@cache
def has_filter(name: str) -> bool:
    out = subprocess.run([binary("ffmpeg"), "-hide_banner", "-filters"], capture_output=True, text=True).stdout
    return any(line.split()[1:2] == [name] for line in out.splitlines() if line.strip())


def ffprobe(src: Path) -> dict:
    try:
        out = subprocess.run(
            [binary("ffprobe"), "-v", "error", "-show_format", "-show_streams", "-of", "json", str(src)],
            check=True, capture_output=True, text=True,
        ).stdout
    except subprocess.CalledProcessError as exc:
        raise VideoError(f"{src.name}：ffprobe 讀不了這個影片（{exc.stderr.strip()[-200:]}）", "unreadable") from None
    return json.loads(out)


@dataclass
class Meta:
    duration: float
    width: int  # 轉正後
    height: int
    transfer: str
    matrix: str
    full_range: bool
    audio: int | None  # 要保留的聲音軌 index
    tags: dict

    @property
    def hdr(self) -> bool:
        return self.transfer in HDR_TRANSFERS


def probe(src: Path) -> Meta:
    data = ffprobe(src)
    videos = [s for s in data.get("streams", []) if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")]
    if not videos:
        raise VideoError(f"{src.name}：找不到畫面軌", "unreadable")
    v = videos[0]
    try:
        duration = float(data.get("format", {}).get("duration") or v.get("duration"))
    except (TypeError, ValueError):
        raise VideoError(f"{src.name}：讀不到影片長度", "unreadable") from None
    if duration > MAX_SECONDS:
        raise VideoError(f"{src.name}：影片長 {duration:.1f} 秒，超過 {MAX_SECONDS} 秒上限，這一張不出片（請先剪短再上傳）", "too-long", round(duration, 1))
    rotation = 0
    for side in v.get("side_data_list", []):
        if "rotation" in side:
            rotation = int(float(side["rotation"]))
    if not rotation and v.get("tags", {}).get("rotate"):
        rotation = int(v["tags"]["rotate"])
    w, h = int(v["width"]), int(v["height"])
    if rotation % 180:
        w, h = h, w
    audios = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
    # iPhone 可能同時有空間音訊軌和 AAC 立體聲軌：先挑常見編碼
    common = [s for s in audios if s.get("codec_name") in {"aac", "alac", "mp3", "opus"} or s.get("codec_name", "").startswith("pcm_")]
    audio = (common or audios or [None])[0]
    return Meta(
        duration=duration, width=w, height=h,
        transfer=v.get("color_transfer", ""), matrix=v.get("color_space", ""),
        full_range=v.get("color_range") == "pc" or v.get("pix_fmt", "").startswith("yuvj"),
        audio=int(audio["index"]) if audio else None,
        tags={k.lower(): str(val) for k, val in data.get("format", {}).get("tags", {}).items()},
    )


def exif_tags(meta: Meta) -> dict[str, str]:
    """只取相機型號和拍攝日期，交給 webimg.exif_fields 用照片同一套格式整理"""
    t = meta.tags
    out = {}
    make = t.get("com.apple.quicktime.make") or t.get("make") or t.get("com.android.manufacturer")
    model = t.get("com.apple.quicktime.model") or t.get("model") or t.get("com.android.model")
    if make:
        out["Make"] = make
    if model:
        out["Model"] = model
    # creationdate 是拍攝地的當地時間；creation_time 是 UTC，只在沒有前者時用
    date = t.get("com.apple.quicktime.creationdate") or t.get("creation_time")
    if date and len(date) >= 10 and date[:4].isdigit() and not date.startswith("1970") and not date.startswith("1904"):
        out["DateTimeOriginal"] = date[:10]
    return out


def tiers(meta: Meta) -> list[tuple[int, int, int]]:
    """[(檔, 寬, 高)]：比原片小的全做，再加一個停在原片尺寸的；不放大，寬高取偶數"""
    long = max(meta.width, meta.height)
    out = []
    for t in TIERS:
        scale = min(1.0, t / long)
        w = max(2, round(meta.width * scale / 2) * 2)
        h = max(2, round(meta.height * scale / 2) * 2)
        out.append((t, w, h))
        if t >= long:
            break
    return out


ZSCALE_MATRIX = {"bt2020nc": "2020_ncl", "bt2020c": "2020_cl", "bt709": "709", "smpte170m": "170m", "bt470bg": "470bg"}
SWS_MATRIX = {"bt2020nc": "bt2020", "bt2020c": "bt2020", "bt709": "bt709", "smpte170m": "bt601", "bt470bg": "bt601"}


def color_chain(meta: Meta, rgb: bool) -> str:
    """轉成 BT.709 SDR：rgb=False 給 MP4（yuv420p 有限範圍），rgb=True 給封面（16-bit RGB）"""
    rin = "full" if meta.full_range else "limited"
    if meta.hdr:
        if not has_filter("zscale"):
            raise VideoError(f"這台機器的 ffmpeg（{binary('ffmpeg')}）沒有 zscale 濾鏡，HDR 影片無法轉 SDR（brew install ffmpeg-full）", "hdr")
        chain = (
            f"zscale=tin={meta.transfer}:min={ZSCALE_MATRIX.get(meta.matrix, '2020_ncl')}:pin=2020:rin={rin}"
            ":t=linear:npl=100,format=gbrpf32le,zscale=p=709,tonemap=tonemap=hable:desat=0,"
        )
        return chain + ("zscale=t=709,format=rgb48be" if rgb else "zscale=t=709:m=709:r=limited,format=yuv420p")
    m = SWS_MATRIX.get(meta.matrix, "bt709")
    srange = "pc" if meta.full_range else "tv"
    if rgb:
        return f"scale=in_color_matrix={m}:in_range={srange},format=rgb48be"
    return f"scale=in_color_matrix={m}:out_color_matrix=bt709:in_range={srange}:out_range=tv,format=yuv420p"


def run_ffmpeg(args: list[str], what: str, code: str = "ffmpeg") -> None:
    try:
        subprocess.run([binary("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *args],
                       check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        raise VideoError(f"{what}：ffmpeg 失敗（{exc.stderr.strip()[-300:]}）", code) from None


def cover(src: Path, meta: Meta, dst: Path) -> None:
    """第一格畫面（轉正、HDR 轉 SDR）存成 16-bit PPM（沒有任何中繼資料），交給 webimg 的 WebP 流程"""
    run_ffmpeg(["-i", str(src), "-map", "0:v:0", "-frames:v", "1", "-vf", color_chain(meta, rgb=True),
                "-map_metadata", "-1", "-c:v", "ppm", "-f", "image2", str(dst)], f"{src.name} 取封面",
               "hdr" if meta.hdr else "ffmpeg")


def encode(src: Path, meta: Meta, w: int, h: int, maxrate: float, crf: int, audio: bool, dst: Path) -> None:
    rate = f"{maxrate:g}M"
    buf = f"{maxrate * 2:g}M"
    args = ["-i", str(src), "-map", "0:v:0"]
    if audio:
        args += ["-map", f"0:{meta.audio}", "-c:a", "aac", "-b:a", AUDIO_BITRATE, "-ac", "2"]
    else:
        args += ["-an"]
    args += [
        "-map_metadata", "-1", "-map_chapters", "-1", "-dn", "-sn",
        "-vf", f"scale={w}:{h}:flags=lanczos,{color_chain(meta, rgb=False)},setsar=1,"
               "setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv",
        "-c:v", "libx264", "-profile:v", "high", "-pix_fmt", "yuv420p", "-crf", str(crf), "-preset", PRESET,
        "-maxrate", rate, "-bufsize", buf, "-threads", str(THREADS),
        "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709", "-color_range", "tv",
        "-movflags", "+faststart", "-fflags", "+bitexact", "-flags:v", "+bitexact", "-flags:a", "+bitexact",
        "-f", "mp4", str(dst),
    ]
    # HDR 片的 ffmpeg 失敗多半在 zscale／tonemap 轉換那一段
    run_ffmpeg(args, f"{src.name} 出 {w}×{h}", "hdr" if meta.hdr else "ffmpeg")


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def render(src: Path, meta: Meta, out_dir: Path, key: str, version: str, audio: bool) -> dict:
    """出兩檔 MP4 到 out_dir/<key>-<檔>.<版本>.mp4，回傳 manifest 的 video／live 欄位"""
    keep_audio = audio and meta.audio is not None
    files = {}
    last = (0, 0)
    for tier, w, h in tiers(meta):
        file = f"{key}-{tier}.{version}.mp4"
        dst = out_dir / file
        dst.parent.mkdir(parents=True, exist_ok=True)
        encode(src, meta, w, h, MAXRATE[tier], CRF, keep_audio, dst)
        if dst.stat().st_size > MAX_BYTES:
            encode(src, meta, w, h, MAXRATE[tier] / 2, RETRY_CRF, keep_audio, dst)
            if dst.stat().st_size > MAX_BYTES:
                mb = dst.stat().st_size / 1e6
                raise VideoError(f"{src.name}：{tier} 檔降低碼率重做後仍有 {mb:.1f} MB，超過 {MAX_BYTES // 1_000_000} MB 上限，這一張不出片", "too-large")
        files[str(tier)] = {"file": file, "bytes": dst.stat().st_size, "sha256": sha256(dst)}
        last = (w, h)
    return {
        "duration": round(meta.duration, 3),
        "width": last[0],
        "height": last[1],
        "audio": keep_audio,
        "files": files,
    }
