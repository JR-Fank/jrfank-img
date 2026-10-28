#!/usr/bin/env python3
"""影片／Live Photo 出片：用 ffmpeg 現做測試片，在暫存倉庫跑真的 build、check、部署驗證的 prepare（不連 GitHub／R2）。"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import verify_photo_deployment as deploy  # noqa: E402
import webimg  # noqa: E402

webvid = webimg.webvid()
try:
    FFMPEG, FFPROBE = webvid.binary("ffmpeg"), webvid.binary("ffprobe")
except webvid.VideoError:
    FFMPEG = FFPROBE = None
GPS = "+22.2783+114.1747"


def ff(*args: str) -> None:
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def probe(path: Path) -> dict:
    out = subprocess.run([FFPROBE, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
                         check=True, capture_output=True, text=True).stdout
    return json.loads(out)


def clip(dst: Path, size: str, seconds: float, *, audio: bool = True, hdr: bool = False, extra: tuple = ()) -> None:
    args = ["-f", "lavfi", "-i", f"testsrc2=s={size}:r=30:d={seconds}"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=f=440:d={seconds}", "-c:a", "aac", "-shortest"]
    if hdr:
        # iPhone 的 HDR：HEVC 10-bit、BT.2020、HLG
        args += ["-c:v", "libx265", "-tag:v", "hvc1", "-pix_fmt", "yuv420p10le",
                 "-x265-params", "log-level=error:colorprim=bt2020:transfer=arib-std-b67:colormatrix=bt2020nc",
                 "-color_primaries", "bt2020", "-color_trc", "arib-std-b67", "-colorspace", "bt2020nc"]
    else:
        args += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast"]
    ff(*args, *extra, str(dst))


@unittest.skipUnless(FFMPEG and shutil.which("magick"), "需要 ffmpeg（建議 brew install ffmpeg-full）和 ImageMagick")
class WebvidBuildTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory(prefix="webvid-test-")
        cls.root = root = Path(cls.tmp.name) / "repo"
        src = root / "originals" / "vid"
        src.mkdir(parents=True)
        tmp = Path(cls.tmp.name)
        clip(src / "sdr.mp4", "1920x1080", 2)                                # 一般 SDR，兩檔
        clip(src / "small.mov", "640x360", 1, audio=False)                   # 比 1280 小：只有一檔、不放大
        clip(src / "hlg.mov", "1280x720", 2, hdr=True)                       # 10-bit HLG 標記
        clip(tmp / "landscape.mov", "1280x720", 1)
        ff("-display_rotation", "90", "-i", str(tmp / "landscape.mov"), "-c", "copy", str(src / "portrait.mov"))  # 直拍
        clip(src / "gps.mov", "1280x720", 1, extra=(                         # 帶 GPS、裝置資料
            "-metadata", f"location={GPS}/", "-metadata", f"com.apple.quicktime.location.ISO6709={GPS}+010.000/",
            "-metadata", "com.apple.quicktime.make=Apple", "-metadata", "com.apple.quicktime.model=iPhone 15 Pro",
            "-metadata", "com.apple.quicktime.creationdate=2026-10-01T18:22:11+0800",
            "-metadata", "com.apple.quicktime.software=18.0", "-movflags", "use_metadata_tags"))
        clip(src / "long.mov", "320x180", 20)                                # 20 秒：要被拒絕
        subprocess.run(["magick", "-size", "96x64", "gradient:#203040-#e0d0b0", str(src / "live.heic")], check=True)
        clip(src / "live.mov", "1280x720", 2)                                # 同 id 的 HEIC + MOV = Live Photo
        subprocess.run(["magick", "-size", "96x64", "gradient:#405060-#a0b0c0", str(src / "photo.png")], check=True)
        cls.gps_probe = probe(src / "gps.mov")

        cls.git("init", "-q", "-b", "main")
        cls.git("config", "user.name", "Fixture")
        cls.git("config", "user.email", "fixture@example.invalid")
        (root / "manifest.json").write_text(json.dumps({"version": 2, "recipe": webimg.RECIPE, "photos": {}, "reviews": {}}))
        cls.git("add", "manifest.json")
        cls.git("commit", "-qm", "base")
        cls.base = cls.git("rev-parse", "HEAD").strip()

        cls.patches = [
            patch.object(webimg, "ROOT", root),
            patch.object(webimg, "ORIGINALS", root / "originals"),
            patch.object(webimg, "MANIFEST", root / "manifest.json"),
            patch.object(webimg, "OVERRIDES", root / "exif-overrides.json"),
            patch.dict(os.environ, {"JRFANK_BUILD_FAILURES": str(tmp / "failures.txt")}),
        ]
        for p in cls.patches:
            p.start()
        cls.failures = tmp / "failures.txt"
        cls.build_output, cls.build_exit = cls.run_build([])
        cls.manifest = json.loads((root / "manifest.json").read_text())
        cls.photos = cls.manifest["photos"]

    @classmethod
    def tearDownClass(cls) -> None:
        for p in reversed(cls.patches):
            p.stop()
        cls.tmp.cleanup()

    @classmethod
    def git(cls, *args: str) -> str:
        return subprocess.check_output(["git", *args], cwd=cls.root, text=True, stderr=subprocess.DEVNULL)

    @staticmethod
    def run_build(args: list[str]) -> tuple[str, int]:
        out = io.StringIO()
        code = 0
        with redirect_stdout(out):
            try:
                webimg.build(args)
            except SystemExit as exc:
                code = exc.code
        return out.getvalue(), code

    def mp4s(self, key: str, part: str = "video") -> list[Path]:
        return [self.root / t["file"] for t in self.photos[key][part]["files"].values()]

    def assert_clean_h264(self, path: Path, audio: bool) -> dict:
        data = probe(path)
        v = [s for s in data["streams"] if s["codec_type"] == "video"]
        a = [s for s in data["streams"] if s["codec_type"] == "audio"]
        self.assertEqual(len(data["streams"]), 1 + int(audio), path.name)
        self.assertEqual((v[0]["codec_name"], v[0]["profile"], v[0]["pix_fmt"]), ("h264", "High", "yuv420p"))
        self.assertEqual((v[0].get("color_transfer"), v[0].get("color_space"), v[0].get("color_primaries")), ("bt709",) * 3)
        self.assertFalse(v[0].get("side_data_list"), "不能留旋轉標記")
        self.assertEqual(len(a), int(audio))
        if audio:
            self.assertEqual(a[0]["codec_name"], "aac")
            self.assertLessEqual(int(a[0]["bit_rate"]), 140_000)
        raw = path.read_bytes()
        self.assertLess(raw.find(b"moov"), raw.find(b"mdat"), "要 +faststart")
        return v[0]

    def test_sdr_two_tiers_and_no_upscale(self) -> None:
        info = self.photos["vid/sdr"]["video"]
        self.assertEqual(set(info["files"]), {"1280", "1920"})
        self.assertEqual((info["width"], info["height"], info["duration"], info["audio"]), (1920, 1080, 2.0, True))
        sizes = [(s["width"], s["height"]) for s in map(lambda p: self.assert_clean_h264(p, audio=True), self.mp4s("vid/sdr"))]
        self.assertEqual(sizes, [(1280, 720), (1920, 1080)])
        for name, t in info["files"].items():
            self.assertRegex(t["file"], rf"^vid/sdr-{name}\.[0-9a-f]{{10}}\.mp4$")
            self.assertEqual(t["sha256"], webimg.sha256(self.root / t["file"]))
        small = self.photos["vid/small"]["video"]
        self.assertEqual(set(small["files"]), {"1280"})
        self.assertEqual((small["width"], small["height"], small["audio"]), (640, 360, False))
        self.assert_clean_h264(self.mp4s("vid/small")[0], audio=False)

    def test_hlg_is_tone_mapped_to_bt709(self) -> None:
        self.assertEqual(self.photos["vid/hlg"]["colorConverted"], True)
        for path in self.mp4s("vid/hlg"):
            self.assert_clean_h264(path, audio=True)
        self.assertFalse(self.photos["vid/sdr"]["colorConverted"])

    def test_hdr_without_zscale_fails_clearly(self) -> None:
        meta = webvid.probe(self.root / "originals/vid/hlg.mov")
        self.assertTrue(meta.hdr)
        with patch.object(webvid, "has_filter", return_value=False):
            with self.assertRaisesRegex(webvid.VideoError, "沒有 zscale 濾鏡，HDR 影片無法轉 SDR"):
                webvid.color_chain(meta, rgb=False)

    def test_portrait_is_rotated_upright(self) -> None:
        entry = self.photos["vid/portrait"]
        self.assertEqual((entry["video"]["width"], entry["video"]["height"]), (720, 1280))
        self.assertEqual(entry["original"], {"width": 720, "height": 1280})
        self.assertLess(entry["width"], entry["height"])  # 封面 WebP 也是直的
        stream = self.assert_clean_h264(self.mp4s("vid/portrait")[0], audio=True)
        self.assertEqual((stream["width"], stream["height"]), (720, 1280))

    def test_gps_and_device_metadata_are_stripped(self) -> None:
        tags = self.gps_probe["format"]["tags"]
        self.assertIn(GPS, tags.get("location", ""))  # 測試片本身真的帶 GPS
        self.assertEqual(self.photos["vid/gps"]["exif"], {"camera": "iPhone 15 Pro", "date": "2026-10-01"})
        files = self.mp4s("vid/gps") + [self.root / t["file"] for t in self.photos["vid/gps"]["sizes"].values()]
        for path in files:
            raw = path.read_bytes()
            for needle in (b"22.2783", b"114.1747", b"iPhone", b"Apple", b"ISO6709", b"creationdate", b"Lavf"):
                self.assertNotIn(needle, raw, f"{path.name} 留了 {needle!r}")
            if path.suffix == ".mp4":
                data = probe(path)
                self.assertEqual(set(data["format"].get("tags", {})), {"major_brand", "minor_version", "compatible_brands"})
                for s in data["streams"]:
                    self.assertLessEqual(set(s.get("tags", {})), {"language", "handler_name", "vendor_id", "encoder"})

    def test_long_video_fails_alone(self) -> None:
        self.assertEqual(self.build_exit, 3)
        self.assertIn("vid/long: ✗ 失敗：long.mov：影片長 20.0 秒，超過 15 秒上限", self.build_output)
        self.assertNotIn("vid/long", self.photos)
        self.assertEqual(self.failures.read_text(), "vid/long\n")
        self.assertFalse(list(self.root.glob("vid/long-*")))
        self.assertIn("vid/photo", self.photos)  # 其他照片照常處理
        self.assertNotIn("vid/long", webimg.batch_keys())
        self.assertIn("vid/photo", webimg.batch_keys())

    def test_live_photo_keeps_still_and_drops_audio(self) -> None:
        entry = self.photos["vid/live"]
        self.assertNotIn("video", entry)
        self.assertEqual(entry["source"], "live.heic")
        self.assertEqual(entry["version"], webimg.photo_version(webimg.sha256(self.root / "originals/vid/live.heic")))
        live = entry["live"]
        self.assertEqual(live["source"], "live.mov")
        self.assertFalse(live["audio"])
        self.assertEqual((live["width"], live["height"]), (1280, 720))
        for path in self.mp4s("vid/live", "live"):
            self.assert_clean_h264(path, audio=False)
        self.assertEqual(set(entry["sizes"]), {"600"})  # 96×64 的靜態圖照舊出 WebP

    def test_swap_still_keeps_live(self) -> None:
        # 只換 Live Photo 的靜態圖：WebP 出新版本，影片不重出、live 原樣保留
        old = json.loads((self.root / "manifest.json").read_text())["photos"]["vid/live"]
        subprocess.run(["magick", "-size", "96x64", "gradient:#802020-#20a0f0", str(self.root / "originals/vid/live.heic")], check=True)
        with patch.object(webvid, "encode", side_effect=AssertionError("不該重新出片")):
            out, _ = self.run_build(["vid/live"])
        new = json.loads((self.root / "manifest.json").read_text())["photos"]["vid/live"]
        self.assertNotEqual(new["version"], old["version"])
        self.assertEqual(new["live"], old["live"])
        self.assertIn("Live Photo 2 秒、1 檔 MP4 沒變", out)

    def test_video_only_has_cover_webp(self) -> None:
        entry = self.photos["vid/sdr"]
        self.assertEqual(entry["source"], "sdr.mp4")
        self.assertEqual(entry["version"], webimg.video_version(entry["sourceSha256"]))
        self.assertEqual(set(entry["sizes"]), {"600", "1200", "1800", "2400"})
        self.assertEqual((entry["width"], entry["height"]), (1920, 1080))
        for t in entry["sizes"].values():
            self.assertTrue(t["file"].endswith(f".{entry['version']}.webp"))
            self.assertEqual(subprocess.check_output(["magick", "identify", "-format", "%m", str(self.root / t["file"])], text=True), "WEBP")
        self.assertTrue(entry["blur"].startswith("data:image/webp;base64,"))

    def test_rebuild_without_force_reuses_everything(self) -> None:
        before = {p: (webimg.sha256(p), p.stat().st_mtime_ns) for p in self.root.glob("vid/*.*")}
        with patch.object(webvid, "encode", side_effect=AssertionError("不該重新出片")):
            out, code = self.run_build([])
        self.assertEqual(code, 3)  # long 仍然失敗
        self.assertEqual(json.loads((self.root / "manifest.json").read_text()), self.manifest)
        self.assertEqual({p: (webimg.sha256(p), p.stat().st_mtime_ns) for p in self.root.glob("vid/*.*")}, before)
        self.assertNotIn("新版本", out)

    def test_check_covers_mp4_size_and_stray(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            webimg.check([])
        self.assertIn("MP4 ", out.getvalue())
        self.assertIn("不符 0；沒引用的舊檔 0", out.getvalue())
        stray = self.root / "vid/old-1280.0000000000.mp4"
        stray.write_bytes(b"old")
        try:
            self.assertIn(stray, webimg.stray_files(self.manifest))
            with patch.object(webimg, "MAX_OUTPUT_BYTES", 1000), redirect_stdout(io.StringIO()) as out2:
                with self.assertRaises(SystemExit):
                    webimg.check([])
            self.assertIn("超過單檔 0 MB 上限", out2.getvalue())
            with redirect_stdout(io.StringIO()):
                webimg.prune([])
            self.assertFalse(stray.exists())
        finally:
            stray.unlink(missing_ok=True)

    def test_oversize_retries_lower_bitrate_then_fails(self) -> None:
        meta = webvid.probe(self.root / "originals/vid/sdr.mp4")
        calls = []
        real = webvid.encode

        def spy(*args):
            calls.append(args[4:6])  # (maxrate, crf)
            return real(*args)

        with tempfile.TemporaryDirectory() as td, patch.object(webvid, "MAX_BYTES", 1000), patch.object(webvid, "encode", side_effect=spy):
            with self.assertRaisesRegex(webvid.VideoError, "降低碼率重做後仍有 .* MB，超過"):
                webvid.render(self.root / "originals/vid/sdr.mp4", meta, Path(td), "vid/x", "0" * 10, audio=True)
        self.assertEqual(calls, [(3, 22), (1.5, 26)])

    def test_deploy_prepare_includes_mp4(self) -> None:
        self.git("add", "-A")
        self.git("commit", "-qm", "build")
        snapshot = deploy.prepare(self.root, self.base, ["vid/sdr", "vid/live"])
        names = set(snapshot["assets"])
        for key, part in (("vid/sdr", "video"), ("vid/live", "live"), ("vid/hlg", "video")):
            for t in self.photos[key][part]["files"].values():
                self.assertIn(t["file"], names)  # hlg 不在 keys 裡，但是建置期間新增的 MP4 也要驗
        remote = json.loads(json.dumps(self.manifest))
        remote["photos"]["vid/live"]["live"]["files"]["1280"]["bytes"] += 1
        with self.assertRaisesRegex(deploy.NotReady, "vid/live"):
            deploy.check_manifest(snapshot, remote, deploy.photo_versions(remote))


if __name__ == "__main__":
    unittest.main()
