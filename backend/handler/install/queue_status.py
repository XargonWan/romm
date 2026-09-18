"""Whether anything is actually listening on the install queue.

Enqueueing a job nobody consumes leaves the session stuck "installing" (or
"streaming") forever: nothing ever touches its RQ job, so there's no natural
path to a FAILED state and the client polls indefinitely. Checked before
enqueueing instead of leaving that to fail silently — a misconfigured
deployment (install-sandbox worker not running) should say so immediately,
not hang.
"""

from __future__ import annotations

from rq import Worker

from handler.redis_handler import install_queue, redis_client


def has_install_worker() -> bool:
    """True if at least one worker is currently registered on the install queue."""
    return any(
        install_queue.name in worker.queue_names()
        for worker in Worker.all(connection=redis_client)
    )
