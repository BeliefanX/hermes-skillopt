from __future__ import annotations

"""Backward-compatible CLI shim for the current FastAPI/React WebUI.

Legacy prototype WebUI code has been removed. Use
``python -m hermes_skillopt.webui`` or ``hermes-skillopt webui`` to launch the
FastAPI server implemented in :mod:`hermes_skillopt.webui_server`.
"""

from typing import Optional


def main(argv: Optional[list[str]] = None) -> int:
    from hermes_skillopt.webui_server import main as server_main

    return server_main(argv)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
