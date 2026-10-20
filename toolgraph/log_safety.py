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
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from toolgraph.error_text import describe_error
from toolgraph.redaction import redact_text

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


def _describe_link(exc: BaseException) -> str:
    try:
        text = describe_error(exc)
    except Exception:
        text = "(message could not be rendered)"
    name = type(exc).__name__
    return redact_text(f"{name}: {text}" if text else name)


def render(record: logging.LogRecord) -> str:
    """The text a handler may show for *record*."""
    try:
        message = record.getMessage()
    except Exception:
        message = f"log message could not be rendered ({record.name})"
    lines = [redact_text(message)]
    # A logging call stores a tuple; a record built by hand may hold anything.
    info = record.exc_info
    exc = info[1] if isinstance(info, tuple) and len(info) > 1 else None
    if isinstance(exc, BaseException):
        lines.extend(_describe_link(link) for link in _chain(exc))
    return "\n".join(lines)


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
        kept = {key: getattr(record, key) for key in _KEPT if hasattr(record, key)}
        for key in _NAMES:
            if isinstance(kept.get(key), str):
                kept[key] = redact_text(kept[key])
        # Not makeLogRecord: it calls the configured record factory, which may
        # add attributes back.
        safe = logging.LogRecord(record.name, record.levelno, "", 0, text, (), None)
        safe.__dict__.update(kept)
        return safe


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


def install_on_last_resort() -> None:
    """Filter the handler Python uses when no handler is configured.

    A program may set ``logging.lastResort`` to None; then there is nothing
    to filter.
    """
    if logging.lastResort is not None:
        install(logging.lastResort)
