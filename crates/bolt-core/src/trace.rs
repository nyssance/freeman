//! Tracing with the W3C trace context.
//!
//! `DJANGO_BOLT_TRACING=1` turns it on. Rust reads `traceparent` and
//! `tracestate`, makes the server span of each request with the OpenTelemetry
//! SDK, and exports the sampled spans over OTLP/HTTP. The SDK reads the
//! standard variables: `OTEL_SERVICE_NAME`, `OTEL_EXPORTER_OTLP_ENDPOINT`,
//! `OTEL_TRACES_SAMPLER`, `OTEL_BSP_*` and the others.
//!
//! Rust makes the span, and not the Python SDK, for three reasons. A request
//! that Rust rejects (401, 403, 404, 429) gets its span too, with no call to
//! Python. A sampled span costs about a microsecond in Rust, and many more in
//! Python. And the span exists also when the Python SDK is not installed.
//!
//! Python gets the ids of the span (`PyRequest.trace`), so the spans that a
//! handler makes are children of the server span.

use crate::settings::{env_flag, improperly_configured, read_env};
use actix_web::HttpRequest;
use opentelemetry::trace::{
    Span as _, SpanContext, SpanId, SpanKind, Status, TraceContextExt, TraceFlags, TraceId, TraceState, Tracer as _,
    TracerProvider as _,
};
use opentelemetry::{Context, KeyValue};
use opentelemetry_sdk::Resource;
use opentelemetry_sdk::error::OTelSdkResult;
use opentelemetry_sdk::trace::{SdkTracer, SdkTracerProvider, SpanData, SpanExporter};
use pyo3::prelude::*;
use std::str::FromStr;
use std::sync::OnceLock;

/// `1` or `true` turns tracing on.
pub const ENV: &str = "DJANGO_BOLT_TRACING";
/// A `tracestate` longer than this is not passed on. The W3C limit is 512.
const MAX_TRACESTATE: usize = 512;

pub struct Tracing {
    provider: SdkTracerProvider,
    tracer: SdkTracer,
}

static TRACING: OnceLock<Option<Tracing>> = OnceLock::new();

/// The tracing of this process, or `None` when it is off. `init` sets it.
#[inline]
pub fn get() -> Option<&'static Tracing> {
    TRACING.get().and_then(Option::as_ref)
}

/// Tell if `DJANGO_BOLT_TRACING` turns tracing on. `OTEL_SDK_DISABLED=true`
/// turns it off, as for each OpenTelemetry SDK.
pub fn enabled(py: Python<'_>) -> PyResult<bool> {
    if read_env(py, "OTEL_SDK_DISABLED", "true or false")?
        .is_some_and(|value| value.trim().eq_ignore_ascii_case("true"))
    {
        return Ok(false);
    }
    env_flag(py, ENV, false)
}

/// An exporter that drops each span. `OTEL_TRACES_EXPORTER=none` selects it:
/// the ids still go to Python and to the access log, and nothing is sent.
#[derive(Debug)]
struct Discard;

impl SpanExporter for Discard {
    async fn export(&self, _batch: Vec<SpanData>) -> OTelSdkResult {
        Ok(())
    }
}

/// Read the variables once, at the start of the server, and start the SDK.
/// An exporter or a protocol that Bolt does not have raises `ImproperlyConfigured`.
pub fn init(py: Python<'_>) -> PyResult<Option<&'static Tracing>> {
    if let Some(tracing) = TRACING.get() {
        return Ok(tracing.as_ref());
    }
    if !enabled(py)? {
        let _ = TRACING.set(None);
        return Ok(None);
    }
    let exporter = read_env(py, "OTEL_TRACES_EXPORTER", "otlp or none")?.map(|value| value.trim().to_ascii_lowercase());
    for name in ["OTEL_EXPORTER_OTLP_TRACES_PROTOCOL", "OTEL_EXPORTER_OTLP_PROTOCOL"] {
        if let Some(protocol) = read_env(py, name, "http/protobuf")?
            && !protocol.trim().is_empty()
            && protocol.trim() != "http/protobuf"
        {
            return Err(improperly_configured(
                py,
                format!("{name} must be http/protobuf: Bolt exports spans over OTLP/HTTP only, got '{protocol}'."),
            ));
        }
    }
    let builder = SdkTracerProvider::builder().with_resource(Resource::builder().build());
    let provider = match exporter.as_deref() {
        None | Some("" | "otlp") => {
            let exporter = opentelemetry_otlp::SpanExporter::builder().with_http().build().map_err(|error| {
                improperly_configured(py, format!("Bolt cannot make the OTLP span exporter: {error}"))
            })?;
            builder.with_batch_exporter(exporter).build()
        }
        Some("none") => builder.with_batch_exporter(Discard).build(),
        Some(other) => {
            return Err(improperly_configured(
                py,
                format!("OTEL_TRACES_EXPORTER must be otlp or none, got '{other}'."),
            ));
        }
    };
    let tracer = provider.tracer("freeman");
    let _ = TRACING.set(Some(Tracing { provider, tracer }));
    Ok(get())
}

/// Send the spans that wait, and stop the SDK. The server calls this when it stops.
pub fn shutdown() {
    if let Some(tracing) = get() {
        let _ = tracing.provider.shutdown();
    }
}

/// Parse a `traceparent` header by the W3C Trace Context rules. `None` means
/// that the header is not valid, and the request starts a new trace.
///
/// The SDK propagator accepts ids that are too short and a `+` before a
/// number, so Bolt parses the header itself. This also makes no allocation.
pub fn parse_traceparent(value: &str) -> Option<(TraceId, SpanId, TraceFlags)> {
    let value = value.trim_matches([' ', '\t']).as_bytes();
    if value.len() < 55 || value[2] != b'-' || value[35] != b'-' || value[52] != b'-' {
        return None;
    }
    let version = hex(&value[0..2])?;
    // Version ff is not valid. Version 00 has exactly four fields. A later
    // version can add fields after a dash, and the first four keep their form.
    if version == 0xff || (version == 0 && value.len() != 55) || (value.len() > 55 && value[55] != b'-') {
        return None;
    }
    let trace_id = hex(&value[3..35])?;
    let span_id = hex(&value[36..52])?;
    let flags = hex(&value[53..55])?;
    if trace_id == 0 || span_id == 0 {
        return None;
    }
    // Only the sampled flag has a meaning in this version.
    Some((TraceId::from(trace_id), SpanId::from(span_id as u64), TraceFlags::new(flags as u8) & TraceFlags::SAMPLED))
}

/// The number of lowercase hex digits. An upper-case digit is not valid in `traceparent`.
#[inline]
fn hex(digits: &[u8]) -> Option<u128> {
    let mut number: u128 = 0;
    for digit in digits {
        let value = match digit {
            b'0'..=b'9' => digit - b'0',
            b'a'..=b'f' => digit - b'a' + 10,
            _ => return None,
        };
        number = (number << 4) | u128::from(value);
    }
    Some(number)
}

/// The parent of the server span: the remote span of a valid `traceparent`,
/// with its `tracestate`, or no parent.
pub fn parent_context(traceparent: Option<&str>, tracestate: Option<&str>) -> Context {
    let Some((trace_id, span_id, flags)) = traceparent.and_then(parse_traceparent) else {
        return Context::new();
    };
    let state = tracestate
        .filter(|state| state.len() <= MAX_TRACESTATE)
        .and_then(|state| TraceState::from_str(state).ok())
        .unwrap_or_default();
    Context::new().with_remote_span_context(SpanContext::new(trace_id, span_id, flags, true, state))
}

/// The ids of a server span that Python gets.
#[derive(Clone, Debug)]
pub struct TraceIds {
    pub trace_id: TraceId,
    pub span_id: SpanId,
    pub sampled: bool,
    /// The `tracestate` of the span, or empty.
    pub trace_state: String,
}

/// The server span of one request.
pub struct ServerSpan {
    span: opentelemetry_sdk::trace::Span,
}

impl Tracing {
    /// Start the server span of `req`. The sampler of the SDK decides here. An
    /// unsampled span records nothing, and its end costs nothing.
    pub fn start(&self, req: &HttpRequest) -> ServerSpan {
        let headers = req.headers();
        let traceparent = headers.get("traceparent").and_then(|value| value.to_str().ok());
        let tracestate = headers.get("tracestate").and_then(|value| value.to_str().ok());
        let parent = parent_context(traceparent, tracestate);
        // The name gets the route at the end, when the route is known.
        let span = self.tracer.span_builder(method_name(req.method().as_str())).with_kind(SpanKind::Server);
        ServerSpan { span: span.start_with_context(&self.tracer, &parent) }
    }
}

/// The span name and `http.request.method` of a method. A method out of the
/// HTTP set gets `_OTHER`, as the OpenTelemetry conventions tell.
fn method_name(method: &str) -> &'static str {
    match method {
        "GET" => "GET",
        "POST" => "POST",
        "PUT" => "PUT",
        "PATCH" => "PATCH",
        "DELETE" => "DELETE",
        "HEAD" => "HEAD",
        "OPTIONS" => "OPTIONS",
        "QUERY" => "QUERY",
        "TRACE" => "TRACE",
        "CONNECT" => "CONNECT",
        _ => "_OTHER",
    }
}

impl ServerSpan {
    #[inline]
    pub fn context(&self) -> &SpanContext {
        self.span.span_context()
    }

    /// The ids that Python gets.
    pub fn ids(&self) -> TraceIds {
        let context = self.span.span_context();
        let trace_state = context.trace_state().header();
        TraceIds {
            trace_id: context.trace_id(),
            span_id: context.span_id(),
            sampled: context.is_sampled(),
            trace_state,
        }
    }

    /// End the span with the attributes of the HTTP semantic conventions.
    /// `route` is the route template, or `None` when no route matched.
    pub fn finish(mut self, req: &HttpRequest, route: Option<&'static str>, status: u16) {
        if !self.span.is_recording() {
            return;
        }
        let method = method_name(req.method().as_str());
        if let Some(route) = route {
            self.span.update_name(format!("{method} {route}"));
            self.span.set_attribute(KeyValue::new("http.route", route));
        }
        let connection = req.connection_info();
        let mut attributes = vec![
            KeyValue::new("http.request.method", method),
            KeyValue::new("http.response.status_code", i64::from(status)),
            KeyValue::new("url.path", req.path().to_owned()),
            KeyValue::new("url.scheme", connection.scheme().to_owned()),
            KeyValue::new("network.protocol.version", protocol_version(req.version())),
        ];
        if method == "_OTHER" {
            attributes.push(KeyValue::new("http.request.method_original", req.method().as_str().to_owned()));
        }
        if let Some(peer) = req.peer_addr() {
            attributes.push(KeyValue::new("network.peer.address", peer.ip().to_string()));
        }
        if let Some(agent) = req.headers().get("user-agent").and_then(|value| value.to_str().ok()) {
            attributes.push(KeyValue::new("user_agent.original", agent.to_owned()));
        }
        let (address, port) = crate::request::parse_host_port(connection.host(), connection.scheme());
        attributes.push(KeyValue::new("server.address", address.to_owned()));
        attributes.push(KeyValue::new("server.port", i64::from(port)));
        // A 5xx is an error of the server. A 4xx is not, for a server span.
        if status >= 500 {
            attributes.push(KeyValue::new("error.type", status.to_string()));
            self.span.set_status(Status::error(""));
        }
        self.span.set_attributes(attributes);
        self.span.end();
    }
}

fn protocol_version(version: actix_web::http::Version) -> &'static str {
    match version {
        actix_web::http::Version::HTTP_09 => "0.9",
        actix_web::http::Version::HTTP_10 => "1.0",
        actix_web::http::Version::HTTP_2 => "2",
        actix_web::http::Version::HTTP_3 => "3",
        _ => "1.1",
    }
}

#[cfg(test)]
mod tests {
    use super::{TraceFlags, parent_context, parse_traceparent};
    use opentelemetry::trace::{SpanId, TraceContextExt, TraceId};

    const VALID: &str = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01";

    #[test]
    fn a_valid_traceparent_gives_its_ids_and_flag() {
        let (trace_id, span_id, flags) = parse_traceparent(VALID).expect("valid");
        assert_eq!(trace_id, TraceId::from(0x4bf92f3577b34da6a3ce929d0e0e4736));
        assert_eq!(span_id, SpanId::from(0x00f067aa0ba902b7));
        assert!(flags.is_sampled());
        let (_, _, flags) = parse_traceparent("00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-00").unwrap();
        assert!(!flags.is_sampled());
        // Spaces and tabs around the value are optional white space.
        assert!(parse_traceparent(&format!(" \t{VALID} ")).is_some());
        // Flags other than sampled are kept out.
        let (_, _, flags) = parse_traceparent("00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-ff").unwrap();
        assert_eq!(flags, TraceFlags::SAMPLED);
    }

    #[test]
    fn a_malformed_traceparent_starts_a_new_trace() {
        for value in [
            "",
            "00",
            "garbage",
            // Upper case.
            "00-4BF92F3577B34DA6A3CE929D0E0E4736-00f067aa0ba902b7-01",
            // Ids that are too short, and a sign before a number.
            "00-abc-def-01",
            "00-+bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
            "00-4bf92f3577b34da6a3ce929d0e0e473-600f067aa0ba902b7-01",
            // A wrong separator, a character that is not hex, extra text.
            "00_4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
            "00-4bf92f3577b34da6a3ce929d0e0e473g-00f067aa0ba902b7-01",
            "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01-extra",
            "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-0",
        ] {
            assert!(parse_traceparent(value).is_none(), "{value:?}");
            assert!(!parent_context(Some(value), None).has_active_span(), "{value:?}");
        }
    }

    #[test]
    fn the_version_must_be_known_or_later_with_the_same_fields() {
        assert!(parse_traceparent("ff-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01").is_none());
        assert!(parse_traceparent("0g-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01").is_none());
        // A later version keeps the first four fields and can add more.
        assert!(parse_traceparent("01-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01").is_some());
        assert!(parse_traceparent("cc-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01-what-the-future").is_some());
        assert!(parse_traceparent("cc-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01.what").is_none());
    }

    #[test]
    fn an_id_of_all_zeros_is_not_valid() {
        assert!(parse_traceparent("00-00000000000000000000000000000000-00f067aa0ba902b7-01").is_none());
        assert!(parse_traceparent("00-4bf92f3577b34da6a3ce929d0e0e4736-0000000000000000-01").is_none());
    }

    #[test]
    fn the_parent_keeps_the_tracestate() {
        let parent = parent_context(Some(VALID), Some("vendor=value,other=1"));
        let span = parent.span();
        let context = span.span_context();
        assert!(context.is_remote() && context.is_sampled());
        assert_eq!(context.trace_state().get("vendor"), Some("value"));
        // A tracestate over the limit is not passed on.
        let long = format!("vendor={}", "x".repeat(600));
        assert_eq!(parent_context(Some(VALID), Some(&long)).span().span_context().trace_state().header(), "");
    }
}
