from unittest.mock import Mock

from rq import Worker

import handler.rq_worker as rq_worker
from handler.rq_worker import RomMWorker


class TestFailInstallSession:
    def test_ignores_jobs_outside_the_install_queue(self, monkeypatch):
        update_session = Mock()
        monkeypatch.setattr(
            "handler.database.db_install_session_handler.update_session",
            update_session,
        )
        job = Mock(origin="default", args=[42])

        RomMWorker._fail_install_session(job)

        update_session.assert_not_called()

    def test_marks_the_session_failed_and_clears_vnc_fields(self, monkeypatch):
        update_session = Mock()
        monkeypatch.setattr(
            "handler.database.db_install_session_handler.update_session",
            update_session,
        )
        job = Mock(origin="install", args=[7])

        RomMWorker._fail_install_session(job)

        update_session.assert_called_once()
        session_id, data = update_session.call_args[0]
        assert session_id == 7
        assert data["state"] == "failed"
        assert data["vnc_url"] is None
        assert data["vnc_web_port"] is None
        assert data["error"]

    def test_swallows_a_missing_args_list_instead_of_raising(self, monkeypatch):
        update_session = Mock()
        monkeypatch.setattr(
            "handler.database.db_install_session_handler.update_session",
            update_session,
        )
        job = Mock(origin="install", args=[])

        RomMWorker._fail_install_session(job)  # must not raise

        update_session.assert_not_called()

    def test_swallows_an_update_failure_instead_of_raising(self, monkeypatch):
        monkeypatch.setattr(
            "handler.database.db_install_session_handler.update_session",
            Mock(side_effect=RuntimeError("db is down")),
        )
        job = Mock(origin="install", args=[7])

        RomMWorker._fail_install_session(job)  # must not raise


class TestHandleException:
    def test_fails_the_install_session_before_delegating_to_rq(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            RomMWorker,
            "_fail_install_session",
            staticmethod(lambda job: calls.append(("fail_install_session", job))),
        )
        monkeypatch.setattr(
            Worker,
            "handle_exception",
            lambda self, job, *exc_info: calls.append(("super", job, exc_info)),
        )

        worker = RomMWorker.__new__(RomMWorker)
        job = Mock()
        exc_info = (ValueError, ValueError("boom"), None)

        worker.handle_exception(job, *exc_info)

        assert calls == [
            ("fail_install_session", job),
            ("super", job, exc_info),
        ]


class TestDropRegistryCleanupFilter:
    def test_drops_the_registry_cleanup_line(self):
        record = Mock()
        record.getMessage.return_value = "Cleaning registries for queue: install"

        assert rq_worker._DropRegistryCleanupFilter().filter(record) is False

    def test_keeps_other_lines(self):
        record = Mock()
        record.getMessage.return_value = "Worker started"

        assert rq_worker._DropRegistryCleanupFilter().filter(record) is True
