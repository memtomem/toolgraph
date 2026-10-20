"""YAML errors print a position and a fixed text, never the document (#223)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from toolgraph import cli
from toolgraph.error_text import describe_error
from toolgraph.yaml_loader import UniqueKeySafeLoader, YAMLContentError, load_yaml_mapping

M = "neo4j://u:QX ZQ@h/r"

# (document, rendered error). Documents plant QX/ZQ where PyYAML's own text
# would quote it: the source line, an alias, anchor, tag or tag handle, a
# character, a key, or a value a constructor rejects. An empty int and deep
# nesting carry no marker: they pin the text for plain Python exceptions
# (IndexError, RecursionError) raised while loading.
CORPUS = {
    "duplicate key": (
        f'agents: [a]\ngoverned_by:\n  "{M}": [p]\n  "{M}": [p]\n',
        "invalid YAML at line 4, column 3: duplicate key",
    ),
    "duplicate key through an alias": (
        "? &k QXZQ\n: one\n? *k\n: two\n",
        "invalid YAML at line 1, column 3: duplicate key",
    ),
    "unterminated quote": (
        f'servers:\n  - env: {{TOKEN: "{M}}}\n',
        "invalid YAML at line 3, column 1: could not be scanned",
    ),
    "bad indent": (
        f"servers:\n  - headers:\n      Authorization: Bearer {M}\n     bad: [\n",
        "invalid YAML at line 4, column 6: could not be parsed",
    ),
    "tab indent": (
        f'servers:\n\t- env: {{TOKEN: "{M}"}}\n',
        "invalid YAML at line 2, column 1: could not be scanned",
    ),
    "unclosed flow": (
        f"servers: [{{env: {{TOKEN: {M}}}}}\n",
        "invalid YAML at line 2, column 1: could not be parsed",
    ),
    "undefined alias": (
        "agents: [*QXZQalias]\n",
        "invalid YAML at line 1, column 10: alias, anchor or document error",
    ),
    "duplicate anchor": (
        "agents: [&QXZQ a, &QXZQ b]\n",
        "invalid YAML at line 1, column 19: alias, anchor or document error",
    ),
    "two documents": (
        "agents: [QXZQ]\n---\nagents: [b]\n",
        "invalid YAML at line 2, column 1: alias, anchor or document error",
    ),
    "unknown tag": (
        "agents: !QXZQtag [a]\n",
        "invalid YAML at line 1, column 9: could not be constructed",
    ),
    "python tag": (
        "agents: !!python/name:QXZQ.x [a]\n",
        "invalid YAML at line 1, column 9: could not be constructed",
    ),
    "undefined tag handle": (
        "agents: !qxzq!x [a]\n",
        "invalid YAML at line 1, column 9: could not be parsed",
    ),
    "duplicate tag handle": (
        "%TAG !q! tag:QXZQ,2000:\n%TAG !q! tag:QXZQ,2000:\n---\nagents: [a]\n",
        "invalid YAML at line 2, column 1: could not be parsed",
    ),
    "unknown escape": (
        'agents: ["QX\\qZQ"]\n',
        "invalid YAML at line 1, column 14: could not be scanned",
    ),
    "mapping values here": (
        "agents: QX ZQ: password: x\n",
        "invalid YAML at line 1, column 14: could not be scanned",
    ),
    "unhashable key": (
        "? [QXZQ]\n: 1\n",
        "invalid YAML at line 1, column 3: could not be constructed",
    ),
    "invalid binary": (
        "x: !!binary 'Q\u00e9XZQ'\n",
        "invalid YAML at line 1, column 4: could not be constructed",
    ),
    "int value": ("x: !!int QXZQ\n", "invalid YAML: a value could not be read"),
    "float value": ("x: !!float QXZQ\n", "invalid YAML: a value could not be read"),
    "bool value": ("x: !!bool QXZQ\n", "invalid YAML: a value could not be read"),
    "empty int": ("x: !!int ''\n", "invalid YAML: a value could not be read"),
    "timestamp value": ("x: !!timestamp QXZQ\n", "invalid YAML: a value could not be read"),
    "escape out of range": (
        'x: "\\U00110000QXZQ"\n',
        "invalid YAML: a value could not be read",
    ),
    "deep nesting": (
        "x: " + "[" * 20000 + "]" * 20000 + "\n",
        "invalid YAML: a value could not be read",
    ),
    "control character": (
        "x: QXZQ\x01\n",
        "invalid YAML: unacceptable character (position 7)",
    ),
    "NUL": (
        "agents: [QXZQ\x00]\n",
        "invalid YAML: unacceptable character (position 13)",
    ),
}


def _assert_hidden(text: str) -> None:
    assert "qx" not in text.lower()
    assert "zq" not in text.lower()


def _load_error(tmp_path: Path, document: str | bytes) -> Exception:
    path = tmp_path / "config.yaml"
    path.write_bytes(document if isinstance(document, bytes) else document.encode("utf-8"))
    with pytest.raises(Exception) as raised:
        load_yaml_mapping(path)
    return raised.value


@pytest.mark.parametrize(("document", "expected"), CORPUS.values(), ids=CORPUS.keys())
def test_a_yaml_error_prints_its_position_and_a_fixed_text(tmp_path, document, expected):
    text = describe_error(_load_error(tmp_path, document))

    assert text == expected
    _assert_hidden(text)


def test_invalid_utf8_names_the_offset_only(tmp_path):
    exc = _load_error(tmp_path, b"agents: [QXZQ\xff]\n")

    assert isinstance(exc, YAMLContentError)
    assert describe_error(exc) == "invalid YAML: not valid UTF-8 (byte offset 13)"


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (
            yaml.MarkedYAMLError("QXZQ context", yaml.Mark("QXZQ", 0, 4, 2, None, None)),
            "invalid YAML at line 5, column 3: could not be read",
        ),
        (
            yaml.scanner.ScannerError("QXZQ", None, "QXZQ problem", None, "QXZQ note"),
            "invalid YAML: could not be scanned",
        ),
        (
            yaml.constructor.ConstructorError(None, None, "found duplicate key 'QXZQ'", None),
            "invalid YAML: could not be constructed",
        ),
    ],
    ids=["context-mark-only", "no-mark", "duplicate-text-not-ours"],
)
def test_a_marked_error_never_prints_its_own_text(exc, expected):
    assert describe_error(exc) == expected


@pytest.mark.parametrize("stage", ["get_single_data", "dispose"])
def test_a_failure_anywhere_in_the_loader_is_contained(tmp_path, monkeypatch, stage):
    def fail(self, *args, **kwargs):
        raise RuntimeError("QXZQ")

    monkeypatch.setattr(UniqueKeySafeLoader, stage, fail)

    exc = _load_error(tmp_path, "agents: [a]\n")

    assert isinstance(exc, YAMLContentError)
    assert describe_error(exc) == "invalid YAML: a value could not be read"


def test_library_callers_keep_pyyaml_errors_that_have_a_position(tmp_path):
    exc = _load_error(tmp_path, "agents: [a]\nagents: [b]\n")

    assert isinstance(exc, yaml.constructor.ConstructorError)
    assert exc.problem == "found duplicate key"
    assert exc.problem_mark.line == 1


# --- CLI print sites ------------------------------------------------------

NATIVE = "agents: [a]\nx: !!bool QXZQ\n"
MARKED = "agents: [*QXZQ]\n"


def _invoke(tmp_path: Path, command: str, document: str):
    path = tmp_path / "config.yaml"
    path.write_text(document, encoding="utf-8")
    args = {
        "crawl": ["crawl", "--servers", str(path)],
        "ingest-manifest": ["ingest-manifest", "--governance", str(path)],
        "policy impact": ["policy", "impact", str(path), "s::t", "--agent", "a"],
        "policy propose": ["policy", "propose", str(path), str(tmp_path / "observed.jsonl")],
    }[command]
    return path, CliRunner().invoke(cli.app, args)


PREFIX = {
    "crawl": "Invalid servers config at {path}: ",
    "ingest-manifest": "Invalid governance config at {path}: ",
    "policy impact": "ERROR: ",
    "policy propose": "ERROR: ",
}


@pytest.mark.parametrize("command", PREFIX)
@pytest.mark.parametrize(
    ("document", "detail"),
    [
        (NATIVE, "invalid YAML: a value could not be read"),
        (MARKED, "invalid YAML at line 1, column 10: alias, anchor or document error"),
    ],
    ids=["native", "marked"],
)
def test_every_cli_site_prints_one_line(tmp_path, monkeypatch, command, document, detail):
    monkeypatch.setattr(cli, "crawl_all", lambda *a, **k: pytest.fail("must not crawl"))

    path, result = _invoke(tmp_path, command, document)

    assert result.exit_code == 1
    assert result.output.strip() == PREFIX[command].format(path=path) + detail
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_policy_impact_prints_no_traceback_in_a_real_process(tmp_path):
    path = tmp_path / "governance.yaml"
    path.write_text(NATIVE, encoding="utf-8")

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "toolgraph.cli",
            "policy",
            "impact",
            str(path),
            "s::t",
            "--agent",
            "a",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 1
    assert proc.stdout == ""
    assert proc.stderr == "ERROR: invalid YAML: a value could not be read\n"
