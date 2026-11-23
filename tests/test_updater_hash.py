# -*- coding: utf-8 -*-
"""自动更新清单与 SHA-256 校验测试。"""
import hashlib
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ui.updater import _normalize_manifest, _sha256_file, _verify_sha256


class TestManifestSha256(unittest.TestCase):
    def test_normalize_basenames_and_validation(self):
        good = "a" * 64
        info = _normalize_manifest({
            "version": "v1.6.0",
            "sha256": {
                r"C:\build\StandardQueryTool.zip": good,
                "setup.exe": good.upper(),
                "bad.zip": "not-a-hash",
            },
        })
        self.assertEqual(info["version"], "1.6.0")
        self.assertEqual(info["sha256"]["standardquerytool.zip"], good)
        self.assertEqual(info["sha256"]["setup.exe"], good)
        self.assertNotIn("bad.zip", info["sha256"])

    def test_missing_sha_is_compatible(self):
        info = _normalize_manifest({"version": "1.6.0"})
        self.assertEqual(info["sha256"], {})


class TestVerifySha256(unittest.TestCase):
    def test_verify_ok_and_mismatch(self):
        fd, path = tempfile.mkstemp(suffix=".bin")
        os.close(fd)
        try:
            with open(path, "wb") as f:
                f.write(b"standard-query-tool")
            digest = hashlib.sha256(b"standard-query-tool").hexdigest()
            self.assertEqual(_sha256_file(path), digest)
            self.assertTrue(_verify_sha256(path, digest))
            self.assertTrue(_verify_sha256(path, ""))
            with self.assertRaises(Exception):
                _verify_sha256(path, "0" * 64)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass


if __name__ == "__main__":
    unittest.main()

class TestFetchLatestManifest(unittest.TestCase):
    def test_picks_highest_version_across_mirrors(self):
        import json
        from unittest.mock import patch
        import ui.updater as updater

        payloads = {
            updater.VERSION_MANIFEST_URLS[0]: {"version": "1.6.0", "notes": "github"},
            updater.VERSION_MANIFEST_URLS[1]: {"version": "1.5.9", "notes": "gitee"},
        }

        class Resp:
            def __init__(self, data):
                self._data = json.dumps(data).encode("utf-8")

            def read(self):
                return self._data

        def fake_open(url, timeout=12):
            return Resp(payloads[url])

        with patch.object(updater, "_updater_urlopen", side_effect=fake_open):
            info = updater.fetch_latest_release()
        self.assertEqual(info["version"], "1.6.0")


class TestDownloadUrlFilter(unittest.TestCase):
    def test_only_trusted_https_release_urls(self):
        from ui.updater import build_download_urls
        info = {
            "download_urls": [
                "https://github.com/lovethx2018/StandardQueryTool/releases/download/v1.6.0/a.zip",
                "https://gitee.com/lovethx2024/app-update-store/releases/download/v1.6.0/a.zip",
                "http://github.com/insecure/a.zip",
                "https://evil.example.com/a.zip",
            ]
        }
        urls = build_download_urls(info)
        self.assertEqual(len(urls), 2)
        self.assertTrue(urls[0].startswith("https://github.com/"))
        self.assertTrue(urls[1].startswith("https://gitee.com/"))
