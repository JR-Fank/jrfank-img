#!/usr/bin/env python3
"""HEIC/HDR branches in webimg.py, plus one isolated real HEIC build."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("webimg", ROOT / "tools/webimg.py")
assert SPEC and SPEC.loader
webimg = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(webimg)


class WebimgHEICTest(unittest.TestCase):
    def test_pq_is_normalized_even_when_image_magick_reports_srgb(self) -> None:
        """The HDR decision must use ColorSync metadata, not IM's colorspace label."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "hdr.heic"
            src.touch()
            calls: list[tuple[str, ...]] = []
            profile_results: list[str] = []

            def fake_magick(*args: str) -> str:
                calls.append(args)
                if args[0] == "identify":
                    fmt = args[2]
                    if fmt == "%w %h":
                        return "8 12"
                    if fmt == "%[profile:icc]":
                        profile_results.append("sRGB")
                        return profile_results[-1]  # the misleading result under test
                    if fmt == "%A":
                        return "Undefined"
                    return ""
                if "-format" in args and "%w %h" in args:
                    return "8 12"
                if "info:" in args:
                    return "#112233"
                for i, arg in enumerate(args[:-1]):
                    if arg == "-write" and args[i + 1] != "mpr:o":
                        Path(args[i + 1]).write_bytes(b"webp fixture")
                if args[-1].endswith("lqip.webp"):
                    Path(args[-1]).write_bytes(b"lqip fixture")
                return ""

            def fake_sips_to_srgb(src_path: Path, dst: Path) -> None:
                dst.write_bytes(b"temporary srgb png")

            with (
                patch.object(webimg, "ROOT", root),
                patch.object(webimg, "OVERRIDES", root / "exif-overrides.json"),
                patch.object(webimg, "sips_property", return_value="Rec. ITU-R BT.2100 PQ"),
                patch.object(webimg, "magick", side_effect=fake_magick),
                patch.object(webimg, "sips_to_srgb", side_effect=fake_sips_to_srgb) as convert,
            ):
                info = webimg.build_one("fixture/hdr", src, "a" * 64)

            self.assertTrue(info["colorConverted"])
            convert.assert_called_once()
            self.assertEqual(convert.call_args.args[0], src)
            converted = convert.call_args.args[1]
            self.assertTrue(str(converted).endswith("input-srgb.png"))
            self.assertEqual(profile_results, ["sRGB"])
            render_calls = [args for args in calls if "mpr:o" in args]
            self.assertTrue(render_calls)
            self.assertTrue(any(any("input-srgb.png[0]" in arg for arg in args) for args in render_calls))

    def test_display_p3_does_not_use_sips_conversion(self) -> None:
        src = Path("p3.heic")
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            with (
                patch.object(webimg, "sips_property", return_value="Display P3"),
                patch.object(webimg, "sips_to_srgb") as convert,
            ):
                input_src, converted = webimg.prepare_input(src, tmp)

        self.assertFalse(converted)
        self.assertEqual(input_src, src)
        convert.assert_not_called()

    def test_non_heic_does_not_query_or_use_sips(self) -> None:
        src = Path("photo.jpg")
        with tempfile.TemporaryDirectory() as td:
            with (
                patch.object(webimg, "sips_property") as prop,
                patch.object(webimg, "sips_to_srgb") as convert,
            ):
                input_src, converted = webimg.prepare_input(src, Path(td))

        self.assertFalse(converted)
        self.assertEqual(input_src, src)
        prop.assert_not_called()
        convert.assert_not_called()

    def test_sips_failure_is_explicit(self) -> None:
        src = Path("hdr.heic")
        dst = Path("/tmp/input-srgb.png")
        failure = subprocess.CalledProcessError(1, ["sips"])
        with patch.object(webimg.subprocess, "run", side_effect=failure):
            with self.assertRaisesRegex(RuntimeError, "HDR HEIC 轉 sRGB 失敗"):
                webimg.sips_to_srgb(src, dst)
        with patch.object(webimg.subprocess, "run", side_effect=FileNotFoundError):
            with self.assertRaisesRegex(RuntimeError, "需要 macOS sips"):
                webimg.sips_to_srgb(src, dst)

    def test_orientation_uses_image_magick_auto_orient_dimensions(self) -> None:
        with patch.object(webimg, "magick", return_value="8 12") as identify:
            self.assertEqual((8, 12), webimg.oriented_dimensions(Path("portrait.heic")))
        identify.assert_called_once_with(
            "portrait.heic[0]", "-auto-orient", "-format", "%w %h", "info:"
        )

    @unittest.skipUnless(
        os.environ.get("WEBIMG_HEIC_ORIENTATION_SAMPLE"),
        "set WEBIMG_HEIC_ORIENTATION_SAMPLE to run the real iPhone HEIC orientation check",
    )
    def test_real_iphone_heic_orientation_sample(self) -> None:
        src = Path(os.environ["WEBIMG_HEIC_ORIENTATION_SAMPLE"])
        self.assertTrue(src.is_file(), src)
        raw = tuple(
            map(int, webimg.magick("identify", "-format", "%w %h", f"{src}[0]").split())
        )
        oriented = webimg.oriented_dimensions(src)
        self.assertEqual(raw, (3024, 4032))
        self.assertEqual(oriented, (3024, 4032))

    @unittest.skipUnless(shutil.which("magick") and shutil.which("sips"), "requires macOS ImageMagick and sips")
    def test_real_small_heic_build_never_upscales(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "repo"
            originals = root / "originals" / "fixture"
            source = originals / "tiny.heic"
            source.parent.mkdir(parents=True)
            subprocess.run(
                [
                    "magick",
                    "-size",
                    "8x12",
                    "gradient:#102030-#d0c0a0",
                    "-depth",
                    "8",
                    str(source),
                ],
                check=True,
                capture_output=True,
                text=True,
            )

            with (
                patch.object(webimg, "ROOT", root),
                patch.object(webimg, "ORIGINALS", root / "originals"),
                patch.object(webimg, "MANIFEST", root / "manifest.json"),
                patch.object(webimg, "OVERRIDES", root / "exif-overrides.json"),
            ):
                webimg.build(["fixture/tiny"])
                webimg.check([])

            manifest = json.loads((root / "manifest.json").read_text())
            info = manifest["photos"]["fixture/tiny"]
            self.assertEqual(info["original"], {"width": 8, "height": 12})
            self.assertFalse(info["colorConverted"])
            self.assertEqual(set(info["sizes"]), {"600"})
            for tier in info["sizes"].values():
                self.assertLessEqual(tier["width"], 8)
                self.assertLessEqual(tier["height"], 12)


if __name__ == "__main__":
    unittest.main()
