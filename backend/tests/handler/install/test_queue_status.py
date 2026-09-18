from unittest.mock import Mock

import handler.install.queue_status as queue_status


class TestHasInstallWorker:
    def test_true_when_a_worker_listens_on_install_queue(self, monkeypatch):
        worker = Mock()
        worker.queue_names.return_value = ["install"]
        monkeypatch.setattr(queue_status.Worker, "all", lambda connection: [worker])

        assert queue_status.has_install_worker() is True

    def test_false_when_no_workers_at_all(self, monkeypatch):
        monkeypatch.setattr(queue_status.Worker, "all", lambda connection: [])

        assert queue_status.has_install_worker() is False

    def test_false_when_workers_exist_but_not_on_install_queue(self, monkeypatch):
        worker = Mock()
        worker.queue_names.return_value = ["high", "default", "low"]
        monkeypatch.setattr(queue_status.Worker, "all", lambda connection: [worker])

        assert queue_status.has_install_worker() is False

    def test_true_when_one_of_several_workers_listens(self, monkeypatch):
        other = Mock()
        other.queue_names.return_value = ["high", "default", "low"]
        install_worker = Mock()
        install_worker.queue_names.return_value = ["install"]
        monkeypatch.setattr(
            queue_status.Worker, "all", lambda connection: [other, install_worker]
        )

        assert queue_status.has_install_worker() is True
