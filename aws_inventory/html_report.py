"""A self-contained, offline HTML cost report. No external scripts or assets."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from .estimates import build_cost_model
from .report_template import render_template


ASSET_DIRECTORY = Path(__file__).parent
CSS = (ASSET_DIRECTORY / "report_styles.css").read_text(encoding="utf-8")
SCRIPT = "\n".join((ASSET_DIRECTORY / name).read_text(encoding="utf-8") for name in (
    "report_ui.js", "report_downloads.js", "report_calculation.js",
))


def render_html(model):
    return render_template(model, CSS, SCRIPT)


def write_html_report(path, rows, *, generated_at, context, partial, excel=False, overwrite=False):
    path = Path(path).expanduser().absolute()
    if path.is_symlink() or path.is_dir():
        raise ValueError("HTML output must be a regular file path, not a directory or symbolic link.")
    if path.exists() and not overwrite:
        raise FileExistsError(f"Report exists: {path}. Use --overwrite.")
    model = build_cost_model(rows, generated_at=generated_at, context=context, partial=partial)
    document = render_html(model)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix=f".{path.name}.",
                                         suffix=".tmp", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(document)
            stream.flush()
            os.fsync(stream.fileno())
        if overwrite:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
            temporary.unlink()
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
