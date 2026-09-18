from handler.install.copier import copy_files


class TestCopyFiles:
    def test_copies_content_and_returns_total(self, tmp_path):
        src_dir = tmp_path / "src"
        dst_dir = tmp_path / "dst"
        src_dir.mkdir()
        f1 = src_dir / "a.txt"
        f2 = src_dir / "b.txt"
        f1.write_bytes(b"hello")
        f2.write_bytes(b"world!")

        total = copy_files(
            [(f1, dst_dir / "a.txt"), (f2, dst_dir / "nested" / "b.txt")]
        )

        assert total == 11
        assert (dst_dir / "a.txt").read_bytes() == b"hello"
        assert (dst_dir / "nested" / "b.txt").read_bytes() == b"world!"

    def test_creates_destination_directories(self, tmp_path):
        src = tmp_path / "a.txt"
        src.write_bytes(b"x")
        dst = tmp_path / "deep" / "nested" / "dir" / "a.txt"

        copy_files([(src, dst)])

        assert dst.exists()

    def test_reports_cumulative_progress(self, tmp_path):
        src1 = tmp_path / "a.txt"
        src2 = tmp_path / "b.txt"
        src1.write_bytes(b"12345")
        src2.write_bytes(b"123")
        dst_dir = tmp_path / "dst"

        seen = []
        copy_files(
            [(src1, dst_dir / "a.txt"), (src2, dst_dir / "b.txt")],
            on_progress=seen.append,
        )
        assert seen[-1] == 8
        assert seen == sorted(seen)

    def test_empty_list_copies_nothing(self, tmp_path):
        assert copy_files([]) == 0

    def test_chunked_copy_matches_content(self, tmp_path, monkeypatch):
        import handler.install.copier as copier_mod

        monkeypatch.setattr(copier_mod, "CHUNK_SIZE", 3)
        src = tmp_path / "a.txt"
        src.write_bytes(b"0123456789")
        dst = tmp_path / "b.txt"

        copy_files([(src, dst)])

        assert dst.read_bytes() == b"0123456789"
