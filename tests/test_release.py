"""Регрессии публичных метаданных и релизной конфигурации."""

import unittest
from pathlib import Path

from app_meta import APP_NAME, APP_VERSION
from web_app import Bridge


ROOT = Path(__file__).resolve().parents[1]


class ReleaseMetadataTests(unittest.TestCase):
    def test_public_metadata_is_consistent(self):
        self.assertEqual(APP_NAME, "Twitch Cutter Profanity")
        self.assertEqual(APP_VERSION, "0.1.0")
        self.assertIn(f"## [{APP_VERSION}]", (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))
        self.assertIn(f"version-{APP_VERSION}", (ROOT / "README.md").read_text(encoding="utf-8"))

    def test_windows_version_info_matches_application(self):
        info = (ROOT / "packaging/windows_version_info.txt").read_text(encoding="utf-8")
        self.assertIn(f"FileVersion', '{APP_VERSION}'", info)
        self.assertIn(f"ProductVersion', '{APP_VERSION}'", info)
        self.assertIn(f"ProductName', '{APP_NAME}'", info)

    def test_bridge_exposes_version(self):
        self.assertEqual(Bridge().get_system_info()["version"], APP_VERSION)

    def test_interface_has_current_brand_and_log_regression_fix(self):
        html = (ROOT / "design/index.html").read_text(encoding="utf-8")
        script = (ROOT / "design/app.js").read_text(encoding="utf-8")
        self.assertIn(APP_NAME, html)
        self.assertIn(f'id="systemVersion">{APP_VERSION}<', html)
        self.assertNotIn("Twitch" + "CutterMats", html + script)
        self.assertIn("renderedLogId = additions[additions.length - 1].id", script)

    def test_generated_and_local_files_are_ignored(self):
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        for pattern in ("dist_icon/", "_dist_previous/", "build_release/", "release/", "*.lnk"):
            self.assertIn(pattern, ignore)


if __name__ == "__main__":
    unittest.main()
