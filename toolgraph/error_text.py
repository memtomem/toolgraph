"""Printable text for errors, with pydantic validation errors rebuilt from trusted parts.

A ``ValidationError`` describes its input. ``hide_input_in_errors`` drops the
input value, but the error location still carries every mapping key and
unknown field name on the way to the failure, and models outside Toolgraph
(the MCP SDK's) do not hide the input at all. Scrubbing the rendered text
afterwards only removes what the prose rule recognizes.

So a validation error is rendered here from parts that cannot come from the
input:

- the location, walked against the schema of the model the caller validated:
  field names, list indexes and union labels that match the schema are kept,
  and anything else is printed as ``<key>``. Without a model, no location;
- the message, chosen by error type from a fixed table. Pydantic's own text is
  used only for types whose text is built from the schema, and only with a
  model; a validator's message only when it raised ``ValueFreeError``;
- the title and the error type, which are set by code.

A caller passes ``model`` only for a ``model.model_validate`` call in the same
``try``: an error title is not evidence of which model raised it.

A YAML error with a position is rendered as that position and a fixed text by
error class: PyYAML's own text quotes the source line, and its messages name
aliases, anchors, tags, characters and keys from the document.

Two more errors are rendered from their producer's fixed text, because their
message holds a shortened repr of the input, which the scrubber cannot read
back as a literal: ``int()`` prints at most 200 characters of the repr, so a
longer one loses its closing quote, and the MCP SDK reports a reply it could not parse with the
pydantic error text, which shortens ``input_value`` in the middle.
"""

from __future__ import annotations

import os
import re
import types
import typing
from typing import Any

import yaml
from mcp.shared.exceptions import MCPError
from mcp_types import PARSE_ERROR
from pydantic import BaseModel, ValidationError

from toolgraph.errors import ValueFreeError
from toolgraph.redaction import PATH_WITHHELD, display_path, redact_text
from toolgraph.yaml_loader import DUPLICATE_KEY

MASK = "<key>"
UNPARSED_REPLY = "the server's reply could not be parsed (JSON-RPC parse error)"

_INT_LITERAL = re.compile(r"invalid literal for int\(\) with base (\d+): ")

_FIXED_MESSAGES = {
    "missing": "field required",
    "extra_forbidden": "unknown field",
    "string_type": "expected a string",
    "list_type": "expected a list",
    "dict_type": "expected a mapping",
    "model_type": "expected a mapping",
    "model_attributes_type": "expected a mapping",
    "bool_type": "expected a boolean",
    "bool_parsing": "expected a boolean",
    "int_type": "expected an integer",
    "int_parsing": "expected an integer",
    "int_from_float": "expected an integer",
    "none_required": "expected null",
}

# Pydantic builds these messages from the schema's constraint (``too_short``
# and ``too_long`` add a count), never from the input.
_SCHEMA_MESSAGES = frozenset(
    {
        "literal_error",
        "string_pattern_mismatch",
        "string_too_short",
        "string_too_long",
        "too_short",
        "too_long",
        "greater_than_equal",
    }
)


def describe_error(exc: BaseException, model: type[BaseModel] | None = None) -> str:
    """Printable text for ``exc``; ``model`` is the one validated, if known."""
    if isinstance(exc, ValidationError):
        return describe_validation_error(exc, model)
    if isinstance(exc, yaml.MarkedYAMLError):
        return describe_yaml_error(exc)
    # Only Python's own OSError text is rebuilt; a subclass that formats
    # itself (``HTTPError``) keeps its text and goes to the scrubber.
    if (
        isinstance(exc, OSError)
        and exc.filename is not None
        and type(exc).__str__ is OSError.__str__
    ):
        return describe_os_error(exc)
    if base := _int_literal_base(exc):
        return f"invalid literal for int() with base {base} (value not shown)"
    # By code, not by the SDK's wording: a server's own parse error loses its
    # text too.
    if isinstance(exc, MCPError) and exc.code == PARSE_ERROR:
        return UNPARSED_REPLY
    return redact_text(exc)


def _int_literal_base(exc: BaseException) -> str | None:
    """The base named by ``int()``'s error for a string it could not read.

    CPython formats it as ``invalid literal for int() with base %d: %.200R``
    for str and bytes alike, and raises exactly ``ValueError``.
    """
    if type(exc) is not ValueError or len(exc.args) != 1 or not isinstance(exc.args[0], str):
        return None
    match = _INT_LITERAL.match(exc.args[0])
    return match.group(1) if match else None


def describe_os_error(exc: OSError) -> str:
    """``str(exc)``, with each credential-shaped file name replaced by ``PATH_WITHHELD``.

    ``str(OSError)`` writes a file name with ``repr``, which escapes a tab or
    a no-break space (`\\t`, `\\xa0`) into a form the scrubber cannot read.
    Python formats a copy, so every other part reads exactly as it would. The
    copy is built from errno and reason alone and given the names afterwards:
    a third constructor argument can mean something else (``BlockingIOError``
    reads an integer there as ``characters_written``).
    """
    copy = OSError(exc.errno, exc.strerror)
    copy.filename = _shown_name(exc.filename)
    if exc.filename2 is not None:
        copy.filename2 = _shown_name(exc.filename2)
    if getattr(exc, "winerror", None) is not None:  # Windows: `[WinError N]`
        copy.winerror = exc.winerror
    return redact_text(copy)


def _shown_name(name: Any) -> Any:
    """*name* as it is, or ``PATH_WITHHELD`` when it is credential-shaped.

    A bytes name is checked decoded, as the OS reads it.
    """
    text = os.fsdecode(name) if isinstance(name, bytes) else name
    if isinstance(text, str | os.PathLike) and display_path(text) == PATH_WITHHELD:
        return PATH_WITHHELD
    return name


def describe_yaml_error(exc: yaml.MarkedYAMLError) -> str:
    """``invalid YAML at line L, column C: <what failed>``, nothing from the document."""
    mark = exc.problem_mark or exc.context_mark
    where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark is not None else ""
    return f"invalid YAML{where}: {_yaml_failure(exc)}"


def _yaml_failure(exc: yaml.MarkedYAMLError) -> str:
    if isinstance(exc, yaml.scanner.ScannerError):
        return "could not be scanned"
    if isinstance(exc, yaml.parser.ParserError):
        return "could not be parsed"
    if isinstance(exc, yaml.composer.ComposerError):
        return "alias, anchor or document error"
    if isinstance(exc, yaml.constructor.ConstructorError):
        return "duplicate key" if exc.problem == DUPLICATE_KEY else "could not be constructed"
    return "could not be read"


def describe_validation_error(exc: ValidationError, model: type[BaseModel] | None = None) -> str:
    """One line naming every error by trusted location, message and type."""
    errors = exc.errors(include_input=False, include_url=False)
    rendered = []
    for error in errors:
        location, message = render_one(error, model)
        text = f"{message} [type={error['type']}]"
        rendered.append(f"{location}: {text}" if location else text)
    count = len(errors)
    noun = "error" if count == 1 else "errors"
    return f"{count} validation {noun} for {exc.title}: " + "; ".join(rendered)


def render_one(error: Any, model: type[BaseModel] | None) -> tuple[str, str]:
    """``(location, message)`` for one entry of ``ValidationError.errors()``."""
    location = _location(error["loc"], model) if model is not None else ""
    return location, _message(error, trusted_schema=model is not None)


def _message(error: Any, *, trusted_schema: bool) -> str:
    kind = error["type"]
    if kind in _FIXED_MESSAGES:
        return _FIXED_MESSAGES[kind]
    if kind in _SCHEMA_MESSAGES and trusted_schema:
        return error["msg"]
    if kind == "value_error":
        cause = (error.get("ctx") or {}).get("error")
        if isinstance(cause, ValueFreeError):
            return str(cause)
    return "invalid value"


def _location(loc: tuple[int | str, ...], model: type[BaseModel]) -> str:
    parts: list[str] = []
    current: Any = model
    for index, part in enumerate(loc):
        members = _members(current)
        if len(members) == 1:
            current = members[0]
        if len(members) > 1:
            labels = {label: member for member in members if (label := _union_label(member))}
            if isinstance(part, str) and part in labels:
                parts.append(part)
                current = labels[part]
                continue
        elif isinstance(current, type) and issubclass(current, BaseModel):
            if isinstance(part, str) and part in current.model_fields:
                parts.append(part)
                current = current.model_fields[part].annotation
                continue
        elif typing.get_origin(current) is list:
            if isinstance(part, int) and not isinstance(part, bool):
                parts.append(str(part))
                (current,) = typing.get_args(current)
                continue
        elif current is dict or typing.get_origin(current) is dict:
            parts.append(MASK)
            rest = loc[index + 1 :]
            if rest == ("[key]",):
                parts.append("[key]")
                return ".".join(parts)
            args = typing.get_args(current)
            current = args[1] if args else Any
            continue
        # The part does not match the schema here, so nothing after it can be
        # placed either: mask it and every part that follows.
        parts.extend(MASK for _ in loc[index:])
        return ".".join(parts)
    return ".".join(parts)


def _members(tp: Any) -> list[Any]:
    """The non-None members of a union, else ``[tp]``.

    Pydantic adds no location part for ``X | None``; a union of two or more
    other members adds the label of the member it tried.
    """
    if typing.get_origin(tp) not in (typing.Union, types.UnionType):
        return [tp]
    return [member for member in typing.get_args(tp) if member is not type(None)]


def _union_label(member: Any) -> str | None:
    # Only the labels pydantic gives the member types Toolgraph's models use.
    if member is str:
        return "str"
    if isinstance(member, type) and issubclass(member, BaseModel):
        return member.__name__
    return None
