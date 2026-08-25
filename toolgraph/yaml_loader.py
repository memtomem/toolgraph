"""Fail-closed YAML loading for operator-authored configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


class UniqueKeySafeLoader(yaml.SafeLoader):
    """SafeLoader variant that rejects duplicate mapping keys at any depth."""


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
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
)


MAX_YAML_BYTES = 5_000_000


def load_yaml_mapping(path: Path) -> dict[str, Any]:
    """Load a size-bounded YAML mapping; reject duplicate keys, non-mapping roots."""
    if path.stat().st_size > MAX_YAML_BYTES:
        raise ValueError(
            f"refusing to load {path}: exceeds the {MAX_YAML_BYTES}-byte limit"
        )
    loader = UniqueKeySafeLoader(path.read_text(encoding="utf-8"))
    try:
        value = loader.get_single_data()
    finally:
        loader.dispose()
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"expected a YAML mapping at document root: {path}")
    return value
