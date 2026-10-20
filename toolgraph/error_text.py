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
"""

from __future__ import annotations

import types
import typing
from typing import Any

from pydantic import BaseModel, ValidationError

from toolgraph.errors import ValueFreeError
from toolgraph.redaction import redact_text

MASK = "<key>"

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
    return redact_text(exc)


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
