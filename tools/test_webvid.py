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
from types import SimpleNamespace
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


def clip(dst: Path, size: str, seconds: float, *, audio: bool = True, hdr: bool = False,
         fps: int = 30, extra: tuple = ()) -> None:
    args = ["-f", "lavfi", "-i", f"testsrc2=s={size}:r={fps}:d={seconds}"]
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


class WebvidIsolationTest(unittest.TestCase):
    def test_bad_probe_data_fails_per_item_and_still_writes_results(self) -> None:
        """幾種壞 ffprobe 輸出和未分類例外同批出現時，正常照片仍完成，報告與 manifest 仍落盤。"""
        with tempfile.TemporaryDirectory(prefix="webvid-isolation-test-") as td:
            root = Path(td) / "repo"
            originals = root / "originals" / "batch"
            originals.mkdir(parents=True)
            bad = {
                "bad-json": "{",
                "missing-size": json.dumps({
                    "streams": [{"codec_type": "video", "height": 720}],
                    "format": {"duration": "1"},
                }),
                "bad-rotation": json.dumps({
                    "streams": [{"codec_type": "video", "width": 1280, "height": 720,
                                 "side_data_list": [{"rotation": "sideways"}]}],
                    "format": {"duration": "1"},
                }),
                "bad-size": json.dumps({
                    "streams": [{"codec_type": "video", "width": 0, "height": 720}],
                    "format": {"duration": "1"},
                }),
            }
            for name in (*bad, "unexpected"):
                (originals / f"{name}.mov").write_bytes(name.encode())
            (originals / "good.png").write_bytes(b"good")
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps({"version": 2, "recipe": webimg.RECIPE, "photos": {}, "reviews": {}}))
            failures = Path(td) / "failures.txt"
            failures_json = Path(td) / "failures.json"

            def fake_probe_run(args, **_kwargs):
                name = Path(args[-1]).stem
                return SimpleNamespace(stdout=bad[name], stderr="", returncode=0)

            def fake_photo(key, src, src_sha, **_kwargs):
                return {
                    "version": webimg.photo_version(src_sha), "width": 1, "height": 1, "sizes": {},
                    "color": "#000000", "blur": "", "original": {"width": 1, "height": 1},
                    "lowRes": True, "exif": {}, "colorConverted": False,
                    "source": src.name, "sourceSha256": src_sha,
                }

            real_build_video = webimg.build_video

            def build_video_with_unexpected(key, src, src_sha, *args, **kwargs):
                if key == "batch/unexpected":
                    raise ValueError("測試用意外解析錯誤")
                return real_build_video(key, src, src_sha, *args, **kwargs)

            out = io.StringIO()
            with (
                patch.object(webimg, "ROOT", root),
                patch.object(webimg, "ORIGINALS", root / "originals"),
                patch.object(webimg, "MANIFEST", manifest_path),
                patch.object(webimg, "OVERRIDES", root / "exif-overrides.json"),
                patch.object(webimg, "build_one", side_effect=fake_photo),
                patch.object(webimg, "build_video", side_effect=build_video_with_unexpected),
                patch.object(webvid, "binary", return_value="ffprobe"),
                patch.object(webvid.subprocess, "run", side_effect=fake_probe_run),
                patch.dict(os.environ, {
                    "JRFANK_BUILD_FAILURES": str(failures),
                    "JRFANK_BUILD_FAILURES_JSON": str(failures_json),
                }),
                redirect_stdout(out),
            ):
                with self.assertRaises(SystemExit) as stopped:
                    webimg.build([])

            self.assertEqual(stopped.exception.code, 3)
            saved = json.loads(manifest_path.read_text())
            self.assertIn("batch/good", saved["photos"])
            for name in (*bad, "unexpected"):
                self.assertNotIn(f"batch/{name}", saved["photos"])

            report = json.loads(failures_json.read_text())
            self.assertEqual(report["schema"], 1)
            self.assertEqual([f["key"] for f in report["failures"]], [
                "batch/bad-json", "batch/bad-rotation", "batch/bad-size", "batch/missing-size", "batch/unexpected",
            ])
            messages = {f["key"]: f["message"] for f in report["failures"]}
            self.assertIn("ffprobe 輸出不是有效的 JSON", messages["batch/bad-json"])
            self.assertIn("ffprobe 缺少有效的影片尺寸", messages["batch/missing-size"])
            self.assertIn("ffprobe 的旋轉資訊不合法", messages["batch/bad-rotation"])
            self.assertIn("ffprobe 的影片尺寸不合法", messages["batch/bad-size"])
            self.assertIn("處理這一張時發生未分類錯誤", messages["batch/unexpected"])
            self.assertEqual(failures.read_text().splitlines(), [
                "batch/bad-json", "batch/bad-rotation", "batch/bad-size", "batch/missing-size", "batch/unexpected",
            ])
            self.assertIn("batch/good: 1×1 0 檔", out.getvalue())
            self.assertIn("5 張失敗（其他照片已照常處理、manifest 已更新）", out.getvalue())


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
        clip(src / "25s.mp4", "3840x2160", 25, fps=1)                        # 25 秒 4K：只出品質足夠的檔
        clip(src / "long.mov", "320x180", 31, fps=1)                         # 超過 30 秒：要被拒絕
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
            patch.dict(os.environ, {"JRFANK_BUILD_FAILURES": str(tmp / "failures.txt"), "JRFANK_BUILD_FAILURES_JSON": str(tmp / "failures.json")}),
        ]
        for p in cls.patches:
            p.start()
        cls.failures = tmp / "failures.txt"
        cls.failures_json = tmp / "failures.json"
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

    def test_lazy_load_without_prior_import(self) -> None:
        # build 只透過 webimg.webvid() 載入（不先 import webvid）：這條路在 Actions 上才會走到
        code = "import sys; sys.path.insert(0, sys.argv[1]); import webimg; m = webimg.webvid(); assert m.Meta and sys.modules['webvid'] is m; print('ok')"
        out = subprocess.run([sys.executable, "-c", code, str(Path(webimg.__file__).parent)], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr[-500:])
        self.assertEqual(out.stdout.strip(), "ok")

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

    def test_high_tiers_and_original_too_small(self) -> None:
        base = dict(duration=2, transfer="", matrix="bt709", full_range=False, audio=None, tags={})
        full = webvid.Meta(width=3840, height=2160, **base)
        self.assertEqual(webvid.tiers(full), [
            (1280, 1280, 720), (1920, 1920, 1080), (2560, 2560, 1440), (3840, 3840, 2160),
        ])
        between = webvid.Meta(width=3000, height=1688, **base)
        self.assertEqual([tier for tier, _w, _h in webvid.tiers(between)], [1280, 1920, 2560])
        too_small = webvid.Meta(width=2558, height=1438, **base)
        self.assertEqual([tier for tier, _w, _h in webvid.tiers(too_small)], [1280, 1920])

    def test_supplement_high_tiers_keeps_existing_low_files(self) -> None:
        src = self.root / "originals/vid/sdr.mp4"
        meta = webvid.Meta(duration=2, width=3840, height=2160, transfer="", matrix="bt709",
                            full_range=False, audio=None, tags={})
        with tempfile.TemporaryDirectory() as root_dir, tempfile.TemporaryDirectory() as out_dir:
            root = Path(root_dir)
            low = {}
            for tier in (1280, 1920):
                file = f"vid/x-{tier}.lowversion.mp4"
                path = root / file
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(f"old-{tier}".encode())
                low[str(tier)] = {"file": file, "bytes": path.stat().st_size, "sha256": webvid.sha256(path)}
            calls = []

            def fake_encode(_src, _meta, w, _h, maxrate, crf, _audio, dst):
                calls.append((w, maxrate, crf))
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(f"new-{w}".encode())

            versions = {1280: "lowversion", 1920: "lowversion", 2560: "hiversion", 3840: "hiversion"}
            with patch.object(webvid, "encode", side_effect=fake_encode):
                info = webvid.render(src, meta, Path(out_dir), "vid/x", versions, audio=False,
                                     existing={"files": low, "omitted": [3840], "omittedRecipe": "stale00000"},
                                     existing_root=root)
            self.assertEqual(info["files"]["1280"], low["1280"])
            self.assertEqual(info["files"]["1920"], low["1920"])
            self.assertEqual(calls, [(2560, 10, 22), (3840, 16, 22)])

    def test_omitted_high_tier_is_bound_to_policy(self) -> None:
        meta = webvid.Meta(duration=2, width=3840, height=2160, transfer="", matrix="bt709",
                            full_range=False, audio=None, tags={})
        versions = webimg.motion_versions("a" * 64, meta, live=False)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            files = {}
            for tier in (1280, 1920):
                file = f"vid/x-{tier}.{versions[tier]}.mp4"
                path = root / file
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"old")
                files[str(tier)] = {"file": file, "bytes": 3, "sha256": webvid.sha256(path)}
            part = {"files": files, "omitted": [2560, 3840], "omittedRecipe": webvid.OMISSION_POLICY_ID}
            with patch.object(webimg, "ROOT", root):
                self.assertTrue(webimg.motion_up_to_date(part, versions))
                self.assertFalse(webimg.motion_up_to_date({**part, "omittedRecipe": "stale00000"}, versions))
                self.assertFalse(webimg.motion_up_to_date({**part, "omitted": [1280, 2560, 3840]}, versions))

    def test_hlg_is_tone_mapped_to_bt709(self) -> None:
        self.assertEqual(self.photos["vid/hlg"]["colorConverted"], True)
        for path in self.mp4s("vid/hlg"):
            self.assert_clean_h264(path, audio=True)
        self.assertFalse(self.photos["vid/sdr"]["colorConverted"])

    def test_hdr_without_zscale_fails_clearly(self) -> None:
        meta = webvid.probe(self.root / "originals/vid/hlg.mov")
        self.assertTrue(meta.hdr)
        with patch.object(webvid, "has_filter", return_value=False):
            with self.assertRaisesRegex(webvid.VideoError, "沒有 zscale 濾鏡，HDR 影片無法轉 SDR") as ctx:
                webvid.color_chain(meta, rgb=False)
        self.assertEqual(ctx.exception.code, "hdr")
        # HDR 片在 ffmpeg 轉換那一步失敗也算 hdr；SDR 片是一般的 ffmpeg 失敗
        sdr = webvid.probe(self.root / "originals/vid/sdr.mp4")
        with patch.object(webvid.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "ffmpeg", stderr="zscale: boom")):
            with self.assertRaises(webvid.VideoError) as hdr_fail:
                webvid.encode(self.root / "originals/vid/hlg.mov", meta, 640, 360, 3, 22, False, Path("/dev/null"))
            with self.assertRaises(webvid.VideoError) as sdr_fail:
                webvid.encode(self.root / "originals/vid/sdr.mp4", sdr, 640, 360, 3, 22, False, Path("/dev/null"))
        self.assertEqual((hdr_fail.exception.code, sdr_fail.exception.code), ("hdr", "ffmpeg"))

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
        self.assertIn("vid/long: ✗ 失敗：long.mov：影片長 31.0 秒，超過 30 秒上限", self.build_output)
        self.assertNotIn("vid/long", self.photos)
        self.assertEqual(self.failures.read_text(), "vid/long\n")
        # 後台讀的失敗報告（photo-build-failures artifact）：照片鍵、種類、原因代碼、秒數、原訊息
        report = json.loads(self.failures_json.read_text())
        self.assertEqual(report["schema"], 1)
        self.assertEqual(len(report["failures"]), 1)
        f = report["failures"][0]
        self.assertEqual((f["key"], f["kind"], f["code"], f["duration"]), ("vid/long", "video", "too-long", 31.0))
        self.assertIn("超過 30 秒上限", f["message"])
        self.assertFalse(list(self.root.glob("vid/long-*")))
        self.assertIn("vid/photo", self.photos)  # 其他照片照常處理
        self.assertNotIn("vid/long", webimg.batch_keys())
        self.assertIn("vid/photo", webimg.batch_keys())

    def test_25_second_video_omits_tiers_below_minimum_bitrate(self) -> None:
        info = self.photos["vid/25s"]["video"]
        self.assertEqual((info["duration"], info["width"], info["height"]), (25.0, 1920, 1080))
        self.assertEqual(set(info["files"]), {"1280", "1920"})
        self.assertEqual(info["omitted"], [2560, 3840])
        self.assertEqual(info["omittedRecipe"], webvid.OMISSION_POLICY_ID)
        self.assertIn("2560 檔要壓在 20 MB 內", self.build_output)
        self.assertIn("3840 檔要壓在 20 MB 內", self.build_output)
        for path in self.mp4s("vid/25s"):
            self.assertLessEqual(path.stat().st_size, webvid.MAX_BYTES)
            self.assert_clean_h264(path, audio=True)

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

    def test_oversize_retries_by_duration_and_skips_only_that_tier(self) -> None:
        src = self.root / "originals/vid/sdr.mp4"
        meta = webvid.Meta(duration=15, width=3840, height=2160, transfer="", matrix="bt709",
                            full_range=False, audio=1, tags={})
        calls = []

        def fake_encode(_src, _meta, w, _h, maxrate, crf, _audio, dst):
            calls.append((w, maxrate, crf))
            # 只有 3840 檔持續超標；其他檔一次成功。
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(b"x")
            with dst.open("r+b") as handle:
                handle.truncate(webvid.MAX_BYTES + 1 if w == 3840 else 10)

        with (
            tempfile.TemporaryDirectory() as td,
            patch.object(webvid, "encode", side_effect=fake_encode),
            redirect_stdout(io.StringIO()) as out,
        ):
            info = webvid.render(src, meta, Path(td), "vid/x", "0" * 10, audio=True)
        self.assertEqual(set(info["files"]), {"1280", "1920", "2560"})
        self.assertEqual(info["omitted"], [3840])
        self.assertEqual(info["omittedRecipe"], webvid.OMISSION_POLICY_ID)
        self.assertIn("略過這一檔", out.getvalue())
        retry = [call for call in calls if call[0] == 3840]
        self.assertEqual(len(retry), 2)
        self.assertEqual(retry[0], (3840, 16, 22))
        expected = webvid.retry_maxrate(meta.duration, True, 16)
        self.assertEqual(retry[1], (3840, expected, 26))

    def test_legacy_tier_retries_with_duration_budget(self) -> None:
        src = self.root / "originals/vid/sdr.mp4"
        meta = webvid.Meta(duration=30, width=1920, height=1080, transfer="", matrix="bt709",
                            full_range=False, audio=1, tags={})
        calls = []

        def fake_encode(_src, _meta, w, _h, maxrate, crf, _audio, dst):
            calls.append((w, maxrate, crf))
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(b"x")
            size = 10 if crf == webvid.RETRY_CRF else (webvid.MAX_BYTES + 1 if w == 1920 else 10)
            with dst.open("r+b") as handle:
                handle.truncate(size)

        with tempfile.TemporaryDirectory() as td, patch.object(webvid, "encode", side_effect=fake_encode):
            info = webvid.render(src, meta, Path(td), "vid/x", "0" * 10, audio=True)
        self.assertEqual(set(info["files"]), {"1280", "1920"})
        expected = webvid.retry_maxrate(meta.duration, True, 6)
        self.assertLess(expected, 6)
        self.assertEqual([call for call in calls if call[0] == 1920], [(1920, 6, 22), (1920, expected, 26)])

    def test_duration_budget_skips_tiers_below_quality_floor(self) -> None:
        src = self.root / "originals/vid/sdr.mp4"
        meta = webvid.Meta(duration=25, width=3840, height=2160, transfer="", matrix="bt709",
                            full_range=False, audio=1, tags={})
        calls = []

        def fake_encode(_src, _meta, w, _h, maxrate, crf, _audio, dst):
            calls.append((w, maxrate, crf))
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(b"x")

        with tempfile.TemporaryDirectory() as td, patch.object(webvid, "encode", side_effect=fake_encode):
            info = webvid.render(src, meta, Path(td), "vid/x", "0" * 10, audio=True)
        self.assertEqual(webvid.MINRATE, {1280: 2, 1920: 3.5, 2560: 6, 3840: 10})
        self.assertEqual(set(info["files"]), {"1280", "1920"})
        self.assertEqual(info["omitted"], [2560, 3840])
        self.assertEqual([call[0] for call in calls], [1280, 1920])

    def test_1280_retries_at_floor_and_is_never_omitted(self) -> None:
        src = self.root / "originals/vid/sdr.mp4"
        meta = webvid.Meta(duration=30, width=3840, height=2160, transfer="", matrix="bt709",
                            full_range=False, audio=1, tags={})
        calls = []

        def fake_encode(_src, _meta, w, _h, maxrate, crf, _audio, dst):
            calls.append((w, maxrate, crf))
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(b"x")
            size = webvid.MAX_BYTES + 1 if w == 1280 and crf == webvid.CRF else 10
            with dst.open("r+b") as handle:
                handle.truncate(size)

        with tempfile.TemporaryDirectory() as td, patch.object(webvid, "encode", side_effect=fake_encode):
            info = webvid.render(src, meta, Path(td), "vid/x", "0" * 10, audio=True)
        self.assertIn("1280", info["files"])
        self.assertNotIn(1280, info["omitted"])
        self.assertEqual([call for call in calls if call[0] == 1280], [(1280, 3, 22), (1280, 2, 26)])

    def test_omission_policy_does_not_change_output_versions(self) -> None:
        meta = webvid.Meta(duration=25, width=3840, height=2160, transfer="", matrix="bt709",
                            full_range=False, audio=1, tags={})
        self.assertEqual(webimg.motion_versions("a" * 64, meta, live=False), {
            1280: "0f25c1c805", 1920: "0f25c1c805", 2560: "a6ca8e3608", 3840: "a6ca8e3608",
        })
        self.assertEqual(webimg.motion_versions("a" * 64, meta, live=True), {
            1280: "9ac17c4cd3", 1920: "9ac17c4cd3", 2560: "32998b8035", 3840: "32998b8035",
        })

    def test_duration_retry_can_fit_under_limit(self) -> None:
        src = self.root / "originals/vid/sdr.mp4"
        meta = webvid.Meta(duration=15, width=2560, height=1440, transfer="", matrix="bt709",
                            full_range=False, audio=1, tags={})
        calls = []

        def fake_encode(_src, _meta, w, _h, maxrate, crf, _audio, dst):
            calls.append((w, maxrate, crf))
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(b"x")
            size = 19_000_000 if crf == webvid.RETRY_CRF else (webvid.MAX_BYTES + 1 if w == 2560 else 10)
            with dst.open("r+b") as handle:
                handle.truncate(size)

        with tempfile.TemporaryDirectory() as td, patch.object(webvid, "encode", side_effect=fake_encode):
            info = webvid.render(src, meta, Path(td), "vid/x", "0" * 10, audio=True)
        self.assertEqual(set(info["files"]), {"1280", "1920", "2560"})
        self.assertNotIn("omitted", info)
        retry = [call for call in calls if call[0] == 2560]
        self.assertEqual(retry[0], (2560, 10, 22))
        self.assertEqual(retry[1], (2560, webvid.retry_maxrate(15, True, 10), 26))

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
