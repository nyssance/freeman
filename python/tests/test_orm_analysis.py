"""Tests for the static analysis of the request parts that a handler reads."""

from __future__ import annotations

import functools
import inspect
import json

import pytest

from freeman.analysis import (
    DependencyNeeds,
    HandlerAnalysis,
    analyze_dependency_tree,
    analyze_handler,
    resolve_introspection_target,
)


def handler_reads_request_headers(request):
    """Handler that directly reads request.headers."""
    return request.headers.get("x-test")


def handler_reads_request_body(request):
    """Handler that directly reads request.body."""
    return request.body


def handler_reads_request_query(request):
    """Handler that directly reads request.query."""
    return request.query.get("page")


def handler_reads_request_cookies(request):
    """Handler that directly reads request.cookies."""
    return request.cookies.get("session")


def handler_reads_request_meta(request):
    """Handler that reads Django-compatible META."""
    return {"host": request.META.get("HTTP_HOST"), "query": request.META.get("QUERY_STRING")}


def handler_reads_request_full_path(request):
    """Handler that reads request.get_full_path()."""
    return request.get_full_path()


def handler_reads_request_absolute_uri(request):
    """Handler that reads request.build_absolute_uri()."""
    return request.build_absolute_uri()


def handler_reads_request_get_known_keys(request):
    """Handler that uses request.get(...) with known component keys."""
    return {
        "headers": request.get("headers"),
        "query": request.get("query"),
        "body": request.get("body"),
        "cookies": request.get("cookies"),
    }


def handler_reads_request_subscript_known_keys(request):
    """Handler that uses request[...] with known component keys."""
    return {
        "headers": request["headers"],
        "query": request["query"],
        "body": request["body"],
        "cookies": request["cookies"],
    }


def handler_reads_request_in_local_helper(request):
    """Handler that reads request components inside a nested helper."""

    def inner():
        return request.headers.get("x-test"), request.query.get("page")

    return inner()


def handler_reads_req_alias(req):
    """Handler that uses a non-default request parameter name."""
    return req.headers.get("x-test"), req.cookies.get("session")


def handler_reads_request_unknown_keys(request):
    """Handler that reads request via unknown keys only."""
    return request.get("state"), request["state"]


def external_helper_reads_request_headers(request):
    """External helper used to document current analysis limits."""
    return request.headers.get("x-test")


def handler_passes_request_to_external_helper(request):
    """Handler that delegates request usage to an external helper."""
    return external_helper_reads_request_headers(request)


# inspect.getsource gives only the second line, which does not parse alone.
lambda_on_a_line_that_does_not_parse = functools.partial(
    print, lambda request: request.query)  # fmt: skip


class TestRequestComponentAnalysis:
    """Tests for AST-based request component detection."""

    @staticmethod
    def assert_request_flags(analysis, *, body=False, query=False, headers=False, cookies=False):
        """Assert the detected request-component flags."""
        assert analysis.request_needs_body is body
        assert analysis.request_needs_query is query
        assert analysis.request_needs_headers is headers
        assert analysis.request_needs_cookies is cookies

    def test_direct_request_headers_detection(self):
        """Direct request.headers access sets only the headers flag."""
        analysis = analyze_handler(handler_reads_request_headers, request_param_names={"request"})
        self.assert_request_flags(analysis, headers=True)

    def test_direct_request_body_detection(self):
        """Direct request.body access sets only the body flag."""
        analysis = analyze_handler(handler_reads_request_body, request_param_names={"request"})
        self.assert_request_flags(analysis, body=True)

    def test_direct_request_query_detection(self):
        """Direct request.query access sets only the query flag."""
        analysis = analyze_handler(handler_reads_request_query, request_param_names={"request"})
        self.assert_request_flags(analysis, query=True)

    def test_direct_request_cookies_detection(self):
        """Direct request.cookies access sets only the cookies flag."""
        analysis = analyze_handler(handler_reads_request_cookies, request_param_names={"request"})
        self.assert_request_flags(analysis, cookies=True)

    def test_request_meta_marks_headers_and_query(self):
        """request.META implies both header and query-string access."""
        analysis = analyze_handler(handler_reads_request_meta, request_param_names={"request"})
        self.assert_request_flags(analysis, query=True, headers=True)

    def test_request_get_full_path_marks_query_only(self):
        """request.get_full_path() requires query-string access only."""
        analysis = analyze_handler(handler_reads_request_full_path, request_param_names={"request"})
        self.assert_request_flags(analysis, query=True)

    def test_request_build_absolute_uri_marks_headers_and_query(self):
        """request.build_absolute_uri() requires headers and query-string access."""
        analysis = analyze_handler(handler_reads_request_absolute_uri, request_param_names={"request"})
        self.assert_request_flags(analysis, query=True, headers=True)

    def test_request_get_known_keys_detection(self):
        """request.get(...) with known keys marks all referenced components."""
        analysis = analyze_handler(handler_reads_request_get_known_keys, request_param_names={"request"})
        self.assert_request_flags(analysis, body=True, query=True, headers=True, cookies=True)

    def test_request_subscript_known_keys_detection(self):
        """request[...] with known keys marks all referenced components."""
        analysis = analyze_handler(handler_reads_request_subscript_known_keys, request_param_names={"request"})
        self.assert_request_flags(analysis, body=True, query=True, headers=True, cookies=True)

    def test_nested_local_helper_detection(self):
        """Nested local helpers are still covered by the AST walk."""
        analysis = analyze_handler(handler_reads_request_in_local_helper, request_param_names={"request"})
        self.assert_request_flags(analysis, query=True, headers=True)

    def test_request_alias_name_detection(self):
        """Custom request parameter names are honored when passed to the analyzer."""
        analysis = analyze_handler(handler_reads_req_alias, request_param_names={"req"})
        self.assert_request_flags(analysis, headers=True, cookies=True)

    def test_unknown_request_keys_do_not_mark_components(self):
        """Unknown request.get/request[...] keys should not trigger component flags."""
        analysis = analyze_handler(handler_reads_request_unknown_keys, request_param_names={"request"})
        self.assert_request_flags(analysis)

    def test_a_request_handed_to_a_helper_needs_every_component(self):
        """The helper can read any part of the request, where this analysis does not look."""
        analysis = analyze_handler(handler_passes_request_to_external_helper, request_param_names={"request"})
        self.assert_request_flags(analysis, body=True, query=True, headers=True, cookies=True)


class TestAnalysisFailure:
    """Tests for analysis failure handling."""

    def test_lambda_analysis_fails_gracefully(self):
        """Test that lambda functions fail gracefully."""
        analysis = analyze_handler(lambda: {"hello": "world"})

        # Lambdas may or may not be parseable depending on context
        # Either way, it shouldn't raise an exception
        assert isinstance(analysis, HandlerAnalysis)

    def test_source_that_does_not_parse_fails_the_analysis(self):
        analysis = analyze_handler(lambda_on_a_line_that_does_not_parse.args[0], {"request"})

        assert analysis.analysis_failed is True
        assert analysis.failure_reason.startswith("Syntax error parsing source: ")

    def test_builtin_function_fails_gracefully(self):
        """Test that built-in functions fail gracefully."""
        analysis = analyze_handler(len)

        assert analysis.analysis_failed is True
        assert analysis.failure_reason is not None
        assert "Could not get source" in analysis.failure_reason


# --- Support fixtures for dependency-tree analysis tests ---


def _dep_reads_query_via_request(request):
    return dict(request.query)


def _dep_reads_headers_via_request(request):
    return dict(request.headers)


def _dep_reads_cookies_via_request(request):
    return dict(request.cookies)


def _dep_reads_body_via_request(request):
    return request.body


def _dep_no_request_access(request):
    return {"hello": "world"}


class _CallableDepReadsQuery:
    def __call__(self, request):
        return dict(request.query)


class TestAnalyzeHandlerClassCallable:
    """analyze_handler must resolve class-callable instances to their __call__."""

    def test_class_callable_query_detection(self):
        instance = _CallableDepReadsQuery()
        analysis = analyze_handler(instance, request_param_names={"request"})
        assert analysis.analysis_failed is False
        assert analysis.request_needs_query is True


def _make_fake_field(*, source, dep_fn=None, name="dep"):
    """Build a minimal object with the attributes analyze_dependency_tree reads."""

    class FakeDependsMarker:
        def __init__(self, dependency):
            self.dependency = dependency

    class FakeField:
        pass

    f = FakeField()
    f.name = name
    f.source = source
    f.dependency = FakeDependsMarker(dep_fn) if dep_fn is not None else None
    return f


class TestAnalyzeDependencyTree:
    """analyze_dependency_tree walks Depends targets and aggregates needs."""

    def _compile_fn(self, fake_metas):
        """Return a compile_dep_fn that looks up pre-built metas by callable."""

        def compiler(fn):
            return fake_metas[fn]

        return compiler

    def test_no_dep_fields_produces_no_needs(self):
        meta = {"fields": []}
        needs = analyze_dependency_tree(meta, lambda _fn: {})
        assert needs == DependencyNeeds()

    def test_dep_reading_request_query_marks_query(self):
        dep_fn = _dep_reads_query_via_request
        dep_meta = {"mode": "request_only", "sig": inspect.signature(dep_fn), "fields": []}
        handler_meta = {"fields": [_make_fake_field(source="dependency", dep_fn=dep_fn)]}

        needs = analyze_dependency_tree(handler_meta, self._compile_fn({dep_fn: dep_meta}))

        assert needs.needs_query is True
        assert needs.needs_headers is False
        assert needs.needs_cookies is False
        assert needs.needs_body is False

    def test_dep_reading_request_headers_marks_headers(self):
        dep_fn = _dep_reads_headers_via_request
        dep_meta = {"mode": "request_only", "sig": inspect.signature(dep_fn), "fields": []}
        handler_meta = {"fields": [_make_fake_field(source="dependency", dep_fn=dep_fn)]}

        needs = analyze_dependency_tree(handler_meta, self._compile_fn({dep_fn: dep_meta}))

        assert needs.needs_headers is True
        assert needs.needs_query is False

    def test_dep_reading_request_cookies_marks_cookies(self):
        dep_fn = _dep_reads_cookies_via_request
        dep_meta = {"mode": "request_only", "sig": inspect.signature(dep_fn), "fields": []}
        handler_meta = {"fields": [_make_fake_field(source="dependency", dep_fn=dep_fn)]}

        needs = analyze_dependency_tree(handler_meta, self._compile_fn({dep_fn: dep_meta}))

        assert needs.needs_cookies is True

    def test_dep_reading_request_body_marks_body(self):
        dep_fn = _dep_reads_body_via_request
        dep_meta = {"mode": "request_only", "sig": inspect.signature(dep_fn), "fields": []}
        handler_meta = {"fields": [_make_fake_field(source="dependency", dep_fn=dep_fn)]}

        needs = analyze_dependency_tree(handler_meta, self._compile_fn({dep_fn: dep_meta}))

        assert needs.needs_body is True

    def test_dep_without_request_access_produces_no_needs(self):
        dep_fn = _dep_no_request_access
        dep_meta = {"mode": "request_only", "sig": inspect.signature(dep_fn), "fields": []}
        handler_meta = {"fields": [_make_fake_field(source="dependency", dep_fn=dep_fn)]}

        needs = analyze_dependency_tree(handler_meta, self._compile_fn({dep_fn: dep_meta}))

        assert needs == DependencyNeeds()

    def test_typed_query_in_dep_is_picked_up_via_needs_query(self):
        """A dep with a typed Query() param already sets needs_query in its meta."""

        def dep_fn(page):
            return {"page": page}

        dep_meta = {"mode": "mixed", "fields": [], "needs_query": True}
        handler_meta = {"fields": [_make_fake_field(source="dependency", dep_fn=dep_fn)]}

        needs = analyze_dependency_tree(handler_meta, self._compile_fn({dep_fn: dep_meta}))

        assert needs.needs_query is True

    def test_compile_failure_marks_all_needs(self):
        """If the dep cannot be compiled, we must fall back to parsing everything."""
        dep_fn = _dep_reads_query_via_request
        handler_meta = {"fields": [_make_fake_field(source="dependency", dep_fn=dep_fn)]}

        def failing_compiler(fn):
            raise TypeError("cannot introspect")

        needs = analyze_dependency_tree(handler_meta, failing_compiler)

        assert needs.needs_body is True
        assert needs.needs_query is True
        assert needs.needs_headers is True
        assert needs.needs_cookies is True

    def test_visited_set_prevents_infinite_recursion_on_self_reference(self):
        """A dep referenced twice is only analyzed once (by identity)."""
        dep_fn = _dep_reads_query_via_request
        dep_meta = {"mode": "request_only", "sig": inspect.signature(dep_fn), "fields": []}
        handler_meta = {
            "fields": [
                _make_fake_field(source="dependency", dep_fn=dep_fn, name="a"),
                _make_fake_field(source="dependency", dep_fn=dep_fn, name="b"),
            ]
        }

        needs = analyze_dependency_tree(handler_meta, self._compile_fn({dep_fn: dep_meta}))
        assert needs.needs_query is True


EVERY_PART = ("body", "query", "headers", "cookies")


def flags(analysis):
    """The request parts that an analysis found, in a fixed order."""
    return tuple(part for part in EVERY_PART if getattr(analysis, f"request_needs_{part}"))


class TestRequestPartReads:
    def test_raw_cookies_are_the_cookies_only(self):
        def handler(request):
            return request.raw_cookies

        assert flags(analyze_handler(handler, {"request"})) == ("cookies",)

    def test_request_attributes_that_hold_no_part_read_no_part(self):
        async def handler(request):
            user = await request.auser()
            return request.method, request.path, request.params, request.context, request.state, request.user, user

        assert flags(analyze_handler(handler, {"request"})) == ()

    def test_a_key_that_get_does_not_serve_reads_no_part(self):
        """request.get("META") gives the default. META is an attribute, not a key."""

        def handler(request):
            return request.get("META"), request.get("get_full_path"), request.get("raw_cookies")

        assert flags(analyze_handler(handler, {"request"})) == ()

    def test_a_get_with_a_default_reads_only_its_part(self):
        def handler(request):
            return request.get("headers", {})

        assert flags(analyze_handler(handler, {"request"})) == ("headers",)

    @pytest.mark.parametrize("access", ["get", "subscript"])
    def test_a_key_known_only_at_run_time_can_name_any_part(self, access):
        def by_get(request, key):
            return request.get(key)

        def by_subscript(request, key):
            return request[key]

        handler = by_get if access == "get" else by_subscript

        assert flags(analyze_handler(handler, {"request"})) == EVERY_PART

    def test_the_arguments_of_a_request_method_are_not_keys(self):
        def handler(request, location):
            return request.build_absolute_uri(location)

        assert flags(analyze_handler(handler, {"request"})) == ("query", "headers")

    def test_other_names_do_not_read_the_request(self):
        def handler(request, page, other):
            return page, other.headers, other.request.query, request.headers

        assert flags(analyze_handler(handler, {"request"})) == ("headers",)

    def test_a_handler_and_its_nested_function_both_count(self):
        def handler(request):
            def inner():
                return request.query

            return request.headers, inner()

        assert flags(analyze_handler(handler, {"request"})) == ("query", "headers")


class _Callable:
    def __call__(self, request):
        return request


class TestResolveIntrospectionTarget:
    def test_functions_methods_classes_and_modules_are_their_own_target(self):
        bound = _Callable().__call__

        assert resolve_introspection_target(handler_reads_request_body) is handler_reads_request_body
        assert resolve_introspection_target(bound) is bound
        assert resolve_introspection_target(_Callable) is _Callable
        assert resolve_introspection_target(json) is json

    def test_a_callable_instance_resolves_to_the_call_of_its_class(self):
        assert resolve_introspection_target(_Callable()) is _Callable.__call__

    def test_a_callable_in_c_resolves_to_the_slot_of_its_type(self):
        partial = functools.partial(handler_reads_request_body)

        assert resolve_introspection_target(partial) is inspect.getattr_static(functools.partial, "__call__")


def _dependency(dep_fn, name="dep"):
    return _make_fake_field(source="dependency", dep_fn=dep_fn, name=name)


def _request_only(dep_fn):
    return {"mode": "request_only", "sig": inspect.signature(dep_fn), "fields": []}


def _mixed(*fields, **needs):
    return {"mode": "mixed", "fields": list(fields), **needs}


def _needs(needs):
    return tuple(part for part in EVERY_PART if getattr(needs, f"needs_{part}"))


def _typed_dep(value):
    return value


def _inner_dep(request):
    return request.cookies


def _outer_dep(inner):
    return inner


def _dep_with_request_and_typed_param(req, page):
    return req.headers, page


class TestDependencyTreeWalk:
    """analyze_dependency_tree adds the parts of every dependency, once each."""

    @pytest.mark.parametrize("part", EVERY_PART)
    def test_a_typed_parameter_of_a_dependency_needs_its_part(self, part):
        metas = {_typed_dep: _mixed(**{f"needs_{part}": True})}

        needs = analyze_dependency_tree({"fields": [_dependency(_typed_dep)]}, metas.__getitem__)

        assert _needs(needs) == (part,)

    @pytest.mark.parametrize("part", EVERY_PART)
    def test_a_later_dependency_does_not_clear_what_an_earlier_one_needs(self, part):
        reads_part = globals()[f"_dep_reads_{part}_via_request"]
        quiet = _dep_no_request_access
        typed_metas = {_typed_dep: _mixed(**{f"needs_{part}": True}), quiet: _request_only(quiet)}
        read_metas = {reads_part: _request_only(reads_part), quiet: _request_only(quiet)}

        typed = analyze_dependency_tree(
            {"fields": [_dependency(_typed_dep, name="typed"), _dependency(quiet, name="quiet")]},
            typed_metas.__getitem__,
        )
        read = analyze_dependency_tree(
            {"fields": [_dependency(reads_part, name="reads"), _dependency(quiet, name="quiet")]},
            read_metas.__getitem__,
        )

        assert _needs(typed) == (part,)
        assert _needs(read) == (part,)

    def test_fields_that_are_not_dependencies_are_skipped(self):
        fields = [_make_fake_field(source="query", name="page"), _dependency(_dep_reads_query_via_request)]
        metas = {_dep_reads_query_via_request: _request_only(_dep_reads_query_via_request)}

        needs = analyze_dependency_tree({"fields": fields}, metas.__getitem__)

        assert _needs(needs) == ("query",)

    def test_a_repeated_dependency_is_compiled_once_and_the_next_one_still_counts(self):
        metas = {
            _dep_reads_query_via_request: _request_only(_dep_reads_query_via_request),
            _dep_reads_cookies_via_request: _request_only(_dep_reads_cookies_via_request),
        }
        compiled = []

        def compile_dep(fn):
            compiled.append(fn)
            return metas[fn]

        fields = [
            _dependency(_dep_reads_query_via_request, name="a"),
            _dependency(_dep_reads_query_via_request, name="b"),
            _dependency(_dep_reads_cookies_via_request, name="c"),
        ]

        needs = analyze_dependency_tree({"fields": fields}, compile_dep)

        assert _needs(needs) == ("query", "cookies")
        assert compiled == [_dep_reads_query_via_request, _dep_reads_cookies_via_request]

    def test_a_dependency_that_depends_on_itself_is_walked_once(self):
        metas = {_typed_dep: _mixed(_dependency(_typed_dep), needs_body=True)}

        needs = analyze_dependency_tree({"fields": [_dependency(_typed_dep)]}, metas.__getitem__)

        assert _needs(needs) == ("body",)

    def test_the_dependencies_of_a_dependency_count(self):
        metas = {_outer_dep: _mixed(_dependency(_inner_dep, name="inner")), _inner_dep: _request_only(_inner_dep)}

        needs = analyze_dependency_tree({"fields": [_dependency(_outer_dep)]}, metas.__getitem__)

        assert _needs(needs) == ("cookies",)

    def test_the_request_parameter_of_a_dependency_with_typed_parameters(self):
        fields = (_make_fake_field(source="request", name="req"), _make_fake_field(source="query", name="page"))
        metas = {_dep_with_request_and_typed_param: _mixed(*fields, needs_query=True)}

        needs = analyze_dependency_tree({"fields": [_dependency(_dep_with_request_and_typed_param)]}, metas.__getitem__)

        assert _needs(needs) == ("query", "headers")

    def test_a_dependency_that_the_analysis_cannot_read_needs_every_part(self):
        """inspect.getsource raises for a functools.partial."""
        dep_fn = functools.partial(_dep_reads_query_via_request)
        metas = {dep_fn: _request_only(dep_fn)}

        needs = analyze_dependency_tree({"fields": [_dependency(dep_fn)]}, metas.__getitem__)

        assert _needs(needs) == EVERY_PART
