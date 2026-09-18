from handler.install.progress import ThrottledProgress


class TestThrottledProgress:
    def test_first_call_always_fires(self, monkeypatch):
        # _last_sent_at starts at 0.0; force `now` low too so the first call
        # doesn't depend on the real monotonic clock's absolute value.
        import handler.install.progress as progress_mod

        monkeypatch.setattr(progress_mod.time, "monotonic", lambda: 0.0)

        seen = []
        report = ThrottledProgress(seen.append, min_interval=999)
        report(10)
        assert seen == [10]

    def test_calls_within_interval_are_dropped(self, monkeypatch):
        import handler.install.progress as progress_mod

        clock = iter([0.0, 0.1, 0.2, 0.3])
        monkeypatch.setattr(progress_mod.time, "monotonic", lambda: next(clock))

        seen = []
        report = ThrottledProgress(seen.append, min_interval=1.0)
        report(10)  # t=0.0, fires (first call)
        report(20)  # t=0.1, dropped
        report(30)  # t=0.2, dropped
        assert seen == [10]

    def test_fires_again_once_interval_elapses(self, monkeypatch):
        import handler.install.progress as progress_mod

        clock = iter([0.0, 2.0])
        monkeypatch.setattr(progress_mod.time, "monotonic", lambda: next(clock))

        seen = []
        report = ThrottledProgress(seen.append, min_interval=1.0)
        report(10)  # t=0.0, fires
        report(20)  # t=2.0, interval elapsed, fires
        assert seen == [10, 20]

    def test_finish_flushes_final_value(self):
        seen = []
        report = ThrottledProgress(seen.append, min_interval=999)
        report(10)
        report.finish(50)
        assert seen == [10, 50]

    def test_finish_is_noop_if_value_already_sent(self):
        seen = []
        report = ThrottledProgress(seen.append, min_interval=999)
        report(10)
        report.finish(10)
        assert seen == [10]
