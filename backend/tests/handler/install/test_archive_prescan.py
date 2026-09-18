from pathlib import Path

from handler.install import archive_prescan


class TestIsArchiveCandidate:
    def test_zip_is_an_archive_candidate(self):
        assert archive_prescan.is_archive_candidate(Path("GameSetup.zip")) is True

    def test_iso_is_an_archive_candidate(self):
        assert archive_prescan.is_archive_candidate(Path("game.iso")) is True

    def test_exe_is_not_an_archive_candidate(self):
        assert archive_prescan.is_archive_candidate(Path("setup.exe")) is False


class TestExtractAndRescan:
    def test_finds_and_returns_the_top_installer_candidate(self, tmp_path, monkeypatch):
        archive_path = tmp_path / "GameSetup.zip"
        archive_path.touch()

        def fake_extract(file_path, dest_dir):
            (dest_dir / "readme.txt").write_bytes(b"hi")
            (dest_dir / "setup.exe").write_bytes(b"x" * 10)
            return True

        monkeypatch.setattr(archive_prescan, "extract_archive_tree", fake_extract)

        result = archive_prescan.extract_and_rescan(archive_path)
        assert result is not None
        temp_dir, extract_root, top = result
        try:
            assert top.path == "setup.exe"
            assert (extract_root / top.path).read_bytes() == b"x" * 10
        finally:
            temp_dir.cleanup()
        assert not extract_root.exists()

    def test_extraction_failure_returns_none_and_cleans_up(self, tmp_path, monkeypatch):
        archive_path = tmp_path / "broken.zip"
        archive_path.touch()

        captured_dest = {}

        def fake_extract(file_path, dest_dir):
            captured_dest["dir"] = dest_dir
            return False

        monkeypatch.setattr(archive_prescan, "extract_archive_tree", fake_extract)

        result = archive_prescan.extract_and_rescan(archive_path)
        assert result is None
        assert not captured_dest["dir"].exists()

    def test_nothing_installer_like_inside_returns_none_and_cleans_up(
        self, tmp_path, monkeypatch
    ):
        archive_path = tmp_path / "just-docs.zip"
        archive_path.touch()

        def fake_extract(file_path, dest_dir):
            (dest_dir / "readme.txt").write_bytes(b"hi")
            return True

        monkeypatch.setattr(archive_prescan, "extract_archive_tree", fake_extract)

        result = archive_prescan.extract_and_rescan(archive_path)
        assert result is None
