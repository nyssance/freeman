"""Set the attributes that Bolt keeps on handler functions.

A decorator records its configuration on the function, for example
``__bolt_middleware__`` or ``__paginated__``. A function object takes any
attribute, but a type checker knows only the attributes of its type. Bolt
sets them through this function and reads them with ``getattr``. Call it at
registration only.
"""

from typing import Any


def set_handler_attr(fn: object, name: str, value: Any) -> None:
    """Set the attribute ``name`` of the function ``fn``."""
    setattr(fn, name, value)


__all__ = ["set_handler_attr"]
