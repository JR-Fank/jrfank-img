#!/usr/bin/env python3
"""Pinned legacy photo recipe compatibility in webimg.py."""
from __future__ import annotations

import hashlib
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("webimg_recipe_test", ROOT / "tools/webimg.py")
assert SPEC and SPEC.loader
webimg = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(webimg)


class WebimgRecipeTest(unittest.TestCase):
    def legacy_info(self, root: Path, src_sha: str) -> dict:
        output = root / "fixture/photo-600.legacy.webp"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"existing v2 output")
        return {
            "version": webimg.recipe_version(src_sha, webimg.LEGACY_V2_RECIPE),
            "sourceSha256": src_sha,
            "sizes": {"600": {"file": output.relative_to(root).as_posix()}},
        }

    def test_v2_jpeg_is_up_to_date(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for extension in ("jpg", "jpeg", "png", "tif", "tiff", "webp", "JPG"):
                with self.subTest(extension=extension):
                    src = root / f"originals/fixture/photo.{extension}"
                    src.parent.mkdir(parents=True, exist_ok=True)
                    src.write_bytes(f"same {extension} source".encode())
                    src_sha = webimg.sha256(src)
                    info = self.legacy_info(root, src_sha)
                    with patch.object(webimg, "ROOT", root):
                        self.assertTrue(webimg.up_to_date(info, src, src_sha))

    def test_v2_heic_is_not_up_to_date(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "originals/fixture/photo.heic"
            src.parent.mkdir(parents=True)
            src.write_bytes(b"same heic source")
            src_sha = webimg.sha256(src)
            info = self.legacy_info(root, src_sha)
            with patch.object(webimg, "ROOT", root):
                self.assertFalse(webimg.up_to_date(info, src, src_sha))

    def test_v2_changed_source_is_not_up_to_date(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "originals/fixture/photo.png"
            src.parent.mkdir(parents=True)
            src.write_bytes(b"old png source")
            old_sha = webimg.sha256(src)
            info = self.legacy_info(root, old_sha)
            src.write_bytes(b"replacement png source")
            new_sha = webimg.sha256(src)
            self.assertNotEqual(old_sha, new_sha)
            with patch.object(webimg, "ROOT", root):
                self.assertFalse(webimg.up_to_date(info, src, new_sha))


    def test_v3_srgb_is_up_to_date_for_all_photo_formats_unless_forced(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for extension in ("jpg", "heic", "png", "webp"):
                with self.subTest(extension=extension):
                    src = root / f"originals/fixture/photo.{extension}"
                    src.parent.mkdir(parents=True, exist_ok=True)
                    src.write_bytes(f"v3 {extension} source".encode())
                    src_sha = webimg.sha256(src)
                    output = root / f"fixture/photo-600.v3-{extension}.webp"
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_bytes(b"existing v3 srgb output")
                    info = {
                        "version": webimg.recipe_version(src_sha, webimg.V3_SRGB_RECIPE),
                        "sourceSha256": src_sha,
                        "sizes": {"600": {"file": output.relative_to(root).as_posix()}},
                    }
                    with patch.object(webimg, "ROOT", root):
                        self.assertTrue(webimg.up_to_date(info, src, src_sha))
                        self.assertFalse(
                            webimg.up_to_date(info, src, src_sha, webimg.photo_version(src_sha))
                        )

    def test_v4_p3_is_up_to_date_only_after_4000_tier_removed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "originals/fixture/photo.jpg"
            src.parent.mkdir(parents=True, exist_ok=True)
            src.write_bytes(b"v4 p3 source")
            src_sha = webimg.sha256(src)
            out_2400 = root / "fixture/photo-2400.v4.webp"
            out_4000 = root / "fixture/photo-4000.v4.webp"
            out_2400.parent.mkdir(parents=True, exist_ok=True)
            out_2400.write_bytes(b"existing 2400 output")
            out_4000.write_bytes(b"existing 4000 output")
            version = webimg.recipe_version(src_sha, webimg.LEGACY_V4_P3_RECIPE)
            with patch.object(webimg, "ROOT", root):
                self.assertFalse(
                    webimg.up_to_date(
                        {
                            "version": version,
                            "sourceSha256": src_sha,
                            "sizes": {
                                "2400": {"file": out_2400.relative_to(root).as_posix()},
                                "4000": {"file": out_4000.relative_to(root).as_posix()},
                            },
                        },
                        src,
                        src_sha,
                    )
                )
                self.assertTrue(
                    webimg.up_to_date(
                        {
                            "version": version,
                            "sourceSha256": src_sha,
                            "sizes": {
                                "2400": {"file": out_2400.relative_to(root).as_posix()},
                            },
                        },
                        src,
                        src_sha,
                    )
                )


if __name__ == "__main__":
    unittest.main()
