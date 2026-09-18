import logging
from typing import Any

from rq import Worker
from rq.job import Job


class _DropRegistryCleanupFilter(logging.Filter):
    """Drops RQ's periodic "cleaning registries for queue" INFO line.

    The maintenance sweep still runs (crash recovery for orphaned jobs, TTL
    reaping, stale worker pruning); only its log record is suppressed so it
    does not flood logs on every maintenance interval.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return "cleaning registries for queue" not in record.getMessage().lower()


class RomMWorker(Worker):
    """RQ worker that silences the noisy registry-cleanup log line and, on
    the install queue, guarantees a stuck InstallSession gets marked FAILED.

    ``handler.install.runner.run_install`` already catches its own
    exceptions and fails the session itself, but that only covers failures
    once the job body is actually running. A failure before that (the job
    can't even be imported - a broken worker image, a bad deploy) never
    reaches that try/except, so without this the session sits in whatever
    state it was last polled at forever and the UI spins indefinitely.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if not any(isinstance(f, _DropRegistryCleanupFilter) for f in self.log.filters):
            self.log.addFilter(_DropRegistryCleanupFilter())

    def handle_exception(self, job: Job, *exc_info: Any) -> None:
        self._fail_install_session(job)
        super().handle_exception(job, *exc_info)

    @staticmethod
    def _fail_install_session(job: Job) -> None:
        # Lazy imports: this runs on any unhandled job exception, including
        # ones raised while importing application modules, so importing
        # these at module load time would risk the same failure mode.
        from handler.redis_handler import QueuePrio

        if job.origin != QueuePrio.INSTALL.value:
            return
        try:
            install_session_id = job.args[0]
        except Exception:  # noqa: BLE001 - malformed/undeserializable job
            return
        try:
            from handler.database import db_install_session_handler
            from models.install_session import InstallSessionState

            db_install_session_handler.update_session(
                install_session_id,
                {
                    "state": InstallSessionState.FAILED,
                    "error": "The install worker crashed before it could run this job",
                    "vnc_url": None,
                    "vnc_web_port": None,
                },
            )
        except Exception as e:  # noqa: BLE001 - safety net, never mask the real error
            logging.getLogger(__name__).error(
                f"Couldn't mark install session {install_session_id} failed "
                f"after a worker crash: {e}"
            )
