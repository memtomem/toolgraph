"""uvicorn's own log lines under `serve --http` carry no credential (#226)."""

from __future__ import annotations

import io
import logging
import os
import re
import socket
import subprocess
import sys
import textwrap
import time
from http import HTTPStatus

import pytest
from uvicorn.config import LOGGING_CONFIG
from uvicorn.logging import AccessFormatter, DefaultFormatter

from toolgraph import log_safety
from toolgraph.redaction import WITHHELD

ACCESS_FORMAT = '%s - "%s %s HTTP/%s" %d'
ACCESS_FMT = LOGGING_CONFIG["formatters"]["access"]["fmt"]
DEFAULT_FMT = LOGGING_CONFIG["formatters"]["default"]["fmt"]
UVICORN_LOGGERS = ("uvicorn.access", "uvicorn.error", "uvicorn.asgi")


def _assert_hidden(text: str) -> None:
    assert "QX" not in text
    assert "ZQ" not in text


def _record(args, msg=ACCESS_FORMAT, **attrs) -> logging.LogRecord:
    record = logging.LogRecord("uvicorn.access", logging.INFO, "h11_impl.py", 1, msg, args, None)
    record.__dict__.update(attrs)
    return record


def _withheld(status: int = 400) -> tuple:
    return ("-", "-", WITHHELD, "-", status)


CLEAN = [
    ("127.0.0.1:5000", "GET", "/mcp?page=2", "1.1", 400),
    ("127.0.0.1:5000", "POST", "/mcp/a%20b", "1.1", 404),
]

CREDENTIAL = [
    ("127.0.0.1:5000", "GET", "/mcp?token=QXZQ", "1.1", 400),
    ("127.0.0.1:5000", "GET", "/mcp?%74oken=QXZQ", "1.1", 400),
    ("127.0.0.1:5000", "POST", "/mcp?password=QXZQ&x=1", "1.1", 400),
    # Each field alone is clean; together they form a credential.
    ("127.0.0.1:5000", "Bearer", "/QXZQ", "1.1", 404),
    ("127.0.0.1:5000", "token", "%3DQXZQ", "1.1", 404),
    # A client address taken from a trusted proxy's X-Forwarded-For.
    ('https://u"QXZQ:0', "GET", "/mcp?x=@", "1.1", 400),
]

# Withheld though nothing in them is a credential: the line is judged whole.
OVER_WITHHELD = [
    # A placeholder sent by the client is not trusted as one.
    ("Bearer <redacted>:0", "GET", "/mcp?page=2", "1.1", 400),
    # A URI's path is hidden everywhere Toolgraph prints one.
    ("127.0.0.1:5000", "GET", "/mcp?url=https://example.org/docs", "1.1", 200),
]


@pytest.mark.parametrize("use_colors", [False, True])
@pytest.mark.parametrize(
    ("args", "expected"),
    [(args, args) for args in CLEAN]
    + [(args, _withheld(args[4])) for args in CREDENTIAL + OVER_WITHHELD],
)
def test_an_access_line_passes_whole_or_keeps_only_its_status(args, expected, use_colors):
    formatter = AccessFormatter(ACCESS_FMT, use_colors=use_colors)

    safe = log_safety.AccessLogFilter().filter(_record(args))

    assert formatter.format(safe) == formatter.format(_record(expected))
    _assert_hidden(formatter.format(safe))


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/mcp?page=2", 'INFO:     127.0.0.1:5000 - "GET /mcp?page=2 HTTP/1.1" 406 Not Acceptable'),
        ("/mcp?token=QXZQ", f'INFO:     - - "- {WITHHELD} HTTP/-" 406 Not Acceptable'),
    ],
)
def test_an_http_status_member_counts_as_the_status(path, expected):
    # The MCP SDK answers with HTTPStatus members, which uvicorn logs as given.
    args = ("127.0.0.1:5000", "GET", path, "1.1", HTTPStatus.NOT_ACCEPTABLE)

    safe = log_safety.AccessLogFilter().filter(_record(args))

    assert type(safe.args[4]) is int
    assert AccessFormatter(ACCESS_FMT, use_colors=False).format(safe) == expected


def test_the_plain_access_lines_read_as_expected():
    formatter = AccessFormatter(ACCESS_FMT, use_colors=False)
    lines = [
        formatter.format(log_safety.AccessLogFilter().filter(_record(args)))
        for args in [CLEAN[0], CREDENTIAL[0]]
    ]

    assert lines == [
        'INFO:     127.0.0.1:5000 - "GET /mcp?page=2 HTTP/1.1" 400 Bad Request',
        f'INFO:     - - "- {WITHHELD} HTTP/-" 400 Bad Request',
    ]


def test_a_message_that_is_not_the_access_format_is_withheld():
    # AccessFormatter builds the request line from the arguments and ignores
    # the message, so a message that drops them would hide them from a check
    # on the message alone.
    args = ("127.0.0.1:5000", "GET", "/mcp?token=QXZQ", "1.1", 200)
    record = _record(args, msg="safe %.0s %.0s %.0s %.0s %d")

    safe = log_safety.AccessLogFilter().filter(record)

    assert (safe.msg, safe.args) == (ACCESS_FORMAT, _withheld(200))


class _FormatsAsCredential(str):
    """Reads as ``/mcp`` through ``%s``; an f-string shows a credential."""

    def __format__(self, spec: str) -> str:
        return "token=QXZQ"


@pytest.mark.parametrize("use_colors", [False, True])
def test_a_field_that_is_not_a_plain_string_is_withheld(use_colors):
    formatter = AccessFormatter(ACCESS_FMT, use_colors=use_colors)
    args = ("127.0.0.1:5000", "GET", _FormatsAsCredential("/mcp"), "1.1", 200)

    safe = log_safety.AccessLogFilter().filter(_record(args))

    assert safe.args == _withheld(200)
    _assert_hidden(formatter.format(safe))


@pytest.mark.parametrize("use_colors", [False, True])
def test_the_rebuilt_access_record_carries_no_extra_attribute(use_colors):
    args = ("127.0.0.1:5000", "GET", "/mcp?page=2", "1.1", 200)
    record = _record(
        args,
        color_message='%s - "%s %s HTTP/%s" %d token=QXZQ',
        exc_text="Traceback: password=QXZQ",
        stack_info="Stack: password=QXZQ",
        payload="password=QXZQ",
    )

    safe = log_safety.AccessLogFilter().filter(record)

    assert not {"color_message", "payload"} & safe.__dict__.keys()
    assert (safe.exc_info, safe.exc_text, safe.stack_info) == (None, None, None)
    assert DefaultFormatter(DEFAULT_FMT, use_colors=use_colors).format(safe) == (
        DefaultFormatter(DEFAULT_FMT, use_colors=use_colors).format(_record(args))
    )


def _handled(emit, formatter: logging.Formatter) -> list[str]:
    """Lines a uvicorn.access logger with the filter writes for *emit*.

    The logger stands outside the logging hierarchy, so pytest's capture
    handler and the real uvicorn loggers are not involved.
    """
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(formatter)
    logger = logging.Logger("uvicorn.access", logging.INFO)
    logger.addFilter(log_safety.AccessLogFilter())
    logger.addHandler(handler)
    emit(logger)
    return stream.getvalue().splitlines()


@pytest.mark.parametrize(
    "emit",
    [
        lambda log: log.info("%s %s", "/mcp?token=QXZQ", "x"),
        lambda log: log.info("%(path)s", {"path": "/mcp?token=QXZQ"}),
        lambda log: log.info("token=QXZQ"),
        # %d of a text status cannot be formatted.
        lambda log: log.info(ACCESS_FORMAT, "127.0.0.1:1", "GET", "/mcp?token=QXZQ", "1.1", "x"),
    ],
)
def test_a_record_the_access_formatter_cannot_read_is_dropped(emit, capsys):
    lines = _handled(emit, AccessFormatter(ACCESS_FMT, use_colors=False))

    assert lines == []
    assert capsys.readouterr().err == ""


def test_an_access_record_drops_its_exception_and_stack(capsys):
    def emit(log):
        try:
            raise RuntimeError("password=QXZQ")
        except RuntimeError:
            log.info(ACCESS_FORMAT, *CLEAN[0], exc_info=True, stack_info=True)

    lines = _handled(emit, AccessFormatter(ACCESS_FMT, use_colors=False))

    assert lines == ['INFO:     127.0.0.1:5000 - "GET /mcp?page=2 HTTP/1.1" 400 Bad Request']
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    ("formatter", "expected"),
    [
        (
            AccessFormatter(ACCESS_FMT, use_colors=False),
            f'INFO:     - - "- {WITHHELD} HTTP/-" 200 OK',
        ),
        (
            DefaultFormatter(DEFAULT_FMT, use_colors=False),
            f'INFO:     - - "- {WITHHELD} HTTP/-" 200',
        ),
    ],
)
def test_a_credential_in_the_message_itself_is_withheld(formatter, expected):
    lines = _handled(
        lambda log: log.info("token=QXZQ %s %s %s %s %d", *CLEAN[0][:4], 200), formatter
    )

    assert lines == [expected]


@pytest.fixture
def uvicorn_filters():
    """Restore the uvicorn loggers' filters after the test."""
    saved = {name: list(logging.getLogger(name).filters) for name in UVICORN_LOGGERS}
    yield
    for name, filters in saved.items():
        logging.getLogger(name).filters[:] = filters


def test_each_uvicorn_logger_gets_one_filter(uvicorn_filters):
    log_safety.filter_uvicorn_loggers()
    log_safety.filter_uvicorn_loggers()

    kinds = {
        name: [
            type(f)
            for f in logging.getLogger(name).filters
            if type(f).__module__ == log_safety.__name__
        ]
        for name in UVICORN_LOGGERS
    }
    assert kinds == {
        "uvicorn.access": [log_safety.AccessLogFilter],
        "uvicorn.error": [log_safety.RedactingFilter],
        "uvicorn.asgi": [log_safety._DropFilter],
    }


@pytest.mark.parametrize("use_colors", [False, True])
def test_uvicorn_error_records_are_rebuilt_without_input(use_colors, uvicorn_filters):
    name = "uvicorn.error"
    log_safety.filter_uvicorn_loggers()
    try:
        raise RuntimeError("connect neo4j://u:QXZQ@h failed")
    except RuntimeError:
        record = logging.getLogger(name).makeRecord(
            name,
            logging.ERROR,
            "websockets_impl.py",
            1,
            '%s - "WebSocket %s" [accepted]',
            ("127.0.0.1:5000", "/ws?token=QXZQ"),
            sys.exc_info(),
        )
    record.color_message = '%s - "WebSocket %s" [accepted] token=QXZQ'

    safe = logging.getLogger(name).filter(record)
    text = DefaultFormatter(DEFAULT_FMT, use_colors=use_colors).format(safe)

    assert "color_message" not in safe.__dict__
    assert "Traceback" not in text
    assert text.splitlines()[-1] == "RuntimeError: connect neo4j://h failed"
    _assert_hidden(text)


def test_uvicorn_asgi_scope_dumps_are_dropped(uvicorn_filters):
    # What uvicorn's message logger writes at TRACE: the whole scope, so a
    # credential can span fields (method and path) or sit in a header.
    log_safety.filter_uvicorn_loggers()
    record = logging.getLogger("uvicorn.asgi").makeRecord(
        "uvicorn.asgi",
        5,
        "message_logger.py",
        1,
        "%s Started scope=%s",
        ("ASGI [1]", {"method": "Bearer", "path": "/QXZQ"}),
        None,
    )

    assert logging.getLogger("uvicorn.asgi").filter(record) is False


# uvicorn replaces its loggers' handlers when a Config is made; the filters
# must survive that. Run in a subprocess so the test process's logging is
# left as it was.
_CONFIGURED = textwrap.dedent(
    """
    import logging
    import uvicorn
    from toolgraph import log_safety

    log_safety.filter_uvicorn_loggers()
    uvicorn.Config(app=lambda scope, receive, send: None)
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d', "127.0.0.1:5000", "GET", "/mcp?token=QXZQ", "1.1", 400
    )
    logging.getLogger("uvicorn.error").error("bind %s", "password=QXZQ")
    """
)


def test_the_filters_survive_uvicorns_logging_configuration():
    proc = subprocess.run(
        [sys.executable, "-c", _CONFIGURED],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines() == [f'INFO:     - - "- {WITHHELD} HTTP/-" 400 Bad Request']
    assert proc.stderr.splitlines() == ["ERROR:    bind password=<redacted>"]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _request(port: int, request_line: str) -> None:
    # A Host with the port passes the SDK's host check, so a request reaches
    # the MCP transport and is answered with an HTTPStatus member.
    head = f"{request_line}\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n"
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall(head.encode())
        while sock.recv(4096):
            pass


def _wait_for(port: int, proc: subprocess.Popen) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(f"server exited with {proc.returncode}")
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            return
        except OSError:
            time.sleep(0.1)
    raise AssertionError("server did not start")


def test_a_served_request_line_with_a_credential_is_withheld(tmp_path):
    port = _free_port()
    env = {
        **{key: value for key, value in os.environ.items() if not key.startswith("TOOLGRAPH_")},
        "TOOLGRAPH_BACKEND": "ladybug",
        "TOOLGRAPH_DB_PATH": str(tmp_path / "graph.lbug"),
    }
    server = (
        f"from toolgraph.server.app import mcp\nmcp.run(transport='streamable-http', port={port})\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", server],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    try:
        _wait_for(port, proc)
        for line in [
            "GET /mcp?page=2 HTTP/1.1",
            "GET /mcp?token=QXZQ HTTP/1.1",
            "POST /mcp?%74oken=QXZQ HTTP/1.1",
            "Bearer /QXZQ HTTP/1.1",
        ]:
            _request(port, line)
    finally:
        proc.terminate()
        try:
            stdout, stderr = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()

    request_lines = re.findall(r' - "(.*)" (\d{3})', stdout)
    assert request_lines == [
        ("GET /mcp?page=2 HTTP/1.1", "406"),
        (f"- {WITHHELD} HTTP/-", "406"),
        (f"- {WITHHELD} HTTP/-", "400"),
        (f"- {WITHHELD} HTTP/-", "404"),
    ]
    _assert_hidden(stdout + stderr)
