"""Run the Roxy-WI scheduler as a single dedicated process."""

import os
import threading


# This entry point is the dedicated scheduler by definition. Override a stale
# service-level value instead of silently starting a process without any jobs.
os.environ['ROXYWI_SCHEDULER_ENABLED'] = '1'

from app import scheduler  # noqa: E402


if __name__ == '__main__':
    if not scheduler.running:
        raise RuntimeError('The scheduler did not start')
    threading.Event().wait()
