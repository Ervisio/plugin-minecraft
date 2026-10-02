"""Archive extraction rejects ambiguous or executable archive entries transactionally."""
from __future__ import annotations

import importlib.util
import io
import pathlib
import stat
import tarfile
import tempfile
import unittest
import zipfile
import warnings


ARCHIVE_FILE = pathlib.Path(__file__).parents[1] / "plugin" / "runtime" / "archives.py"
SPEC = importlib.util.spec_from_file_location("minecraft_archives", ARCHIVE_FILE)
archives = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(archives)


class ArchiveExtractionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.destination = self.root / "extracted"

    def tearDown(self):
        self.tmp.cleanup()

    def make_zip(self, name, members):
        path = self.root / name
        with zipfile.ZipFile(path, "w") as package:
            for entry_name, data, mode in members:
                info = zipfile.ZipInfo(entry_name)
                if mode is not None:
                    info.external_attr = (mode & 0xFFFF) << 16
                package.writestr(info, data)
        return path

    def make_tar(self, name, members):
        path = self.root / name
        with tarfile.open(path, "w:gz") as package:
            for entry_name, kind, data in members:
                info = tarfile.TarInfo(entry_name)
                if kind == "file":
                    raw = data.encode()
                    info.size = len(raw)
                    package.addfile(info, io.BytesIO(raw))
                elif kind == "symlink":
                    info.type = tarfile.SYMTYPE
                    info.linkname = data
                    package.addfile(info)
                elif kind == "fifo":
                    info.type = tarfile.FIFOTYPE
                    package.addfile(info)
        return path

    def test_extracts_valid_zip_and_tar_with_regular_files_and_directories(self):
        zip_path = self.make_zip("valid.zip", [("mods/example.jar", b"jar bytes", stat.S_IFREG),
                                                 ("config/", b"", stat.S_IFDIR)])
        result = archives.extract_archive(zip_path, self.destination)
        self.assertEqual(result["files"], 2)
        self.assertEqual((self.destination / "mods/example.jar").read_bytes(), b"jar bytes")
        self.assertEqual((self.destination / "mods/example.jar").stat().st_mode & 0o777, 0o640)

        tar_path = self.make_tar("valid.tar.gz", [("config/options.txt", "file", "safe=true")])
        second = self.root / "second"
        archives.extract_archive(tar_path, second)
        self.assertEqual((second / "config/options.txt").read_text(), "safe=true")

    def test_rejects_zip_traversal_absolute_paths_and_duplicates_without_touching_destination(self):
        (self.destination).mkdir()
        sentinel = self.destination / "keep.txt"
        sentinel.write_text("preserve")
        for name, entries in (
            ("traversal.zip", [("../escape", b"bad", stat.S_IFREG)]),
            ("absolute.zip", [("/tmp/escape", b"bad", stat.S_IFREG)]),
            ("duplicate.zip", [("same", b"one", stat.S_IFREG), ("same", b"two", stat.S_IFREG)]),
        ):
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                path = self.make_zip(name, entries)
            with self.subTest(archive=name), self.assertRaises(archives.ArchiveError):
                archives.extract_archive(path, self.destination)
            self.assertEqual(sentinel.read_text(), "preserve")

    def test_rejects_zip_symlink_entries_and_tar_links_or_special_files(self):
        zip_link = self.make_zip("link.zip", [("link", b"../../etc/passwd", stat.S_IFLNK | 0o777)])
        tar_link = self.make_tar("link.tar.gz", [("link", "symlink", "../../etc/passwd")])
        tar_fifo = self.make_tar("fifo.tar.gz", [("pipe", "fifo", "")])
        for path in (zip_link, tar_link, tar_fifo):
            with self.subTest(archive=path.name), self.assertRaises(archives.ArchiveError):
                archives.extract_archive(path, self.root / (path.stem + "-out"))

    def test_enforces_member_and_expanded_size_limits_and_rejects_symlink_destination(self):
        path = self.make_zip("limited.zip", [("large.bin", b"0123456789", stat.S_IFREG)])
        with self.assertRaises(archives.ArchiveError):
            archives.extract_archive(path, self.destination, max_bytes=4)
        with self.assertRaises(archives.ArchiveError):
            archives.extract_archive(path, self.destination, max_files=0)
        outside = self.root / "outside"
        outside.mkdir()
        link = self.root / "destination-link"
        link.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(archives.ArchiveError):
            archives.extract_archive(path, link)

    def test_rejects_existing_nonempty_destination(self):
        path = self.make_zip("valid.zip", [("file", b"data", stat.S_IFREG)])
        self.destination.mkdir()
        (self.destination / "existing").write_text("untouched")
        with self.assertRaises(archives.ArchiveError):
            archives.extract_archive(path, self.destination)
        self.assertEqual((self.destination / "existing").read_text(), "untouched")


if __name__ == "__main__":
    unittest.main()
