"""Regression test: PowerBIConnector.set_user_token is thread-scoped (P1).

Incident class: set_user_token used to mutate shared instance state, so one
user's delegated token leaked into concurrent requests and into calls meant
for the service principal (gunicorn reuses its 8 worker threads).

Run standalone:  python tests/test_powerbi_token_scoping.py
"""
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

from powerbi_connector import PowerBIConnector  # noqa: E402


def test_thread_scoped_token():
    pb = PowerBIConnector()
    seen = {}

    def worker():
        pb.set_user_token("tok-B")
        seen["in_thread"] = pb.user_token

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert seen["in_thread"] == "tok-B", seen
    # The main thread must NOT see the other thread's override.
    assert pb.user_token is None, pb.user_token


def test_instance_default_and_clear():
    pb = PowerBIConnector(user_token="tok-default")
    assert pb.user_token == "tok-default"
    pb.set_user_token("tok-x")
    assert pb.user_token == "tok-x"       # thread override wins
    pb.clear_user_token()
    assert pb.user_token == "tok-default"  # falls back after clear


def test_attribute_assignment_is_instance_default():
    # __init__ / direct assignment go through the setter -> instance default
    pb = PowerBIConnector()
    pb.user_token = "tok-init"
    assert pb.user_token == "tok-init"
    other = threading.Thread(target=lambda: pb.set_user_token("tok-t"))
    other.start()
    other.join()
    assert pb.user_token == "tok-init"    # thread override did not leak back


if __name__ == "__main__":
    test_thread_scoped_token()
    test_instance_default_and_clear()
    test_attribute_assignment_is_instance_default()
    print("All tests passed.")
