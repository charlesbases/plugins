"""An OS-owned writer lock for one public command's data directory and plan.

Only the CLI acquires this lock. Nested pipeline operations retain their own
leases and validation. The empty lock file is a stable rendezvous; its presence
or any process identifier is never evidence that a command is still running.
"""
import contextlib
import errno
import os

from contracts import RetryableError
from file_io import plan_path


@contextlib.contextmanager
def command_gate(root, plan, *, read_only=False):
    target = plan_path(root, plan)
    if read_only:
        yield
        return
    # Keep this outside plans/<plan>: Store's empty-plan check must remain
    # authoritative, and a command lock is not existing financial state.
    directory = target.parent.parent/".command-gates"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory/(plan+".lock")
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    acquired = False
    try:
        os.set_inheritable(descriptor, False)
        try:
            if os.name == "nt":
                import msvcrt
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK} or getattr(exc, "winerror", None) in {32, 33}:
                raise RetryableError("Another investment command is running for this data directory and plan. "
                    "Wait for the original command session to finish, then retry the same request.") from exc
            raise
        yield
    finally:
        try:
            if acquired:
                if os.name == "nt":
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
