"""Shared Numba import with a no-op fallback.

Both grid builders JIT-compile their raster stamps; the fallback keeps
them importable (slow but correct) when Numba is unavailable.
"""

from __future__ import annotations

try:
    from numba import njit
except ImportError:  # pragma: no cover - Numba is a hard dependency.

    def njit(*args, **kwargs):
        """No-op fallback when Numba is unavailable (slow but correct)."""
        if len(args) == 1 and callable(args[0]) and not kwargs:
            return args[0]

        def wrap(fn):
            return fn

        return wrap
