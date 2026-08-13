"""HTTP routes — composition-root only (F9/F3): each module here wires quadrant collaborators
(`studio_workbench`, `studio_engine`, `studio_kb`, `studio_evalhub`) into one FastAPI endpoint.
No route module owns business logic of its own — it only assembles calls to seams the quadrants
already expose (`resolve_session`, `interpreter.run`, `publish`, `EvalHarness.run`, ...)."""

from __future__ import annotations
