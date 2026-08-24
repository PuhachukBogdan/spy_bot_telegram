"""Load the built React shell and fill it with a metrics JSON island.

This is the whole Python side of the frontend: no templating, no markup. Python
produces numbers, the shell renders them. That split is what makes period
comparison, arbitrary date ranges and drill-down possible without a server round
trip — the page carries its own data.

The shell is built by ``frontend/`` (``npm run build``) into ONE self-contained
HTML file with all JS and CSS inlined, so the filled page keeps every property
the current report has: served straight from the database behind a capability
URL, no external asset requests, printable, and byte-stable once issued.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.utils.logging import get_logger

log = get_logger(__name__)

_ROOT = Path(__file__).resolve().parents[2]

#: Where the built shell may live. The image path wins so a container never picks
#: up a stale dev build that happens to be lying around in the source tree.
_SHELL_PATHS = (
    _ROOT / "static" / "report-shell.html",
    _ROOT / "frontend" / "dist" / "index.html",
)

_ISLAND_ID = "report-data"


def shell_path() -> Path | None:
    """First existing shell build, or ``None`` when the frontend was never built."""
    return next((path for path in _SHELL_PATHS if path.is_file()), None)


def load_shell() -> str | None:
    """Read the built shell, or ``None`` if it is missing."""
    path = shell_path()
    if path is None:
        log.warning("report.shell.missing", searched=[str(p) for p in _SHELL_PATHS])
        return None
    return path.read_text(encoding="utf-8")


def _encode(payload: dict[str, Any]) -> str:
    """JSON, hardened for embedding inside a <script> element.

    ``</script>`` anywhere in the data would end the element early and spill the
    rest of the document as markup, so every ``<`` is escaped. ``ensure_ascii``
    stays off to keep Cyrillic readable, which also keeps the page smaller.
    """
    encoded = json.dumps(payload, ensure_ascii=False, default=str)
    return encoded.replace("<", "\\u003c").replace("\u2028", "\\u2028").replace(
        "\u2029", "\\u2029"
    )


def fill_shell(shell: str, payload: dict[str, Any]) -> str:
    """Inject the metrics island into the shell, just before ``</body>``.

    Placed at the end of the body so the island is parsed before the module
    script runs but adds nothing to the critical path.
    """
    island = (
        f'<script id="{_ISLAND_ID}" type="application/json">'
        f"{_encode(payload)}</script>"
    )
    marker = "</body>"
    index = shell.rfind(marker)
    if index == -1:
        # A shell without </body> is a broken build; appending still yields a
        # page that renders rather than one that silently shows nothing.
        return shell + island
    return shell[:index] + island + shell[index:]


def render_with_shell(payload: dict[str, Any]) -> str | None:
    """Fill the shell with ``payload``, or ``None`` when no shell is built."""
    shell = load_shell()
    return None if shell is None else fill_shell(shell, payload)
