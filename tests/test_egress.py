import pytest

from summerand import egress


def test_probe_treats_refused_connects_as_blocked(monkeypatch):
    # What sandbox-exec and `docker --network none` produce. The real check runs under
    # offline-run (see docs/verification.md); pytest-socket raises a different error type.
    def deny(*args, **kwargs):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(egress.socket, "create_connection", deny)
    assert egress.open_targets(timeout=0.5) == []
    egress.enforce("require-blocked", "test")


def test_enforce_exits_when_egress_is_open(monkeypatch):
    monkeypatch.setattr(egress, "open_targets", lambda timeout=3.0: ["1.1.1.1:443"])
    with pytest.raises(SystemExit) as exc:
        egress.enforce("require-blocked", "api")
    assert exc.value.code == 3
    egress.enforce("off", "api")  # disabled by default
