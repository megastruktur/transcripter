"""Subprocess CLI: python -m worker.export_once <recording_id> [--rename-only].

Spawned by the export_transcript activity and worker.backfill with a hard
timeout + SIGKILL-abandon: a dead NAS mount parks this process in D-state,
which no in-process exception handling can survive. Exit codes: 0 ok/no-op,
2 export error (message on stderr).
"""

import sys

from .export import ExportError, run


def main() -> None:
    rename_only = "--rename-only" in sys.argv[1:]
    argv = [a for a in sys.argv[1:] if a != "--rename-only"]
    if len(argv) != 1:
        print("usage: python -m worker.export_once <recording_id> [--rename-only]", file=sys.stderr)
        raise SystemExit(2)
    try:
        path = run(argv[0], rename_only=rename_only)
    except ExportError as e:
        print(f"export failed: {e}", file=sys.stderr)
        raise SystemExit(2)
    if path is not None:
        print(path)
if __name__ == "__main__":
    main()
