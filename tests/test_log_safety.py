"""Log records reach a handler without input or exception text (#206)."""

from __future__ import annotations

import asyncio
import io
import logging
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pydantic_core import InitErrorDetails, ValidationError
from typer.testing import CliRunner

from toolgraph import cli, log_safety
from toolgraph.redaction import WITHHELD

MALFORMED_SERVER = str(Path(__file__).parent / "fixtures" / "malformed_list_server.py")


def _assert_hidden(text: str) -> None:
    assert "QX" not in text
    assert "ZQ" not in text


LOGGER = "toolgraph.test.log_safety"


def _logged(emit) -> list[str]:
    """Lines a filtered handler writes for the records *emit* logs.

    The logger stands outside the logging hierarchy: pytest attaches its own
    capture handler to every registered logger that does not propagate.
    """
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    log_safety.install(handler)
    logger = logging.Logger(LOGGER, logging.DEBUG)
    logger.addHandler(handler)
    emit(logger)
    return stream.getvalue().splitlines()


def test_arguments_are_redacted_after_formatting():
    lines = _logged(lambda log: log.info("tool %r failed", "password=QXZQ"))

    assert lines == ["tool 'password=<redacted> failed"]


# Credential-shaped values that `%r` prints escaped (`\\t`, `\\xa0`, `\\u2000`).
ESCAPED = ["password\t=QXZQ", "password\u00a0=QXZQ", "password\u2000=QXZQ"]


@pytest.mark.parametrize("value", ESCAPED)
def test_an_argument_repr_escapes_is_checked_before_formatting(value):
    lines = _logged(lambda log: log.info("Tool %r failed", value))

    assert lines == [WITHHELD]


def test_mapping_arguments_are_checked_before_formatting():
    lines = _logged(lambda log: log.info("%(name)r", {"name": ESCAPED[0]}))

    assert lines == [WITHHELD]


def test_an_ordinary_argument_is_formatted_as_before():
    lines = _logged(lambda log: log.info("Tool %r failed", "nope"))

    assert lines == ["Tool 'nope' failed"]


@pytest.mark.parametrize("value", ESCAPED)
def test_an_exception_argument_repr_escapes_is_withheld(value):
    """``KeyError``'s text is the ``repr`` of its key."""
    assert _chain_lines(KeyError(value)) == ["boom", f"KeyError: {WITHHELD}"]


def test_percent_encoded_credential_withholds_the_message():
    lines = _logged(lambda log: log.info("Tool %r failed", "?%74oken=QXZQ"))

    assert lines == [WITHHELD]


@pytest.mark.parametrize(
    "emit",
    [
        lambda log: log.warning("bad %d", "password=QXZQ"),
        lambda log: log.warning("%s", _Unprintable()),
    ],
    ids=["bad-interpolation", "raising-str"],
)
def test_a_message_that_cannot_be_rendered_is_replaced(emit):
    lines = _logged(emit)

    assert lines == [f"log message could not be rendered ({LOGGER})"]


class _Unprintable:
    def __str__(self) -> str:
        raise RuntimeError("password=QXZQ")


def _chain_lines(exc: BaseException) -> list[str]:
    def emit(log):
        try:
            raise exc
        except BaseException:
            log.exception("boom")

    return _logged(emit)


def test_each_chain_link_is_one_redacted_line_and_no_traceback():
    try:
        try:
            raise KeyError("password=QXZQ")
        except KeyError as inner:
            raise ValueError("connect neo4j://u:QXZQ@h/r failed") from inner
    except ValueError as exc:
        lines = _chain_lines(exc)

    assert lines == [
        "boom",
        "ValueError: connect neo4j://h failed",
        "KeyError: 'password=<redacted>",
    ]


def test_implicit_context_is_shown_unless_suppressed():
    try:
        try:
            raise KeyError("first")
        except KeyError:
            raise ValueError("second")  # noqa: B904 - the implicit context is the subject
    except ValueError as exc:
        assert _chain_lines(exc) == ["boom", "ValueError: second", "KeyError: 'first'"]

    try:
        try:
            raise KeyError("first")
        except KeyError:
            raise ValueError("second") from None
    except ValueError as exc:
        assert _chain_lines(exc) == ["boom", "ValueError: second"]


def test_a_cause_cycle_is_walked_once():
    a, b = ValueError("a"), TypeError("b")
    a.__cause__, b.__cause__ = b, a

    assert _chain_lines(a) == ["boom", "ValueError: a", "TypeError: b"]


def test_an_exception_group_lists_its_members():
    group = ExceptionGroup("g", [ValueError("password=QXZQ"), TypeError("t")])

    assert _chain_lines(group) == [
        "boom",
        "ExceptionGroup: g (2 sub-exceptions)",
        "ValueError: password=<redacted>",
        "TypeError: t",
    ]


def test_nested_base_exception_groups_list_every_member():
    inner = BaseExceptionGroup("inner", [KeyboardInterrupt(), asyncio.CancelledError()])
    outer = BaseExceptionGroup("outer", [inner, ValueError("password=QXZQ")])

    assert _chain_lines(outer) == [
        "boom",
        "BaseExceptionGroup: outer (2 sub-exceptions)",
        "BaseExceptionGroup: inner (2 sub-exceptions)",
        "KeyboardInterrupt",
        "CancelledError",
        "ValueError: password=<redacted>",
    ]


def test_a_long_chain_is_cut():
    exc = ValueError("0")
    for index in range(1, 100):
        outer = ValueError(str(index))
        outer.__cause__ = exc
        exc = outer

    lines = _chain_lines(exc)

    assert len(lines) == 1 + 64
    assert lines[1] == "ValueError: 99"


def test_a_validation_error_keeps_no_input_and_a_withheld_link_keeps_the_others():
    error = ValidationError.from_exception_data(
        "?%74oken=QXZQ",
        [InitErrorDetails(type="missing", loc=("neo4j://u:QX ZQ@h",), input={"QXZQ": 1})],
    )
    try:
        try:
            raise error
        except ValidationError as inner:
            raise RuntimeError("load failed") from inner
    except RuntimeError as exc:
        lines = _chain_lines(exc)

    assert lines == ["boom", "RuntimeError: load failed", WITHHELD]


def test_type_name_and_message_are_redacted_together():
    password = type("password", (Exception,), {})

    assert _chain_lines(password("QXZQ")) == ["boom", "password=<redacted>"]


def test_an_exception_that_cannot_be_printed_keeps_its_type():
    class Unprintable(Exception):
        def __str__(self) -> str:
            raise RuntimeError("password=QXZQ")

    assert _chain_lines(Unprintable()) == [
        "boom",
        "Unprintable: (message could not be rendered)",
    ]


def test_the_handler_gets_a_new_record_with_only_standard_fields():
    try:
        raise ValueError("password=QXZQ")
    except ValueError:
        record = logging.Logger("password=QXZQ").makeRecord(
            "password=QXZQ",
            logging.ERROR,
            "/src/password=QXZQ.py",
            7,
            "x %s",
            ("password=QXZQ",),
            sys.exc_info(),
            func="f",
            extra={"payload": {"token": "QXZQ"}},
        )
    record.exc_text = "Traceback: password=QXZQ"
    record.stack_info = "Stack (most recent call last): password=QXZQ"
    record.color_message = "x %s"
    original = dict(record.__dict__)

    safe = log_safety.RedactingFilter().filter(record)

    assert record.__dict__ == original
    assert safe.msg == "x password=<redacted>\nValueError: password=<redacted>"
    assert (safe.args, safe.exc_info, safe.exc_text, safe.stack_info) == ((), None, None, None)
    assert not {"payload", "color_message"} & safe.__dict__.keys()
    assert (safe.name, safe.pathname, safe.filename, safe.module) == (
        "password=<redacted>",
        "/src/password=<redacted>",
        "password=<redacted>",
        "password=<redacted>",
    )
    assert (safe.levelno, safe.lineno, safe.funcName) == (logging.ERROR, 7, "f")
    formatted = logging.Formatter("%(name)s %(pathname)s %(module)s %(message)s").format(safe)
    _assert_hidden(formatted)


@pytest.mark.parametrize(
    "field",
    [
        "name",
        "levelname",
        "pathname",
        "filename",
        "module",
        "funcName",
        "threadName",
        "processName",
        "taskName",
    ],
)
def test_every_name_a_formatter_may_print_is_scrubbed(field):
    record = logging.makeLogRecord({"msg": "m", field: "password=QXZQ"})

    safe = log_safety.RedactingFilter().filter(record)

    assert getattr(safe, field) == "password=<redacted>"


def test_a_custom_record_factory_adds_nothing_back(monkeypatch):
    default = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = default(*args, **kwargs)
        record.payload = "password=QXZQ"
        record.color_message = "password=QXZQ"
        return record

    monkeypatch.setattr(logging, "_logRecordFactory", factory)
    record = logging.makeLogRecord({"msg": "m"})

    safe = log_safety.RedactingFilter().filter(record)

    assert not {"payload", "color_message"} & safe.__dict__.keys()


@pytest.mark.parametrize(
    "exc_info", [True, (None, None, None), ("x", "y", "z")], ids=["true", "nones", "junk"]
)
def test_an_exc_info_that_is_not_an_exception_is_ignored(exc_info):
    record = logging.makeLogRecord({"msg": "password=QXZQ", "exc_info": exc_info})

    assert log_safety.RedactingFilter().filter(record).msg == "password=<redacted>"


def test_a_record_that_cannot_be_rendered_does_not_raise(monkeypatch):
    def broken(record):
        raise RuntimeError("password=QXZQ")

    monkeypatch.setattr(log_safety, "render", broken)
    record = logging.makeLogRecord({"msg": "password=QXZQ"})

    assert log_safety.RedactingFilter().filter(record).msg == "log record could not be rendered"


def test_stack_info_is_dropped_from_the_output():
    lines = _logged(lambda log: log.warning("password=QXZQ", stack_info=True))

    assert lines == ["password=<redacted>"]


def test_install_adds_one_filter():
    handler = logging.StreamHandler(io.StringIO())

    log_safety.install(handler)
    log_safety.install(handler)

    assert sum(isinstance(f, log_safety.RedactingFilter) for f in handler.filters) == 1


def _filtered(handler: logging.Handler) -> bool:
    return any(isinstance(f, log_safety.RedactingFilter) for f in handler.filters)


def test_handlers_configured_on_an_empty_root_are_filtered(monkeypatch):
    root = logging.getLogger()
    added = logging.StreamHandler(io.StringIO())
    monkeypatch.setattr(root, "handlers", [])

    with log_safety.filtering_root_handlers_if_unconfigured():
        root.addHandler(added)

    assert _filtered(added)


def test_nothing_is_filtered_when_the_root_already_had_a_handler(monkeypatch):
    """A handler the program swaps in meanwhile is the program's too."""
    root = logging.getLogger()
    existing = logging.StreamHandler(io.StringIO())
    replacement = logging.StreamHandler(io.StringIO())
    monkeypatch.setattr(root, "handlers", [existing])

    with log_safety.filtering_root_handlers_if_unconfigured():
        root.removeHandler(existing)
        root.addHandler(replacement)

    assert (_filtered(existing), _filtered(replacement)) == (False, False)


def test_a_program_that_configured_logging_first_keeps_its_formatter():
    """A host formatter naming an ``extra`` attribute would fail on a filtered
    record, and the host's own log line would be lost."""
    program = textwrap.dedent(
        """
        import logging, sys
        logging.basicConfig(stream=sys.stdout, format="%(request_id)s %(message)s")
        import toolgraph.server.app
        logging.getLogger("host").warning("m", extra={"request_id": "r2"})
        """
    )

    proc = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, check=False, timeout=120
    )

    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "r2 m\n", "")


def test_no_last_resort_handler_is_not_an_error(monkeypatch):
    monkeypatch.setattr(logging, "lastResort", None)

    log_safety.install_on_last_resort()
    result = CliRunner().invoke(cli.app, ["init", "--backend", "bogus"])

    assert result.exit_code == 2
    assert isinstance(result.exception, SystemExit)


def test_crawl_prints_the_sdk_parse_failure_without_input(tmp_path):
    """The MCP SDK client logs a reply that fails JSON-RPC validation with its
    traceback; with no handler configured, Python's last-resort handler printed
    the pydantic error and the planted value."""
    servers = tmp_path / "servers.yaml"
    servers.write_text(
        "servers:\n"
        "  - name: malformed\n"
        "    transport: stdio\n"
        f"    command: {sys.executable}\n"
        f"    args: [{MALFORMED_SERVER}, envelope]\n",
        encoding="utf-8",
    )
    env = {
        **{key: value for key, value in os.environ.items() if not key.startswith("TOOLGRAPH_")},
        "TOOLGRAPH_BACKEND": "ladybug",
        "TOOLGRAPH_DB_PATH": str(tmp_path / "graph.lbug"),
    }

    proc = subprocess.run(
        [sys.executable, "-m", "toolgraph.cli", "crawl", "--servers", str(servers)]
        + ["--timeout", "5"],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
        env=env,
        timeout=120,
    )

    assert proc.returncode == 1
    assert "Failed to parse JSONRPC message from server" in proc.stderr
    assert "ValidationError: " in proc.stderr
    assert "Traceback" not in proc.stderr
    assert "input_value" not in proc.stderr
    _assert_hidden(proc.stderr)


# A server whose every graph read raises with a credential in the message. The
# patch is applied after `server.app` is imported, so the handlers are filtered
# exactly as in a real start.
_CRASHING_SERVER = textwrap.dedent(
    """
    from toolgraph.server import app

    def crash(fetch):
        raise RuntimeError("connect neo4j://u:QXZQ@h failed; password=QXZQ")

    app._with_generation = crash
    app.mcp.run()
    """
)


async def _call_server(stderr) -> list[str]:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-c", _CRASHING_SERVER],
        env={**os.environ, "COLUMNS": "400"},
    )
    texts = []
    async with (
        stdio_client(params, errlog=stderr) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        for name, arguments in [
            ("check_access", {"agent": "a", "tool": "s::t"}),
            ("?%74oken=QXZQ", {}),
            ("password=QXZQ", {}),
            *[(name, {}) for name in ESCAPED],
        ]:
            result = await session.call_tool(name, arguments)
            texts.append(result.content[0].text)
    return texts


def test_server_log_carries_no_input_or_exception_text(tmp_path):
    log = tmp_path / "stderr.log"
    with log.open("w") as stderr:
        texts = asyncio.run(asyncio.wait_for(_call_server(stderr), timeout=120))
    output = log.read_text()

    assert texts[0] == "Error executing tool check_access"
    assert "Tool 'check_access' raised an unexpected exception" in output
    assert "RuntimeError: connect neo4j://h failed; password=<redacted>" in output
    assert WITHHELD in output
    assert "Tool 'password=<redacted> failed" in output
    assert "Traceback" not in output
    _assert_hidden(output)
    for text in texts:
        _assert_hidden(text)
