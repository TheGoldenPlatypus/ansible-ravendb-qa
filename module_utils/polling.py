"""polling -- a small poll-until-condition helper shared by the RavenDB modules."""

import time

def poll_until(predicate, timeout, interval=2.0):
    """Call `predicate` every `interval` until it returns done; return (done, value, elapsed)."""
    start = time.monotonic()
    deadline = start + timeout

    while True:
        done, value = predicate()
        elapsed = time.monotonic() - start

        if done:
            return True, value, elapsed

        if time.monotonic() >= deadline:
            return False, value, elapsed

        time.sleep(interval)
