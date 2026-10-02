"""Forge and NeoForge catalogue handling without contacting the Internet."""
from __future__ import annotations

import importlib.util
import json
import pathlib
import tempfile
import unittest
from unittest import mock


RUNTIME = pathlib.Path(__file__).parents[1] / "plugin" / "runtime"


def import_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, RUNTIME / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


forge = import_module("minecraft_forge_catalog", "forge_catalog.py")
runtime = import_module("minecraft_runtime_for_hash_tests", "minecraft_runtime.py")


class ForgeCatalogueTests(unittest.TestCase):
    def test_maps_neoforge_releases_to_minecraft_versions(self):
        self.assertEqual(forge.neo_minecraft_version("21.1.200"), "1.21.1")
        self.assertEqual(forge.neo_minecraft_version("20.4.237"), "1.20.4")
        self.assertEqual(forge.neo_minecraft_version("26.3.0.17-beta"), "26.3")
        self.assertIsNone(forge.neo_minecraft_version("1.21.1"))
        with mock.patch.object(forge, "_all", return_value=("21.1.200", "26.3.0.17-beta")):
            self.assertEqual(forge.versions("neoforge"), ["26.3", "1.21.1"])
            self.assertEqual(forge.loader_versions("neoforge", "1.21.1"), ["21.1.200"])

    def test_resolves_compatible_release_and_validates_catalogue_checksum(self):
        versions = ("1.21.1-52.0.1", "1.21.1-52.0.2-beta", "1.20.4-49.0.2")
        with mock.patch.object(forge, "_all", return_value=versions), \
             mock.patch.object(forge, "_read", return_value=b"0123456789abcdef0123456789abcdef01234567\n"):
            resolved = forge.resolve("forge", "1.21.1")
            self.assertEqual(resolved["loaderVersion"], "1.21.1-52.0.1")
            self.assertTrue(resolved["url"].endswith("/1.21.1-52.0.1/forge-1.21.1-52.0.1-installer.jar"))
            self.assertEqual(resolved["sha1"], "0123456789abcdef0123456789abcdef01234567")

        with mock.patch.object(forge, "_all", return_value=("21.1.200",)), \
             mock.patch.object(forge, "_read", return_value=b"not-a-checksum"):
            with self.assertRaises(forge.CatalogueError):
                forge.resolve("neoforge", "1.21.1")

    def test_launch_args_use_supported_relative_argfile_and_reject_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            argfile = root / "libraries/net/neoforged/neoforge/21.1.200/unix_args.txt"
            argfile.parent.mkdir(parents=True)
            argfile.write_text("-cp libraries/*", encoding="utf-8")
            self.assertEqual(forge.launch_args(root, "neoforge", "21.1.200"),
                             ["@libraries/net/neoforged/neoforge/21.1.200/unix_args.txt"])
            argfile.unlink()
            argfile.symlink_to(root / "elsewhere")
            with self.assertRaises(forge.CatalogueError):
                forge.launch_args(root, "neoforge", "21.1.200")

    def test_catalogue_url_policy_rejects_http_and_unapproved_hosts(self):
        for url in ("http://maven.minecraftforge.net/file", "https://example.org/file",
                    "https://user@maven.minecraftforge.net/file", "https://maven.minecraftforge.net:444/file"):
            with self.subTest(url=url), self.assertRaises(forge.CatalogueError):
                forge._check(url)


class VerifiedDownloadTests(unittest.TestCase):
    def test_hash_mismatch_does_not_replace_destination_or_leave_partial_file(self):
        class FakeResponse:
            headers = {"Content-Length": "8"}
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, size=-1):
                nonlocal payload_read
                if payload_read: return b""
                payload_read = True
                return b"new-data"

        with tempfile.TemporaryDirectory() as tmp:
            destination = pathlib.Path(tmp) / "server.jar"
            destination.write_bytes(b"old-binary")
            payload_read = False
            opener = mock.Mock()
            opener.open.return_value = FakeResponse()
            with mock.patch.object(runtime, "OPENER", opener):
                with self.assertRaises(runtime.APIError) as caught:
                    runtime.download("https://piston-data.mojang.com/server.jar", destination,
                                     sha1="0" * 40)
            self.assertEqual(caught.exception.code, "hash_mismatch")
            self.assertEqual(destination.read_bytes(), b"old-binary")
            self.assertEqual(list(pathlib.Path(tmp).iterdir()), [destination])


if __name__ == "__main__":
    unittest.main()
