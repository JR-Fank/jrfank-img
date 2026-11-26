#!/usr/bin/env python3
"""影片與 Live Photo 動態部分的 MP4 出片（ffmpeg／ffprobe）。webimg.py 遇到影片原檔才載入這個檔。

  - 長邊 1280 / 1920 / 2560 / 3840 四檔 MP4（原片較小就停在原片尺寸，不放大；寬高取偶數）
  - H.264 High、yuv420p、CRF 22、preset slow；maxrate 3M / 6M / 10M / 16M（bufsize 兩倍）；+faststart
  - 依旋轉資訊轉正（ffmpeg autorotate），輸出不帶旋轉標記
  - -map_metadata -1 -map_chapters -1，只取一條畫面（和一條聲音）：GPS、裝置等中繼資料和 iPhone 的中繼資料軌都不進輸出
  - HDR（color_transfer 是 arib-std-b67 或 smpte2084）用 zscale + tonemap（hable）轉成 BT.709 SDR
  - 聲音：影片保留（AAC 128k 立體聲），Live Photo 一律去掉
  - 超過 30 秒拒絕；依片長壓進 20 MB 需要低於最低碼率時略過該檔（1280 一定保留）
"""
from __future__ import annotations

import hashlib
import json
import math
import mmap
import os
import shutil
import subprocess
from dataclasses import dataclass
from functools import cache
from pathlib import Path

VIDEO_EXTS = {".mov", ".mp4", ".m4v"}
TIERS = (1280, 1920, 2560, 3840)
LEGACY_TIERS = (1280, 1920)
CRF = 22
PRESET = "slow"
MAXRATE = {1280: 3, 1920: 6, 2560: 10, 3840: 16}  # Mbit/s；bufsize 兩倍
MINRATE = {1280: 2, 1920: 3.5, 2560: 6, 3840: 10}  # Mbit/s；低於這個品質下限就不出該檔
RETRY_CRF = 26
RETRY_SIZE_MARGIN = 0.95  # 容器、音訊和碼率控制誤差預留 5%
AUDIO_BITRATE = "128k"
MAX_SECONDS = 30
MAX_BYTES = 20_000_000
HDR_TRANSFERS = {"arib-std-b67", "smpte2084"}
THREADS = 4  # x264 的結果和執行緒數有關，固定下來，本機和 Actions 重出逐位元組相同
# 改了上面任何一項（或 ffmpeg 指令）就改這個字串：只有影片和 Live Photo 的版本跟著變，照片不受影響。
# 1280／1920 的既有檔名和位元組要保持不變；這個 v1 字串是凍結的舊版識別，
# 其中 retry=half 描述已上線配方，不隨下面獨立版本的略過／重試政策改動。
VIDEO_RECIPE = (
    f"video-v1 mp4 sizes={','.join(map(str, LEGACY_TIERS))} h264 high yuv420p crf={CRF} preset={PRESET} "
    f"maxrate={','.join(f'{t}:{MAXRATE[t]}M' for t in LEGACY_TIERS)} bufsize=2x faststart threads={THREADS} "
    f"strip hdr=zscale-hable-bt709 audio=aac{AUDIO_BITRATE} retry=crf{RETRY_CRF}-half cover=first-frame"
)
# 新增檔位另用一個版本配方，避免補檔時改寫已上線的 1280／1920 檔。
HI_TIER_RECIPE = (
    f"video-hi-v1 mp4 sizes=2560,3840 h264 high yuv420p crf={CRF} preset={PRESET} "
    f"maxrate=2560:{MAXRATE[2560]}M,3840:{MAXRATE[3840]}M bufsize=2x faststart threads={THREADS} "
    f"strip hdr=zscale-hable-bt709 audio=aac{AUDIO_BITRATE} "
    f"retry=crf{RETRY_CRF}-duration-budget-{RETRY_SIZE_MARGIN:g} cover=unchanged"
)
# 檔案版本配方保持不變，已上線影片才不會重出；略過策略用自己的版本，只補先前缺的檔。
OMISSION_POLICY = (
    f"video-omission-v1 max-bytes={MAX_BYTES} retry=crf{RETRY_CRF}-duration-budget-{RETRY_SIZE_MARGIN:g} "
    f"audio={AUDIO_BITRATE} "
    f"minrate={','.join(f'{t}:{MINRATE[t]:g}M' for t in TIERS)} 1280=required"
)
OMISSION_POLICY_ID = hashlib.sha256(OMISSION_POLICY.encode()).hexdigest()[:10]


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
        detail = (exc.stderr or "").strip()[-200:]
        suffix = f"（{detail}）" if detail else ""
        raise VideoError(f"{src.name}：ffprobe 讀不了這個影片{suffix}", "unreadable") from None
    except UnicodeError:
        raise VideoError(f"{src.name}：ffprobe 輸出含有無法解析的文字", "unreadable") from None
    try:
        data = json.loads(out)
    except (json.JSONDecodeError, TypeError):
        raise VideoError(f"{src.name}：ffprobe 輸出不是有效的 JSON", "unreadable") from None
    if not isinstance(data, dict):
        raise VideoError(f"{src.name}：ffprobe 輸出的資料結構不合法", "unreadable")
    return data


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

    def number(value, message: str) -> float:
        if isinstance(value, bool):
            raise VideoError(f"{src.name}：{message}", "unreadable")
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError):
            raise VideoError(f"{src.name}：{message}", "unreadable") from None
        if not math.isfinite(parsed):
            raise VideoError(f"{src.name}：{message}", "unreadable")
        return parsed

    def text(value, message: str) -> str:
        if value in (None, ""):
            return ""
        if not isinstance(value, str):
            raise VideoError(f"{src.name}：{message}", "unreadable")
        return value

    streams = data.get("streams")
    if not isinstance(streams, list) or any(not isinstance(s, dict) for s in streams):
        raise VideoError(f"{src.name}：ffprobe 缺少有效的軌道資料", "unreadable")
    videos = []
    for stream in streams:
        disposition = stream.get("disposition", {})
        if disposition is None:
            disposition = {}
        if not isinstance(disposition, dict):
            raise VideoError(f"{src.name}：ffprobe 的畫面軌資料不合法", "unreadable")
        attached_pic = disposition.get("attached_pic", 0)
        if not isinstance(attached_pic, (bool, int)):
            raise VideoError(f"{src.name}：ffprobe 的畫面軌資料不合法", "unreadable")
        if stream.get("codec_type") == "video" and not attached_pic:
            videos.append(stream)
    if not videos:
        raise VideoError(f"{src.name}：找不到畫面軌", "unreadable")
    v = videos[0]
    fmt = data.get("format", {})
    if fmt is None:
        fmt = {}
    if not isinstance(fmt, dict):
        raise VideoError(f"{src.name}：ffprobe 的影片格式資料不合法", "unreadable")
    duration = number(fmt.get("duration") or v.get("duration"), "讀不到影片長度")
    if duration <= 0:
        raise VideoError(f"{src.name}：影片長度不合法", "unreadable")
    if duration > MAX_SECONDS:
        raise VideoError(f"{src.name}：影片長 {duration:.1f} 秒，超過 {MAX_SECONDS} 秒上限，這一張不出片（請先剪短再上傳）", "too-long", round(duration, 1))

    def rotation_value(value) -> int:
        parsed = number(value, "ffprobe 的旋轉資訊不合法")
        if not parsed.is_integer() or int(parsed) % 90:
            raise VideoError(f"{src.name}：ffprobe 的旋轉資訊不合法", "unreadable")
        return int(parsed)

    rotation = 0
    side_data = v.get("side_data_list", [])
    if side_data is None:
        side_data = []
    if not isinstance(side_data, list) or any(not isinstance(side, dict) for side in side_data):
        raise VideoError(f"{src.name}：ffprobe 的旋轉資訊不合法", "unreadable")
    for side in side_data:
        if "rotation" in side:
            rotation = rotation_value(side["rotation"])
    video_tags = v.get("tags", {})
    if video_tags is None:
        video_tags = {}
    if not isinstance(video_tags, dict):
        raise VideoError(f"{src.name}：ffprobe 的畫面軌標籤不合法", "unreadable")
    if not rotation and video_tags.get("rotate") not in (None, ""):
        rotation = rotation_value(video_tags["rotate"])

    if v.get("width") in (None, "") or v.get("height") in (None, ""):
        raise VideoError(f"{src.name}：ffprobe 缺少有效的影片尺寸", "unreadable") from None
    raw_w = number(v.get("width"), "ffprobe 的影片尺寸不合法")
    raw_h = number(v.get("height"), "ffprobe 的影片尺寸不合法")
    if not raw_w.is_integer() or not raw_h.is_integer() or raw_w <= 0 or raw_h <= 0:
        raise VideoError(f"{src.name}：ffprobe 的影片尺寸不合法", "unreadable")
    w, h = int(raw_w), int(raw_h)
    if rotation % 180:
        w, h = h, w
    audios = [s for s in streams if s.get("codec_type") == "audio"]
    # iPhone 可能同時有空間音訊軌和 AAC 立體聲軌：先挑常見編碼
    codecs = [(s, text(s.get("codec_name"), "ffprobe 的聲音軌編碼不合法")) for s in audios]
    common = [s for s, codec in codecs if codec in {"aac", "alac", "mp3", "opus"} or codec.startswith("pcm_")]
    audio = (common or audios or [None])[0]
    try:
        raw_audio_index = number(audio["index"], "ffprobe 的聲音軌 index 不合法") if audio else None
    except KeyError:
        raise VideoError(f"{src.name}：ffprobe 的聲音軌 index 不合法", "unreadable") from None
    if raw_audio_index is not None and (not raw_audio_index.is_integer() or raw_audio_index < 0):
        raise VideoError(f"{src.name}：ffprobe 的聲音軌 index 不合法", "unreadable")
    audio_index = int(raw_audio_index) if raw_audio_index is not None else None
    format_tags = fmt.get("tags", {})
    if format_tags is None:
        format_tags = {}
    if not isinstance(format_tags, dict):
        raise VideoError(f"{src.name}：ffprobe 的影片標籤不合法", "unreadable")
    return Meta(
        duration=duration, width=w, height=h,
        transfer=text(v.get("color_transfer"), "ffprobe 的色彩轉換資料不合法"),
        matrix=text(v.get("color_space"), "ffprobe 的色彩空間資料不合法"),
        full_range=text(v.get("color_range"), "ffprobe 的色彩範圍資料不合法") == "pc"
                   or text(v.get("pix_fmt"), "ffprobe 的像素格式資料不合法").startswith("yuvj"),
        audio=audio_index,
        tags={str(k).lower(): str(val) for k, val in format_tags.items()},
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
    """[(檔, 寬, 高)]：舊檔沿用既有停在原片尺寸規則；新高檔只在原片夠大時才做。"""
    if meta.width <= 0 or meta.height <= 0:
        raise VideoError("影片尺寸不合法，這一張不出片", "unreadable")
    long = max(meta.width, meta.height)
    out = []
    for t in TIERS:
        if t not in LEGACY_TIERS and long < t:
            continue
        scale = min(1.0, t / long)
        w = max(2, round(meta.width * scale / 2) * 2)
        h = max(2, round(meta.height * scale / 2) * 2)
        out.append((t, w, h))
        if t in LEGACY_TIERS and t >= long:
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


COVER_MEAN_LUMA_FLOOR = 0.05  # 平均亮度 < 5% 且 P95 < 12% 視為黑幕淡入首格，往後找非黑畫面
COVER_P95_LUMA_FLOOR = 0.12
COVER_SETTLED_MEAN_LUMA = 0.18  # 探測格平均亮度 ≥ 18% 或 P95 ≥ 35% 視為淡入已完成，不必再往後找
COVER_SETTLED_P95_LUMA = 0.35


def ppm_luma_stats(path: Path) -> tuple[float, float] | None:
    """讀 binary PPM（P6，8/16-bit）的抽樣 Rec.709 亮度：(平均亮度, P95 亮度)，範圍 0..1；無法解析回傳 None。"""
    try:
        with path.open("rb") as fh:
            size = os.fstat(fh.fileno()).st_size
            if size <= 0:
                return None
            with mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as buf:
                n = len(buf)
                pos = 0
                tokens: list[bytes] = []
                while len(tokens) < 4 and pos < n:
                    while pos < n and buf[pos] in b" \t\r\n":
                        pos += 1
                    if pos < n and buf[pos] == ord("#"):
                        while pos < n and buf[pos] != ord("\n"):
                            pos += 1
                        continue
                    start = pos
                    while pos < n and buf[pos] not in b" \t\r\n#":
                        pos += 1
                    if pos > start:
                        tokens.append(buf[start:pos])
                if len(tokens) < 4 or tokens[0] != b"P6":
                    return None
                # maxval 與像素資料之間可能緊貼 # 註解，最後固定以單一空白位元組分隔
                while pos < n and buf[pos] == ord("#"):
                    while pos < n and buf[pos] != ord("\n"):
                        pos += 1
                if pos >= n or buf[pos] not in b" \t\r\n":
                    return None
                pos += 1
                try:
                    width, height, maxval = int(tokens[1]), int(tokens[2]), int(tokens[3])
                except ValueError:
                    return None
                if width <= 0 or height <= 0 or maxval <= 0:
                    return None
                bps = 2 if maxval > 255 else 1
                stride = 3 * bps
                total = min(width * height, (n - pos) // stride)
                if total <= 0:
                    return None
                step = max(1, total // 4096)
                samples: list[float] = []
                inv = 1.0 / maxval
                for idx in range(0, total, step):
                    off = pos + idx * stride
                    if bps == 2:
                        r = (buf[off] << 8) | buf[off + 1]
                        g = (buf[off + 2] << 8) | buf[off + 3]
                        b = (buf[off + 4] << 8) | buf[off + 5]
                    else:
                        r, g, b = buf[off], buf[off + 1], buf[off + 2]
                    samples.append((0.2126 * r + 0.7152 * g + 0.0722 * b) * inv)
                samples.sort()
                mean = sum(samples) / len(samples)
                p95 = samples[min(len(samples) - 1, int(len(samples) * 0.95))]
                return (mean, p95)
    except (OSError, ValueError):
        return None


def is_near_black_luma(mean: float, p95: float) -> bool:
    return mean < COVER_MEAN_LUMA_FLOOR and p95 < COVER_P95_LUMA_FLOOR


def cover_probe_times(duration: float) -> list[float]:
    """首格近黑時往後探測的秒數清單（不超過片尾前 0.1 秒，最多 3 個點）。"""
    if duration <= 0.2:
        return []
    limit = max(0.1, duration - 0.1)
    out: list[float] = []
    for raw in (0.5, 1.2, 2.5, round(duration * 0.35, 3), round(duration * 0.6, 3)):
        if 0.08 <= raw <= limit and all(abs(raw - prev) >= 0.15 for prev in out):
            out.append(raw)
            if len(out) >= 3:
                break
    return out


def extract_cover_frame(src: Path, meta: Meta, dst: Path, ss: float | None = None) -> None:
    seek = ["-ss", f"{ss:.3f}"] if ss is not None else []
    run_ffmpeg([*seek, "-i", str(src), "-map", "0:v:0", "-frames:v", "1", "-vf", color_chain(meta, rgb=True),
                "-map_metadata", "-1", "-c:v", "ppm", "-f", "image2", str(dst)], f"{src.name} 取封面",
               "hdr" if meta.hdr else "ffmpeg")


def cover(src: Path, meta: Meta, dst: Path) -> None:
    """取代表畫面（轉正、HDR 轉 SDR）存成 16-bit PPM，交給 webimg 的 WebP 流程。

    預設取第 0 格（既有非黑片逐位元組不變）；若第 0 格為黑幕淡入（平均與 P95 亮度皆極低），
    自動往後探測前幾秒挑選第一個非黑畫面，整支都暗則保留較亮的一格。"""
    extract_cover_frame(src, meta, dst)
    stats0 = ppm_luma_stats(dst)
    if stats0 is None:
        return
    m0, p0 = stats0
    if not is_near_black_luma(m0, p0):
        return
    probes = cover_probe_times(meta.duration)
    if not probes:
        return
    best_score = m0 + 0.5 * p0
    probe_dst = dst.with_name(f"{dst.stem}.probe{dst.suffix}")
    try:
        for t in probes:
            probe_dst.unlink(missing_ok=True)
            try:
                extract_cover_frame(src, meta, probe_dst, ss=t)
            except VideoError:
                continue
            if not probe_dst.is_file() or probe_dst.stat().st_size == 0:
                continue
            stats = ppm_luma_stats(probe_dst)
            if stats is None:
                continue
            mt, pt = stats
            score = mt + 0.5 * pt
            if score > best_score:
                best_score = score
                probe_dst.replace(dst)
            if mt >= COVER_SETTLED_MEAN_LUMA or pt >= COVER_SETTLED_P95_LUMA:
                return
    finally:
        probe_dst.unlink(missing_ok=True)


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


def retry_maxrate(duration: float, audio: bool, configured: float) -> float:
    """按片長把總輸出預算壓在 20 MB 內；音訊與容器開銷先從預算扣除。"""
    audio_mbps = 0.128 if audio else 0.0
    budget_mbps = MAX_BYTES * 8 * RETRY_SIZE_MARGIN / duration / 1_000_000
    return max(0.1, min(configured, budget_mbps - audio_mbps))


def render(
    src: Path,
    meta: Meta,
    out_dir: Path,
    key: str,
    versions: str | dict[int, str],
    audio: bool,
    existing: dict | None = None,
    existing_root: Path | None = None,
) -> dict:
    """出 MP4，回傳 manifest 的 video／live 欄位；可沿用已存在且版本相符的舊檔。"""
    keep_audio = audio and meta.audio is not None
    files = {}
    omitted = []
    last = (0, 0)
    for tier, w, h in tiers(meta):
        version = versions[tier] if isinstance(versions, dict) else versions
        file = f"{key}-{tier}.{version}.mp4"
        if (
            (existing or {}).get("omittedRecipe") == OMISSION_POLICY_ID
            and tier != 1280
            and tier in (existing or {}).get("omitted", [])
        ):
            omitted.append(tier)
            continue
        old = (existing or {}).get("files", {}).get(str(tier))
        old_path = existing_root / old["file"] if existing_root is not None and old else None
        if old and old.get("file") == file and old_path is not None and old_path.is_file():
            files[str(tier)] = old
            last = (w, h)
            continue
        retry_rate = retry_maxrate(meta.duration, keep_audio, MAXRATE[tier])
        if retry_rate < MINRATE[tier] and tier != 1280:
            print(f"{src.name}：{tier} 檔要壓在 {MAX_BYTES // 1_000_000} MB 內需降到 "
                  f"{retry_rate:g} Mbit/s，低於 {MINRATE[tier]:g} Mbit/s 下限，略過這一檔")
            omitted.append(tier)
            continue
        dst = out_dir / file
        dst.parent.mkdir(parents=True, exist_ok=True)
        encode(src, meta, w, h, MAXRATE[tier], CRF, keep_audio, dst)
        if dst.stat().st_size > MAX_BYTES:
            retry_rate = MINRATE[tier] if tier == 1280 else retry_rate
            encode(src, meta, w, h, retry_rate, RETRY_CRF, keep_audio, dst)
            if dst.stat().st_size > MAX_BYTES:
                mb = dst.stat().st_size / 1e6
                if tier == 1280:
                    raise VideoError(f"{src.name}：1280 檔用最低碼率重做後仍有 {mb:.1f} MB，超過 "
                                     f"{MAX_BYTES // 1_000_000} MB 上限，這一張不出片", "too-large")
                print(f"{src.name}：{tier} 檔降低碼率重做後仍有 {mb:.1f} MB，超過 "
                      f"{MAX_BYTES // 1_000_000} MB 上限，略過這一檔")
                dst.unlink(missing_ok=True)
                omitted.append(tier)
                continue
        files[str(tier)] = {"file": file, "bytes": dst.stat().st_size, "sha256": sha256(dst)}
        last = (w, h)
    return {
        "duration": round(meta.duration, 3),
        "width": last[0],
        "height": last[1],
        "audio": keep_audio,
        "files": files,
        **({"omitted": omitted} if omitted else {}),
        **({"omittedRecipe": OMISSION_POLICY_ID} if omitted else {}),
    }
