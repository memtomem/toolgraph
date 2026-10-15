"""Keep the notebooks under docs/notebooks honest.

The committed notebook carries its executed outputs so it reads on GitHub.
The default suite checks those outputs: the notebook is well formed and fully
executed, no output names a machine path, every command it runs exists, and
the decisions it shows are the right ones, parsed row by row.

Executing the notebook needs nbclient and ipykernel installed next to a wheel
of this tree, which the dev environment does not have. That test is opt-in:
set ``TOOLGRAPH_NOTEBOOK_PYTHON`` to such an environment's interpreter, as the
CI ``notebooks`` job does. It runs the same checks on the fresh outputs.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests._helpers import ROOT
from toolgraph.cli import app

NOTEBOOK_DIR = ROOT / "docs" / "notebooks"
FIRST = NOTEBOOK_DIR / "01-first-policy-bundle.ipynb"
NOTEBOOK_PYTHON = os.environ.get("TOOLGRAPH_NOTEBOOK_PYTHON")

READ = "policy-gateway::read_note"
PUBLISH = "policy-gateway::publish_note"
DRAFTS = "file:///demo/drafts"
POLICY = "draft-publish-deny"

# A drive letter needs a non-letter before it, or `file:///demo` would match.
_MACHINE_PATH = re.compile(
    r"/(?:Users|home|private|tmp|opt|workspace)/|/var/folders/|(?<![A-Za-z])[A-Za-z]:[\\/]"
)
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _code_cells(nb: dict) -> list[dict]:
    return [cell for cell in nb["cells"] if cell["cell_type"] == "code"]


def _cell(nb: dict, tag: str) -> dict:
    (cell,) = [c for c in _code_cells(nb) if tag in c["metadata"].get("tags", [])]
    return cell


def _stdout(cell: dict) -> str:
    return "".join(
        "".join(out["text"])
        for out in cell["outputs"]
        if out["output_type"] == "stream" and out["name"] == "stdout"
    )


def _lines(cell: dict) -> list[str]:
    return [line.strip() for line in _stdout(cell).splitlines()]


def _json_values(cell: dict) -> list:
    """Every JSON document a cell printed, in order.

    ``run`` prints a command's output verbatim, and toolgraph prints JSON
    starting at column 0, so each document starts a line with ``[`` or ``{``.
    """
    text = _stdout(cell)
    decoder = json.JSONDecoder()
    values, index = [], 0
    while index < len(text):
        if text[index] in "[{" and (index == 0 or text[index - 1] == "\n"):
            value, index = decoder.raw_decode(text, index)
            values.append(value)
        else:
            index += 1
    return values


def _check_structure(nb: dict) -> None:
    assert nb["nbformat"] == 4
    code = _code_cells(nb)
    assert [cell["execution_count"] for cell in code] == list(range(1, len(code) + 1))
    assert all(cell["outputs"] for cell in code), "every code cell prints something"
    errors = [out for cell in code for out in cell["outputs"] if out["output_type"] == "error"]
    assert errors == []
    streams = {
        out["name"] for cell in code for out in cell["outputs"] if out["output_type"] == "stream"
    }
    assert streams == {"stdout"}, "run() prints stderr into stdout; a stderr stream is a stray"


def _strings(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [s for item in value for s in _strings(item)]
    if isinstance(value, dict):
        return [s for item in value.values() for s in _strings(item)]
    return []


def _check_no_machine_paths(nb: dict) -> None:
    exposed = [nb["metadata"]]
    for cell in nb["cells"]:
        exposed.append(cell["metadata"])
        exposed.extend(cell.get("outputs", []))
    prefixes = {sys.prefix, str(ROOT), str(Path.home()), tempfile.gettempdir()}
    leaks = [
        text
        for text in _strings(exposed)
        if _MACHINE_PATH.search(text) or any(prefix in text for prefix in prefixes)
    ]
    assert leaks == []


def _check_outputs(nb: dict) -> None:
    """The decisions the notebook shows, checked against what its prose says."""
    doctor = _lines(_cell(nb, "tg-doctor"))
    checks = [line for line in doctor if line.startswith("[")]
    assert [line.split("]")[0] for line in checks] == ["[PASS"] * 4
    assert doctor[-1] == "Overall status: HEALTHY"
    instance = next(line for line in doctor if line.startswith("- instance_id: "))
    instance_id = instance.removeprefix("- instance_id: ")
    assert _UUID.fullmatch(instance_id)

    assert "[ok] policy-gateway (2 tools, 1 resources)" in _lines(_cell(nb, "tg-crawl"))

    dry_run = _lines(_cell(nb, "tg-dry-run"))
    assert dry_run[0] == "DRY-RUN: manifest at governance.yaml would apply cleanly"
    assert "Data access: +2 / -0" in dry_run
    assert "Provenance modified: 0" in dry_run
    assert _lines(_cell(nb, "tg-ingest"))[0] == "Ingested governance from governance.yaml"
    assert _json_values(_cell(nb, "tg-unbacked-before")) == [[]]

    (eligible,) = _json_values(_cell(nb, "tg-eligible"))
    assert eligible["eligible"] == [READ]
    (rejected,) = eligible["rejected"]
    assert (rejected["tool_key"], rejected["reason"]) == (PUBLISH, "DENY_VIOLATION")
    assert [POLICY in path for path in rejected["paths"]] == [True]

    (explained,) = _json_values(_cell(nb, "tg-explain"))
    assert (explained["tool_key"], explained["decision"]) == (PUBLISH, "rejected")

    (compiled,) = _json_values(_cell(nb, "tg-compile"))
    assert compiled["output"] == ".toolgraph/policy-bundle.json"
    assert (compiled["agent"], compiled["profile"]) == ("vibe-coder", "review")
    assert (compiled["eligible"], compiled["rejected"]) == (1, 1)
    assert re.fullmatch(r"[0-9a-f]{64}", compiled["bundle_digest"])
    assert compiled["graph_state"]["instance_id"] == instance_id

    assert [line.split() for line in _lines(_cell(nb, "tg-bundle"))] == [
        ["digest", "matches:", "True"],
        ["graph", "state", "matches:", "True"],
        [],
        [PUBLISH, "rejected", "DENY_VIOLATION"],
        [READ, "eligible"],
    ]

    edited = _lines(_cell(nb, "tg-provenance-dry-run"))
    assert "Provenance modified: 1" in edited
    assert "Data access: +0 / -0" in edited
    (unbacked,) = _json_values(_cell(nb, "tg-unbacked-edited"))
    assert [(e["edge_type"], e["src"], e["dst"]) for e in unbacked] == [("WRITES", PUBLISH, DRAFTS)]
    assert _json_values(_cell(nb, "tg-unbacked-restored")) == [[]]

    (unsafe,) = _json_values(_cell(nb, "tg-unsafe"))
    assert [(r["tool_key"], r["policy"], r["classification"]) for r in unsafe] == [
        (PUBLISH, POLICY, "violation")
    ]
    (blast,) = _json_values(_cell(nb, "tg-blast"))
    assert blast["found"] is True
    assert [(r["agent"], r["tool_key"], r["resource"]) for r in blast["impacted"]] == [
        ("vibe-coder", PUBLISH, DRAFTS)
    ]
    assert _json_values(_cell(nb, "tg-empty-audits")) == [[], []]

    assert _lines(_cell(nb, "tg-cleanup")) == ["Removed <sandbox>"]


def _toolgraph_calls(nb: dict) -> list[list[str]]:
    calls = []
    for cell in _code_cells(nb):
        for node in ast.walk(ast.parse("".join(cell["source"]))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in {"run", "run_json"}
                # Skips run_json's own forwarding call, run(*args).
                and all(isinstance(arg, ast.Constant) for arg in node.args)
            ):
                calls.append([arg.value for arg in node.args])
    return calls


def test_committed_notebook_is_fully_executed():
    _check_structure(_load(FIRST))


def test_committed_notebook_carries_no_execution_timestamps():
    # Timestamps change on every regeneration and bury the output diff; the
    # command in docs/notebooks/README.md executes with record_timing=False.
    timed = [cell["id"] for cell in _code_cells(_load(FIRST)) if "execution" in cell["metadata"]]
    assert timed == []


def test_committed_outputs_carry_no_machine_paths():
    _check_no_machine_paths(_load(FIRST))


def test_committed_outputs_show_the_documented_decisions():
    _check_outputs(_load(FIRST))


@pytest.mark.parametrize(
    "text",
    [
        "/Users/someone/x",
        "/home/runner/work",
        "/private/var/folders/x",
        "/tmp/abc",
        "C:/data/secret",
        "C:\\123\\secret",
        'at "D:\\work"',
    ],
)
def test_machine_path_pattern_catches(text):
    assert _MACHINE_PATH.search(text)


@pytest.mark.parametrize(
    "text", ["file:///demo/drafts", "<sandbox>/quickstart", ".toolgraph/config.json"]
)
def test_machine_path_pattern_spares(text):
    assert not _MACHINE_PATH.search(text)


def test_every_command_the_notebook_runs_exists():
    calls = [args for args in _toolgraph_calls(_load(FIRST)) if not args[0].startswith("-")]
    assert len(calls) == 19
    runner = CliRunner()
    for args in calls:
        # --help is eager, but click parses the whole line first, so an
        # unknown command or option still fails here.
        result = runner.invoke(app, [*args, "--help"])
        assert result.exit_code == 0, (args, result.output)


def test_guides_link_the_notebook():
    link = "docs/notebooks/01-first-policy-bundle.ipynb"
    assert link in (ROOT / "README.md").read_text(encoding="utf-8")
    for guide in ("beginner-guide.md", "ko-beginner-guide.md"):
        text = (ROOT / "docs" / guide).read_text(encoding="utf-8")
        assert "notebooks/01-first-policy-bundle.ipynb" in text, guide


@pytest.mark.skipif(
    not NOTEBOOK_PYTHON,
    reason="needs TOOLGRAPH_NOTEBOOK_PYTHON: a wheel install with nbclient and ipykernel",
)
def test_notebook_executes_from_an_installed_wheel(tmp_path):
    python = Path(NOTEBOOK_PYTHON)
    nb = _load(FIRST)
    # Clear first: a cell nbclient skips would otherwise keep its committed
    # output and pass the checks below without running.
    for cell in _code_cells(nb):
        cell["outputs"], cell["execution_count"] = [], None
    nb["cells"].append(
        {
            "cell_type": "code",
            "id": "tg-probe",
            "metadata": {"tags": ["tg-probe"]},
            "source": "import sys\nprint(sys.prefix)",
            "outputs": [],
            "execution_count": None,
        }
    )
    work = tmp_path / "work"
    work.mkdir()
    target = work / FIRST.name
    target.write_text(json.dumps(nb), encoding="utf-8")

    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("JUPYTER_", "TOOLGRAPH_", "NEO4J_"))
        and key not in {"IPYTHONDIR", "PYTHONPATH", "PYTHONHOME"}
    }
    env["JUPYTER_CONFIG_DIR"] = str(tmp_path / "jupyter-config")
    env["IPYTHONDIR"] = str(tmp_path / "ipython")
    done = subprocess.run(
        [str(python.parent / "jupyter"), "execute", "--inplace", str(target)],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert done.returncode == 0, done.stderr

    executed = _load(target)
    probe = executed["cells"].pop()
    expected_prefix = subprocess.run(
        [str(python), "-c", "import sys; print(sys.prefix)"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert _stdout(probe) == expected_prefix, "the kernel is not the wheel environment"
    _check_structure(executed)
    _check_no_machine_paths(executed)
    _check_outputs(executed)
