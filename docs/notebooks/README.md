# Notebooks

| Notebook | What it covers |
|---|---|
| [`01-first-policy-bundle.ipynb`](01-first-policy-bundle.ipynb) | The [Beginner Guide](../beginner-guide.md) path, run one step at a time: quickstart, crawl, authored policy, decision, bundle, a provenance experiment, and the audits. |

The notebook drives the real `toolgraph` command inside a temporary directory
and deletes it at the end. It needs no Docker, Node.js, network access or
repository clone.

## Running it

Use a Jupyter kernel whose environment has Toolgraph installed:

```bash
pip install "toolgraph[ladybug]" ipykernel
```

Then choose **Restart Kernel and Run All Cells**.

## Updating it

The notebook is committed with its outputs so it reads on GitHub.
`tests/test_notebooks.py` checks those outputs: no machine paths, every command
still exists, and the decisions shown are the ones the prose describes.

Regenerate the outputs from a wheel of this tree, installed outside the
checkout. Running `uv run` inside the checkout would resolve the local project
instead of the wheel.

These commands are for macOS and Linux.

```bash
WORK="$(mktemp -d)"
uv build --wheel --out-dir "$WORK/dist"
uv venv "$WORK/venv"
uv pip install --python "$WORK/venv/bin/python" \
  "$(ls "$WORK"/dist/*.whl)[ladybug]" nbclient ipykernel
env -u TOOLGRAPH_BACKEND -u TOOLGRAPH_DB_PATH -u PYTHONPATH \
  "$WORK/venv/bin/python" - docs/notebooks/01-first-policy-bundle.ipynb <<'EOF'
import sys

import nbclient
import nbformat

path = sys.argv[1]
nb = nbformat.read(path, as_version=4)
for cell in nb.cells:
    cell.metadata.pop("execution", None)
nbclient.NotebookClient(nb, record_timing=False).execute()
nbformat.write(nb, path)
EOF
uv run ruff format docs/notebooks
```

The script drops any per-cell timestamps already in the file, and
`record_timing=False` stops new ones being written. A regenerated notebook then
differs only where the outputs do: the graph instance id and the bundle digest
change on every run. `jupyter execute` has no switch for this.

To run the same execution check CI runs:

```bash
TOOLGRAPH_NOTEBOOK_PYTHON="$WORK/venv/bin/python" \
  uv run pytest tests/test_notebooks.py
```

When you change a CLI command, its output, or the quickstart files, search the
notebooks as well as the Markdown docs: `grep -rn '<text>' docs README.md`.
