"""Fail-closed YAML loading for operator-authored configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


class UniqueKeySafeLoader(yaml.SafeLoader):
    """SafeLoader variant that rejects duplicate mapping keys at any depth."""


class YAMLContentError(ValueError):
    """A document PyYAML could not read, with a message that quotes none of it.

    PyYAML reports some failures as plain Python exceptions whose messages
    quote the value (``!!int x`` raises ``ValueError: invalid literal for
    int() ... 'x'``, ``!!bool x`` a ``KeyError``), and some as unmarked
    errors that name a character or byte. These become this error.
    """


# Fixed text: the key is in the error's mark, and printing it would print the
# document (error_text renders the position only).
DUPLICATE_KEY = "found duplicate key"


def _construct_mapping(
    loader: UniqueKeySafeLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable key",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                DUPLICATE_KEY,
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
)


MAX_YAML_BYTES = 5_000_000


def load_yaml_mapping(path: Path) -> dict[str, Any]:
    """Load a size-bounded YAML mapping; reject duplicate keys, non-mapping roots.

    A document PyYAML cannot read raises its ``MarkedYAMLError`` when the
    failure marks a line and column, and ``YAMLContentError`` otherwise
    (including ``ReaderError``, which carries only a character position).
    """
    # Bounded read from one open descriptor (stat-then-read is a TOCTOU).
    with path.open("rb") as handle:
        raw = handle.read(MAX_YAML_BYTES + 1)
    if len(raw) > MAX_YAML_BYTES:
        raise ValueError(f"refusing to load {path}: exceeds the {MAX_YAML_BYTES}-byte limit")
    try:
        loader = UniqueKeySafeLoader(raw.decode("utf-8"))
        try:
            value = loader.get_single_data()
        finally:
            loader.dispose()
    except yaml.MarkedYAMLError:
        raise
    except UnicodeDecodeError as exc:
        raise YAMLContentError(f"invalid YAML: not valid UTF-8 (byte offset {exc.start})") from None
    except yaml.reader.ReaderError as exc:
        raise YAMLContentError(
            f"invalid YAML: unacceptable character (position {exc.position})"
        ) from None
    except Exception:
        raise YAMLContentError("invalid YAML: a value could not be read") from None
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"expected a YAML mapping at document root: {path}")
    return value
