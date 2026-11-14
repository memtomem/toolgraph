"""Log records rebuilt without credential-shaped values or exception text.

Toolgraph's own log calls and the MCP SDK's both log text that came from
outside: a tool name a client sent, a reply a crawled server sent, an
exception whose message quotes a connection URI. A traceback repeats every
exception message in the chain verbatim.

``RedactingFilter`` sits on a handler and hands it a new record in which:

- the message is rendered once and passed through ``redact_text``;
- each exception in the chain becomes one line, ``<Type>: <text>``, with the
  text from ``describe_error`` (a pydantic ``ValidationError`` keeps no input),
  and the whole line passed through ``redact_text``;
- of the original's other attributes, only the standard ones a formatter may
  print are kept, the names among them passed through ``redact_text``. The
  traceback, cached exception text, stack, arguments and any ``extra``
  attribute are not carried over.

Frames are not printed: a frame's file and function names are not input, but
nothing guarantees it, and the exception chain is what an operator reads.

Served over HTTP, uvicorn logs each request line with its query string on
loggers of its own. ``AccessLogFilter`` withholds such a line when it carries
a credential, and ``filter_uvicorn_loggers`` puts the filters on those loggers.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager

from toolgraph.error_text import describe_error
from toolgraph.redaction import WITHHELD, is_credential_shaped, redact_text

# Enough for any real chain; a longer one is cut rather than walked.
_MAX_LINKS = 64

# The standard record attributes a format string may name, besides the
# message. The names are scrubbed like the message; the rest are numbers.
_NAMES = (
    "name",
    "levelname",
    "pathname",
    "filename",
    "module",
    "funcName",
    "threadName",
    "processName",
    "taskName",
)
_KEPT = (
    *_NAMES,
    "levelno",
    "lineno",
    "created",
    "msecs",
    "relativeCreated",
    "thread",
    "process",
)


def _chain(exc: BaseException) -> list[BaseException]:
    """*exc*, its causes and its group members, each once, outermost first.

    ``__context__`` is followed only when Python did not suppress it, as
    ``raise ... from None`` asks.
    """
    links: list[BaseException] = []
    seen: set[int] = set()
    pending = [exc]
    while pending and len(links) < _MAX_LINKS:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        links.append(current)
        following: list[BaseException] = []
        if isinstance(current, BaseExceptionGroup):
            following.extend(current.exceptions)
        if current.__cause__ is not None:
            following.append(current.__cause__)
        elif current.__context__ is not None and not current.__suppress_context__:
            following.append(current.__context__)
        pending.extend(reversed(following))
    return links


def _credential_shaped_value(values: Iterable[object]) -> bool:
    """Whether any value is credential-shaped and changed by ``repr``.

    `%r`, and an exception's text built from the ``repr`` of its arguments
    (``KeyError``), escape a tab or a no-break space (`\\t`, `\\xa0`) out of
    the scrubber's sight, so such a value is checked as it was before
    formatting. A value ``repr`` leaves as it is reaches the scrubber intact.
    """
    for value in values:
        try:
            text = value if isinstance(value, str) else str(value)
        except Exception:
            continue
        if repr(text)[1:-1] != text and is_credential_shaped(text):
            return True
    return False


def _describe_link(exc: BaseException) -> str:
    try:
        text = WITHHELD if _credential_shaped_value(exc.args) else describe_error(exc)
    except Exception:
        text = "(message could not be rendered)"
    name = type(exc).__name__
    return redact_text(f"{name}: {text}" if text else name)


def render(record: logging.LogRecord) -> str:
    """The text a handler may show for *record*."""
    args = record.args
    values = args.values() if isinstance(args, Mapping) else args or ()
    try:
        message = WITHHELD if _credential_shaped_value(values) else record.getMessage()
    except Exception:
        message = f"log message could not be rendered ({record.name})"
    lines = [redact_text(message)]
    # A logging call stores a tuple; a record built by hand may hold anything.
    info = record.exc_info
    exc = info[1] if isinstance(info, tuple) and len(info) > 1 else None
    if isinstance(exc, BaseException):
        lines.extend(_describe_link(link) for link in _chain(exc))
    return "\n".join(lines)


def _rebuilt(record: logging.LogRecord, msg: str, args: tuple[object, ...]) -> logging.LogRecord:
    """A new record carrying *msg*, *args* and only the standard attributes.

    The traceback, cached exception text, stack and any ``extra`` attribute
    (``color_message`` among them) are not carried over.
    """
    kept = {key: getattr(record, key) for key in _KEPT if hasattr(record, key)}
    for key in _NAMES:
        if isinstance(kept.get(key), str):
            kept[key] = redact_text(kept[key])
    # Not makeLogRecord: it calls the configured record factory, which may
    # add attributes back.
    safe = logging.LogRecord(record.name, record.levelno, "", 0, msg, args, None)
    safe.__dict__.update(kept)
    return safe


class RedactingFilter(logging.Filter):
    """Hand the handler a rebuilt record; never alter the original.

    A record is shared by every handler it reaches, so a new one keeps this
    handler's view from leaking into another's.
    """

    def filter(self, record: logging.LogRecord) -> logging.LogRecord:
        # An exception raised here would escape the logging call itself.
        try:
            text = render(record)
        except Exception:
            text = "log record could not be rendered"
        return _rebuilt(record, text, ())


# The access line uvicorn logs: client address, method, path with its query
# string, HTTP version, status code. Its AccessFormatter builds the request
# line from these five arguments, not from the message.
_ACCESS_FORMAT = '%s - "%s %s HTTP/%s" %d'


class AccessLogFilter(logging.Filter):
    """Withhold every text field of an access line that carries a credential.

    The whole line is judged once, as uvicorn formatted it from the client's
    request: a line ``redact_text`` would leave as it is passes unchanged;
    any other line keeps only its status code. Scrubbing field by field
    misses a credential the fields form together (a ``Bearer`` method before
    a ``/<token>`` path) or one sent as a proxied client address.

    A record whose message is not uvicorn's access format, whose text fields
    are not plain ``str``, or whose status is not an ``int`` is withheld too:
    AccessFormatter prints the arguments whatever the message says, through
    f-strings that a subclass's ``__format__`` can make differ from the ``%``
    text judged here. One whose arguments the access formatter cannot read is dropped,
    since a formatter error prints the logging call's traceback and source.
    """

    def filter(self, record: logging.LogRecord) -> logging.LogRecord | bool:
        args = record.args
        if not (isinstance(args, tuple) and len(args) == 5):
            return False
        try:
            message = record.getMessage()
            # The MCP SDK answers with HTTPStatus members; the formatter reads
            # the status through int(), so its digits are all that prints.
            status = int(args[4]) if isinstance(args[4], int) else None
            clean = (
                record.msg == _ACCESS_FORMAT
                and status is not None
                and all(type(arg) is str for arg in args[:4])
                and not _credential_shaped_value(args)
                and redact_text(message) == message
            )
        except Exception:
            return False
        if clean:
            fields = (*args[:4], status)
        else:
            fields = ("-", "-", WITHHELD, "-", 0 if status is None else status)
        return _rebuilt(record, _ACCESS_FORMAT, fields)


def install(handler: logging.Handler) -> None:
    """Add a ``RedactingFilter`` to *handler* unless it has one."""
    if not any(isinstance(f, RedactingFilter) for f in handler.filters):
        handler.addFilter(RedactingFilter())


@contextmanager
def filtering_root_handlers_if_unconfigured() -> Iterator[None]:
    """Filter the root handlers the block configures, if there were none.

    When the root logger already had a handler, the program owns logging
    and nothing is filtered: its formatters may name attributes (``extra``)
    that a filtered record no longer carries. ``basicConfig`` likewise acts
    only on a root logger without handlers.
    """
    configured = bool(logging.getLogger().handlers)
    yield
    if not configured:
        for handler in logging.getLogger().handlers:
            install(handler)


class _DropFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return False


def filter_uvicorn_loggers() -> None:
    """Filter what uvicorn logs while it serves the MCP app over HTTP.

    uvicorn logs on its own loggers with its own handlers, which it replaces
    when it starts; a filter on the logger itself survives that. A program
    that runs the app under its own server owns that logging instead.

    ``uvicorn.asgi`` logs whole ASGI scopes and messages, headers included,
    at uvicorn's TRACE level, which the MCP server's log level cannot select;
    its records are dropped rather than scrubbed.
    """
    for name, kind in (
        ("uvicorn.access", AccessLogFilter),
        ("uvicorn.error", RedactingFilter),
        ("uvicorn.asgi", _DropFilter),
    ):
        logger = logging.getLogger(name)
        if not any(isinstance(f, kind) for f in logger.filters):
            logger.addFilter(kind())


def install_on_last_resort() -> None:
    """Filter the handler Python uses when no handler is configured.

    A program may set ``logging.lastResort`` to None; then there is nothing
    to filter.
    """
    if logging.lastResort is not None:
        install(logging.lastResort)
