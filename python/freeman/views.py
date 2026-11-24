"""
Class-based views for Freeman.

Provides Django-style class-based views that integrate seamlessly with
Bolt's routing, dependency injection, guards, and authentication.

Example:
    api = BoltAPI()

    @api.view("/hello")
    class HelloView(APIView):
        guards = [IsAuthenticated()]

        async def get(self, request, current_user=Depends(get_current_user)) -> dict:
            return {"user": current_user.id}
"""

import inspect
from collections.abc import Callable
from typing import Any

from ._view_context import _current_request
from .request import Request


def _layer(value: bool | None, outer: bool | None) -> bool | None:
    """Return ``value`` when it is set, else the next outer layer's value."""
    return outer if value is None else value


def _copy_route_middleware(source: Callable, target: Callable) -> None:
    """Copy the route middleware marks of a view method to the handler that wraps it.

    ``@rate_limit``, ``@middleware`` and ``@skip_middleware`` mark the method.
    Bolt registers the wrapper, so the wrapper needs the same marks. The copies
    are new objects, because registration writes the normalized list back.
    """
    if hasattr(source, "__bolt_middleware__"):
        target.__bolt_middleware__ = list(source.__bolt_middleware__)
    if hasattr(source, "__bolt_skip_middleware__"):
        target.__bolt_skip_middleware__ = set(source.__bolt_skip_middleware__)


_REQUEST_PARAM_NAMES = frozenset({"request", "req"})


def _plan_request_argument(
    params: list[inspect.Parameter],
) -> tuple[list[inspect.Parameter], int | None, str | None, int]:
    """Find where the handler of a view method gets the request.

    Bolt passes the request to the parameter with the name ``request`` or
    ``req``. A method with no such parameter gets one in the handler signature,
    in front. The handler reads it for ``self.request`` and does not pass it on.

    Returns:
        The handler parameters, the positional index of the request (or None),
        the keyword name of the request (or None), and the number of leading
        arguments that the method does not take.
    """
    for index, param in enumerate(params):
        if param.name in _REQUEST_PARAM_NAMES:
            if param.kind is inspect.Parameter.KEYWORD_ONLY:
                return params, None, param.name, 0
            return params, index, None, 0

    kind = (
        inspect.Parameter.POSITIONAL_ONLY
        if params and params[0].kind is inspect.Parameter.POSITIONAL_ONLY
        else inspect.Parameter.POSITIONAL_OR_KEYWORD
    )
    return [inspect.Parameter("request", kind), *params], 0, None, 1


class APIView:
    """
    Base class for class-based views in Freeman.

    Attributes:
        http_method_names: Tuple of supported HTTP methods (lowercase)
        guards: List of guard/permission classes to apply to all methods
        auth: List of authentication backends to apply to all methods
        status_code: Default status code for responses (can be overridden per-method)
    """

    http_method_names = ("get", "post", "put", "patch", "delete", "head", "options", "query")

    # Class-level defaults (can be overridden by subclasses)
    guards: list[Any] | None = None
    auth: list[Any] | None = None
    status_code: int | None = None
    validate_response: bool | None = None
    include_in_schema: bool | None = None

    def __init__(self, **kwargs):
        """
        Initialize the view instance.

        Args:
            **kwargs: Additional instance attributes
        """
        for key, value in kwargs.items():
            setattr(self, key, value)

    @property
    def request(self) -> Request:
        return _current_request.get()

    @classmethod
    def as_view(cls, method: str, **initkwargs: Any) -> Callable:
        """
        Create a handler callable for a specific HTTP method.

        This method:
        1. Validates that the HTTP method is supported
        2. Creates a wrapper that instantiates the view and calls the method handler
        3. Preserves the method signature for parameter extraction and dependency injection
        4. Attaches class-level metadata (guards, auth) for middleware compilation
        5. Supports both sync and async handler methods

        Args:
            method: HTTP method name (lowercase, e.g., "get", "post")
            **initkwargs: Attributes of the view instance of this handler. They
                do not change the class.

        Returns:
            Handler function compatible with BoltAPI routing (sync or async)

        Raises:
            ValueError: If method is not supported by this view
        """
        method_lower = method.lower()

        if method_lower not in cls.http_method_names:
            raise ValueError(f"Method '{method}' not allowed. Allowed methods: {cls.http_method_names}")

        method_handler = getattr(cls, method_lower, None)
        if method_handler is None:
            raise ValueError(f"View class {cls.__name__} does not implement method '{method_lower}'")

        # Handlers can be sync or async
        # Sync handlers will be executed via spawn_blocking or inline mode
        # This is determined at registration time based on the 'inline' parameter

        # Create wrapper that preserves signature for parameter extraction
        # The wrapper's signature matches the method handler (excluding 'self')
        sig = inspect.signature(method_handler)
        params = list(sig.parameters.values())[1:]  # Skip 'self' parameter

        # Decide one time where the request is. A method with no request
        # parameter gets one in the handler signature, for `self.request`.
        params, request_index, request_name, skip = _plan_request_argument(params)

        # Build new signature without 'self'
        new_sig = sig.replace(parameters=params)

        # Create single view instance at registration time (not per-request)
        # This eliminates the per-request instantiation overhead (~40% faster)
        view_instance = cls(**initkwargs)

        # Bind the method once to eliminate lookup overhead
        bound_method = method_handler.__get__(view_instance, cls)

        # Create handler wrapper based on whether method is async or sync
        is_async_method = inspect.iscoroutinefunction(method_handler)

        if is_async_method:
            # Create async wrapper for async methods
            async def view_handler(*args, **kwargs):
                """Auto-generated async view handler that calls bound method directly."""
                _current_request.set(args[request_index] if request_name is None else kwargs[request_name])
                return await bound_method(*args[skip:], **kwargs)

        else:
            # Create sync wrapper for sync methods
            def view_handler(*args, **kwargs):
                """Auto-generated sync view handler that calls bound method directly."""
                _current_request.set(args[request_index] if request_name is None else kwargs[request_name])
                return bound_method(*args[skip:], **kwargs)

        view_handler.__wrapped__ = method_handler

        # Attach the signature (for parameter extraction)
        view_handler.__signature__ = new_sig
        view_handler.__annotations__ = {k: v for k, v in inspect.get_annotations(method_handler).items() if k != "self"}

        # Preserve docstring and name
        view_handler.__name__ = f"{cls.__name__}.{method_lower}"
        view_handler.__doc__ = method_handler.__doc__
        view_handler.__module__ = cls.__module__

        # Attach class-level metadata for middleware compilation
        # These will be picked up by BoltAPI._route_decorator
        if cls.guards is not None:
            view_handler.__bolt_guards__ = cls.guards
        if cls.auth is not None:
            view_handler.__bolt_auth__ = cls.auth
        if cls.status_code is not None:
            view_handler.__bolt_status_code__ = cls.status_code
        if cls.validate_response is not None:
            view_handler.__bolt_validate_response__ = cls.validate_response

        if getattr(method_handler, "validate_response", None) is not None:
            view_handler.validate_response = method_handler.validate_response

        _copy_route_middleware(method_handler, view_handler)

        # Copy pagination attributes from method handler (for @paginate decorator)
        if hasattr(method_handler, "__paginated__"):
            view_handler.__paginated__ = method_handler.__paginated__
        if hasattr(method_handler, "__pagination_class__"):
            view_handler.__pagination_class__ = method_handler.__pagination_class__
        # The route sets the item type of @paginate on this original handler.
        view_handler.__original_handler__ = method_handler

        return view_handler

    @classmethod
    def get_allowed_methods(cls) -> set[str]:
        """
        Get the set of HTTP methods that this view implements.

        Returns:
            Set of uppercase HTTP method names (e.g., {"GET", "POST"})
        """
        allowed = set()
        for method in cls.http_method_names:
            if hasattr(cls, method) and callable(getattr(cls, method)):
                allowed.add(method.upper())
        return allowed
