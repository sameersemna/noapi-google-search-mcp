"""Entry point for the PyInstaller build.

PyInstaller runs its target script as a top-level ``__main__`` module, so
``src/google_search_mcp/__main__.py`` cannot be used directly — its
``from . import main`` is a relative import with no parent package.

This shim imports the installed package instead, which works both in a source
checkout and in the frozen binary (which bundles the package via the wheel).
"""

from google_search_mcp import main

if __name__ == "__main__":
    main()
