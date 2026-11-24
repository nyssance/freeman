from __future__ import annotations

import contextvars
from typing import Any

_current_request: contextvars.ContextVar[Any] = contextvars.ContextVar("_current_request")
