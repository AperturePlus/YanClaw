"""Compatibility shim for historical imports.

The CLI implementation is now owned by the runtime package.
"""

from __future__ import annotations

import sys

from runtime import crawler_cli as _runtime_crawler_cli

sys.modules[__name__] = _runtime_crawler_cli
