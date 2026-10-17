"""Allow running as ``python -m google_search_mcp``.

Delegates to :func:`google_search_mcp.main` so the CLI behaves identically
whether invoked via the console script or the module.

NOTE: this module previously called ``main()`` at import time (outside the
``__name__`` guard), which started the server whenever the module was imported.
The call is now correctly guarded.
"""

from . import main

if __name__ == "__main__":
    main()
