"""Helpers shared by test modules (not a test module itself — the name does not
match the ``test*.py`` discovery pattern).

``TempMediaRoot`` — a throw-away MEDIA_ROOT for one test module that is safe
under every ``--parallel`` start method. The old pattern (``mkdtemp`` at import
+ ``rmtree`` in ``tearDownModule``) broke under ``fork`` (Linux, the production
image): every worker inherits the *same* path from the parent, and the first
worker to finish its share of the module deleted the directory while another
was still writing photos into it (``FileNotFoundError`` in the amd64 run,
2026-10-04). On macOS (``spawn``) each worker re-imports the module and gets
its own directory, which is why it never showed locally.

Now only the process that created the directory removes it: a forked worker's
``cleanup()`` is a no-op and the parent removes the directory at exit.
"""

import atexit
import os
import shutil
import tempfile


class TempMediaRoot:
    def __init__(self, prefix):
        self.path = tempfile.mkdtemp(prefix=prefix)
        self._owner_pid = os.getpid()
        atexit.register(self.cleanup)

    def cleanup(self):
        if os.getpid() == self._owner_pid:
            shutil.rmtree(self.path, ignore_errors=True)
