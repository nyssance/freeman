//! Request metrics in the Prometheus text format, served by Rust.
//!
//! `DJANGO_BOLT_METRICS=/metrics` turns them on. Each request adds one count
//! to a series of its method, route template and status, and one duration to
//! the histogram of that series. Rust answers a scrape of the path itself, and
//! Python does not run for it.
//!
//! # Many worker processes
//!
//! `runbolt --processes N` runs N processes on one port, and a scrape reaches
//! one of them. Each process writes its numbers to a file in a shared folder
//! (`DJANGO_BOLT_METRICS_DIR`) once a second. The process that gets the scrape
//! adds the files of the other processes to its own live numbers. This is the
//! method of the multiprocess mode of the Prometheus Python client, which
//! Gunicorn users run. The file of a process that stops stays, so a counter
//! does not go down when a worker is recycled.
//!
//! # Cost
//!
//! Each worker thread has its own map of series behind a mutex that only that
//! thread and a scrape lock. A request takes the lock with no contention and
//! adds to plain integers. It does not allocate after the first request of a
//! series.

use crate::settings::{env_var, read_env};
use actix_web::HttpResponse;
use actix_web::http::header::{self, HeaderMap};
use ahash::AHashMap;
use parking_lot::Mutex;
use pyo3::prelude::*;
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::fmt::Write as _;
use std::net::IpAddr;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicI64, Ordering};
use std::sync::{Arc, OnceLock};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

/// The path of the metrics, such as `/metrics`. Without it, there are no metrics.
pub const PATH_ENV: &str = "DJANGO_BOLT_METRICS";
/// With this token, a scrape must send `Authorization: Bearer <token>`.
pub const TOKEN_ENV: &str = "DJANGO_BOLT_METRICS_TOKEN";
/// The folder that the worker processes share. `runbolt --processes N` sets it.
pub const DIR_ENV: &str = "DJANGO_BOLT_METRICS_DIR";

/// Upper bounds of the duration buckets, in seconds. These are the bounds that
/// the OpenTelemetry HTTP semantic conventions advise for the request duration.
pub const BUCKETS: [f64; 14] = [0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75, 1.0, 2.5, 5.0, 7.5, 10.0];

/// The method labels. A method out of this set gets `_OTHER`, as in the
/// OpenTelemetry conventions, so a client cannot make new series with it.
const METHODS: [&str; 9] = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "QUERY", "_OTHER"];

/// A process writes its file this often.
const WRITE_INTERVAL: Duration = Duration::from_secs(1);
/// The requests in flight of a file older than this do not count. Its process
/// stopped, or it cannot write.
const LIVE_LIMIT: f64 = 5.0;

/// The index of the method label of `method`.
#[inline]
pub fn method_index(method: &str) -> u8 {
    match method {
        "GET" => 0,
        "POST" => 1,
        "PUT" => 2,
        "PATCH" => 3,
        "DELETE" => 4,
        "HEAD" => 5,
        "OPTIONS" => 6,
        "QUERY" => 7,
        _ => 8,
    }
}

/// The status label. A status out of 100 to 599 is not an HTTP status. It
/// gets 0, so the number of status labels has a limit.
#[inline]
pub fn status_label(status: u16) -> u16 {
    if (100..=599).contains(&status) { status } else { 0 }
}

/// The index of the bucket of a duration: the first bound that is not less
/// than `seconds`. A duration over the last bound gets `BUCKETS.len()` (+Inf).
#[inline]
pub fn bucket_index(seconds: f64) -> usize {
    BUCKETS.iter().position(|bound| seconds <= *bound).unwrap_or(BUCKETS.len())
}

/// The count, the sum and the buckets of one series. A bucket holds the
/// requests of its own range only. The text format adds them up.
#[derive(Clone, Copy, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct Cell {
    pub count: u64,
    pub sum: f64,
    pub buckets: [u64; BUCKETS.len() + 1],
}

impl Cell {
    #[inline]
    pub fn add(&mut self, seconds: f64) {
        self.count += 1;
        self.sum += seconds;
        self.buckets[bucket_index(seconds)] += 1;
    }

    fn merge(&mut self, other: &Cell) {
        self.count += other.count;
        self.sum += other.sum;
        for (bucket, more) in self.buckets.iter_mut().zip(other.buckets.iter()) {
            *bucket += more;
        }
    }
}

/// The key of a series in a shard: the address of the interned route label,
/// the method index and the status. The label itself is in the value.
type ShardKey = (usize, u8, u16);

/// The series of one worker thread.
#[derive(Default)]
struct Shard {
    series: Mutex<AHashMap<ShardKey, (&'static str, Cell)>>,
}

/// The labels of a series in a merged view: route, method and status.
type SeriesKey = (String, String, u16);

/// The numbers of one process, as its file holds them.
#[derive(Default, Serialize, Deserialize)]
struct ProcessFile {
    pid: u32,
    /// Seconds since the epoch when the process wrote the file.
    time: f64,
    in_flight: i64,
    series: Vec<FileSeries>,
}

#[derive(Serialize, Deserialize)]
struct FileSeries {
    route: String,
    method: String,
    status: u16,
    #[serde(flatten)]
    cell: Cell,
}

/// The answer to a scrape before the numbers.
#[derive(Debug, PartialEq, Eq)]
pub enum Access {
    Allow,
    /// A token is set, and the request does not have it.
    Unauthorized,
    /// No token is set, and the request does not come straight from this host.
    Forbidden,
}

pub struct Metrics {
    path: String,
    token: Option<String>,
    /// The shared folder and the name of the file of this process in it.
    dir: Option<(PathBuf, String)>,
    shards: Mutex<Vec<Arc<Shard>>>,
    in_flight: AtomicI64,
}

static METRICS: OnceLock<Option<Metrics>> = OnceLock::new();

thread_local! {
    static SHARD: Arc<Shard> = new_shard();
}

fn new_shard() -> Arc<Shard> {
    let shard = Arc::new(Shard::default());
    if let Some(metrics) = get() {
        metrics.shards.lock().push(Arc::clone(&shard));
    }
    shard
}

/// The metrics of this process, or `None` when they are off. `init` sets them.
#[inline]
pub fn get() -> Option<&'static Metrics> {
    METRICS.get().and_then(Option::as_ref)
}

/// Read the metrics variables once, at the start of the server. An invalid
/// value raises `ImproperlyConfigured`. With a shared folder, this starts the
/// thread that writes the file of this process.
pub fn init(py: Python<'_>) -> PyResult<Option<&'static Metrics>> {
    if let Some(metrics) = METRICS.get() {
        return Ok(metrics.as_ref());
    }
    let path = env_var(py, PATH_ENV, "a path that starts with '/'", |path: &String| path.starts_with('/'))?;
    let metrics = match path {
        None => None,
        Some(path) => {
            let token = read_env(py, TOKEN_ENV, "a token")?.filter(|token| !token.trim().is_empty());
            let dir = match read_env(py, DIR_ENV, "a folder")?.filter(|dir| !dir.trim().is_empty()) {
                Some(dir) => {
                    let dir = PathBuf::from(dir);
                    std::fs::create_dir_all(&dir).map_err(|error| {
                        crate::settings::improperly_configured(
                            py,
                            format!("{DIR_ENV} must be a folder that Bolt can write, got '{}': {error}", dir.display()),
                        )
                    })?;
                    Some((dir, process_file_name()))
                }
                None => None,
            };
            Some(Metrics::new(path, token, dir))
        }
    };
    let _ = METRICS.set(metrics);
    let metrics = get();
    if let Some(metrics) = metrics
        && metrics.dir.is_some()
    {
        std::thread::Builder::new()
            .name("bolt-metrics".to_string())
            .spawn(move || {
                loop {
                    std::thread::sleep(WRITE_INTERVAL);
                    metrics.write_file(metrics.in_flight.load(Ordering::Relaxed));
                }
            })
            .map_err(|error| {
                pyo3::exceptions::PyRuntimeError::new_err(format!("cannot start the metrics writer: {error}"))
            })?;
    }
    Ok(metrics)
}

/// Write the last numbers of this process, with no request in flight. The
/// server calls this when it stops.
pub fn flush() {
    if let Some(metrics) = get() {
        metrics.write_file(0);
    }
}

/// The name of the file of this process. The start time is in the name, so a
/// new process that gets the pid of an old one does not replace its file.
fn process_file_name() -> String {
    let start = SystemTime::now().duration_since(UNIX_EPOCH).map_or(0, |elapsed| elapsed.as_nanos());
    format!("metrics-{}-{start}.json", std::process::id())
}

fn now_seconds() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map_or(0.0, |elapsed| elapsed.as_secs_f64())
}

/// A request in flight. The count goes down when this drops, also when the
/// client goes away before the response.
pub struct InFlight<'a>(&'a AtomicI64);

impl Drop for InFlight<'_> {
    #[inline]
    fn drop(&mut self) {
        self.0.fetch_sub(1, Ordering::Relaxed);
    }
}

impl Metrics {
    fn new(path: String, token: Option<String>, dir: Option<(PathBuf, String)>) -> Self {
        Self { path, token, dir, shards: Mutex::new(Vec::new()), in_flight: AtomicI64::new(0) }
    }

    /// The path of the metrics.
    #[inline]
    pub fn path(&self) -> &str {
        &self.path
    }

    /// Count a request in flight until the guard drops.
    #[inline]
    pub fn start(&self) -> InFlight<'_> {
        self.in_flight.fetch_add(1, Ordering::Relaxed);
        InFlight(&self.in_flight)
    }

    /// Add one request to the series of its labels. `route` is the interned
    /// route template (see `router::intern_template`) or `UNMATCHED_ROUTE`.
    #[inline]
    pub fn record(&self, route: &'static str, method: &str, status: u16, seconds: f64) {
        let key = (route.as_ptr() as usize, method_index(method), status_label(status));
        SHARD.with(|shard| {
            shard.series.lock().entry(key).or_insert_with(|| (route, Cell::default())).1.add(seconds);
        });
    }

    /// Tell if a request can read the metrics. With a token, the request must
    /// send it as a bearer token. Without a token, the peer must be a loopback
    /// address and the request must have no forwarding header: a proxy on
    /// this host must not make a public request look local.
    pub fn access(&self, headers: &HeaderMap, peer: Option<IpAddr>) -> Access {
        if let Some(token) = &self.token {
            let given = headers
                .get(header::AUTHORIZATION)
                .and_then(|value| value.to_str().ok())
                .and_then(|value| value.strip_prefix("Bearer "));
            return match given {
                Some(given)
                    if aws_lc_rs::constant_time::verify_slices_are_equal(given.as_bytes(), token.as_bytes())
                        .is_ok() =>
                {
                    Access::Allow
                }
                _ => Access::Unauthorized,
            };
        }
        let forwarded = ["forwarded", "x-forwarded-for", "x-real-ip"].iter().any(|name| headers.contains_key(*name));
        if peer.is_some_and(|ip| ip.is_loopback()) && !forwarded { Access::Allow } else { Access::Forbidden }
    }

    /// The response to a request for the metrics path.
    pub fn answer(&self, headers: &HeaderMap, peer: Option<IpAddr>) -> HttpResponse {
        match self.access(headers, peer) {
            Access::Allow => HttpResponse::Ok()
                .content_type("text/plain; version=0.0.4; charset=utf-8")
                .insert_header((header::CACHE_CONTROL, "no-store"))
                .body(self.render()),
            Access::Unauthorized => HttpResponse::Unauthorized()
                .insert_header((header::WWW_AUTHENTICATE, "Bearer"))
                .content_type("text/plain; charset=utf-8")
                .body("The metrics need a bearer token.\n"),
            Access::Forbidden => HttpResponse::Forbidden()
                .content_type("text/plain; charset=utf-8")
                .body(format!("Without {TOKEN_ENV}, only a client on this host can read the metrics.\n")),
        }
    }

    /// The live series of this process, merged over its worker threads.
    fn own_series(&self) -> BTreeMap<SeriesKey, Cell> {
        let shards: Vec<Arc<Shard>> = self.shards.lock().clone();
        let mut merged = BTreeMap::new();
        for shard in shards {
            for (route, cell) in shard.series.lock().iter().map(|((_, method, status), (route, cell))| {
                ((route.to_string(), METHODS[*method as usize].to_string(), *status), cell)
            }) {
                merged.entry(route).or_insert_with(Cell::default).merge(cell);
            }
        }
        merged
    }

    /// Write the file of this process: a new file, then a rename, so a reader
    /// never sees half a file.
    fn write_file(&self, in_flight: i64) {
        let Some((dir, name)) = &self.dir else {
            return;
        };
        let file = ProcessFile {
            pid: std::process::id(),
            time: now_seconds(),
            in_flight,
            series: self
                .own_series()
                .into_iter()
                .map(|((route, method, status), cell)| FileSeries { route, method, status, cell })
                .collect(),
        };
        let Ok(text) = serde_json::to_vec(&file) else {
            return;
        };
        let temporary = dir.join(format!(".{name}.tmp"));
        if std::fs::write(&temporary, text).is_ok() {
            let _ = std::fs::rename(&temporary, dir.join(name));
        }
    }

    /// The text of the metrics: the live numbers of this process, and the
    /// numbers in the files of the other processes.
    pub fn render(&self) -> String {
        let mut series = self.own_series();
        let mut in_flight = self.in_flight.load(Ordering::Relaxed);
        let (mut live, mut exited) = (1u64, 0u64);
        if let Some((dir, own)) = &self.dir {
            let now = now_seconds();
            for file in read_process_files(dir, own) {
                if now - file.time <= LIVE_LIMIT {
                    live += 1;
                    in_flight += file.in_flight;
                } else {
                    exited += 1;
                }
                for entry in file.series {
                    series.entry((entry.route, entry.method, entry.status)).or_default().merge(&entry.cell);
                }
            }
        }
        encode(&series, in_flight, live, exited)
    }
}

/// Read the files of the other processes in `dir`. A file that does not parse
/// is skipped: its process may write it at this moment for the first time.
fn read_process_files(dir: &Path, own: &str) -> Vec<ProcessFile> {
    let Ok(entries) = std::fs::read_dir(dir) else {
        return Vec::new();
    };
    entries
        .filter_map(Result::ok)
        .filter(|entry| {
            let name = entry.file_name();
            let name = name.to_string_lossy();
            name.starts_with("metrics-") && name.ends_with(".json") && name != own
        })
        .filter_map(|entry| std::fs::read(entry.path()).ok())
        .filter_map(|bytes| serde_json::from_slice::<ProcessFile>(&bytes).ok())
        .collect()
}

/// Escape a label value for the text format.
fn escape(value: &str) -> std::borrow::Cow<'_, str> {
    if !value.contains(['\\', '"', '\n']) {
        return std::borrow::Cow::Borrowed(value);
    }
    std::borrow::Cow::Owned(value.replace('\\', r"\\").replace('"', "\\\"").replace('\n', r"\n"))
}

/// The Prometheus text format of the series.
fn encode(series: &BTreeMap<SeriesKey, Cell>, in_flight: i64, live: u64, exited: u64) -> String {
    let mut out = String::with_capacity(512 + series.len() * 1400);
    out.push_str("# HELP http_server_requests_total Requests that the server answered.\n");
    out.push_str("# TYPE http_server_requests_total counter\n");
    for ((route, method, status), cell) in series {
        let _ = writeln!(
            out,
            r#"http_server_requests_total{{method="{method}",route="{}",status="{status}"}} {}"#,
            escape(route),
            cell.count
        );
    }
    out.push_str("# HELP http_server_request_duration_seconds Time from the request to the response headers.\n");
    out.push_str("# TYPE http_server_request_duration_seconds histogram\n");
    for ((route, method, status), cell) in series {
        let labels = format!(r#"method="{method}",route="{}",status="{status}""#, escape(route));
        let mut total = 0;
        for (index, bound) in BUCKETS.iter().enumerate() {
            total += cell.buckets[index];
            let _ = writeln!(out, r#"http_server_request_duration_seconds_bucket{{{labels},le="{bound}"}} {total}"#);
        }
        total += cell.buckets[BUCKETS.len()];
        let _ = writeln!(out, r#"http_server_request_duration_seconds_bucket{{{labels},le="+Inf"}} {total}"#);
        let _ = writeln!(out, "http_server_request_duration_seconds_sum{{{labels}}} {}", cell.sum);
        let _ = writeln!(out, "http_server_request_duration_seconds_count{{{labels}}} {}", cell.count);
    }
    out.push_str("# HELP http_server_active_requests Requests in flight.\n");
    out.push_str("# TYPE http_server_active_requests gauge\n");
    let _ = writeln!(out, "http_server_active_requests {in_flight}");
    out.push_str("# HELP freeman_metrics_processes Worker processes whose numbers are in this answer.\n");
    out.push_str("# TYPE freeman_metrics_processes gauge\n");
    let _ = writeln!(out, r#"freeman_metrics_processes{{state="live"}} {live}"#);
    let _ = writeln!(out, r#"freeman_metrics_processes{{state="exited"}} {exited}"#);
    out
}

#[cfg(test)]
mod tests {
    use super::{
        Access, BUCKETS, Cell, Metrics, ProcessFile, bucket_index, encode, method_index, read_process_files,
        status_label,
    };
    use crate::router::{UNMATCHED_ROUTE, intern_template};
    use actix_web::http::header::{HeaderMap, HeaderName, HeaderValue};
    use std::collections::BTreeMap;
    use std::net::IpAddr;

    #[test]
    fn a_duration_goes_to_the_first_bucket_that_is_not_less() {
        assert_eq!(bucket_index(0.0), 0);
        assert_eq!(bucket_index(0.005), 0);
        assert_eq!(bucket_index(0.0051), 1);
        assert_eq!(bucket_index(0.1), 5);
        assert_eq!(bucket_index(10.0), BUCKETS.len() - 1);
        assert_eq!(bucket_index(10.5), BUCKETS.len());
        assert_eq!(bucket_index(f64::INFINITY), BUCKETS.len());
    }

    #[test]
    fn the_buckets_add_up_in_the_text() {
        let mut cell = Cell::default();
        for seconds in [0.001, 0.003, 0.02, 0.3, 20.0] {
            cell.add(seconds);
        }
        let mut series = BTreeMap::new();
        series.insert(("/items/{id}".to_string(), "GET".to_string(), 200), cell);
        let text = encode(&series, 2, 1, 0);
        let labels = r#"method="GET",route="/items/{id}",status="200""#;
        assert!(text.contains(&format!("http_server_requests_total{{{labels}}} 5\n")), "{text}");
        assert!(text.contains(&format!(r#"_bucket{{{labels},le="0.005"}} 2"#)), "{text}");
        assert!(text.contains(&format!(r#"_bucket{{{labels},le="0.025"}} 3"#)), "{text}");
        assert!(text.contains(&format!(r#"_bucket{{{labels},le="0.5"}} 4"#)), "{text}");
        assert!(text.contains(&format!(r#"_bucket{{{labels},le="10"}} 4"#)), "{text}");
        assert!(text.contains(&format!(r#"_bucket{{{labels},le="+Inf"}} 5"#)), "{text}");
        assert!(text.contains(&format!("_count{{{labels}}} 5\n")), "{text}");
        assert!(text.contains("http_server_active_requests 2\n"), "{text}");
    }

    #[test]
    fn the_labels_have_a_limit() {
        // A client cannot add a method label or a status label.
        assert_eq!(method_index("GET"), 0);
        assert_eq!(method_index("BREW"), 8);
        assert_eq!(method_index("get"), 8);
        assert_eq!(status_label(404), 404);
        assert_eq!(status_label(99), 0);
        assert_eq!(status_label(600), 0);
        // The route label is the interned template, one for each path.
        let first = intern_template("/items/{id}");
        let second = intern_template(&String::from("/items/{id}"));
        assert!(std::ptr::eq(first, second));
        let metrics = Metrics::new("/metrics".into(), None, None);
        for (method, status) in [("GET", 200), ("BREW", 200), ("PROPFIND", 200), ("GET", 999), ("GET", 700)] {
            metrics.record(first, method, status, 0.001);
        }
        metrics.record(UNMATCHED_ROUTE, "GET", 404, 0.001);
        // `record` adds to the shard of this thread. A test registers it by hand.
        super::SHARD.with(|shard| metrics.shards.lock().push(std::sync::Arc::clone(shard)));
        let series = metrics.own_series();
        let keys: Vec<_> =
            series.keys().map(|(route, method, status)| (route.as_str(), method.as_str(), *status)).collect();
        assert_eq!(
            keys,
            [
                ("/items/{id}", "GET", 0),
                ("/items/{id}", "GET", 200),
                ("/items/{id}", "_OTHER", 200),
                ("unmatched", "GET", 404)
            ]
        );
        assert_eq!(series[&("/items/{id}".to_string(), "_OTHER".to_string(), 200)].count, 2);
    }

    #[test]
    fn a_label_value_is_escaped() {
        let mut series = BTreeMap::new();
        series.insert(("/a\"b\\c".to_string(), "GET".to_string(), 200), Cell::default());
        assert!(encode(&series, 0, 1, 0).contains(r#"route="/a\"b\\c""#));
    }

    fn headers(pairs: &[(&'static str, &'static str)]) -> HeaderMap {
        let mut map = HeaderMap::new();
        for (name, value) in pairs {
            map.insert(HeaderName::from_static(name), HeaderValue::from_static(value));
        }
        map
    }

    #[test]
    fn without_a_token_only_a_local_client_with_no_proxy_reads_the_metrics() {
        let metrics = Metrics::new("/metrics".into(), None, None);
        let local: Option<IpAddr> = Some([127, 0, 0, 1].into());
        let remote: Option<IpAddr> = Some([10, 0, 0, 7].into());
        assert_eq!(metrics.access(&headers(&[]), local), Access::Allow);
        assert_eq!(metrics.access(&headers(&[]), Some(std::net::Ipv6Addr::LOCALHOST.into())), Access::Allow);
        assert_eq!(metrics.access(&headers(&[]), remote), Access::Forbidden);
        assert_eq!(metrics.access(&headers(&[]), None), Access::Forbidden);
        assert_eq!(metrics.access(&headers(&[("x-forwarded-for", "203.0.113.9")]), local), Access::Forbidden);
        assert_eq!(metrics.access(&headers(&[("forwarded", "for=203.0.113.9")]), local), Access::Forbidden);
    }

    #[test]
    fn with_a_token_each_client_must_send_it() {
        let metrics = Metrics::new("/metrics".into(), Some("s3cret".into()), None);
        let remote: Option<IpAddr> = Some([10, 0, 0, 7].into());
        assert_eq!(metrics.access(&headers(&[("authorization", "Bearer s3cret")]), remote), Access::Allow);
        assert_eq!(metrics.access(&headers(&[("authorization", "Bearer wrong")]), remote), Access::Unauthorized);
        assert_eq!(metrics.access(&headers(&[("authorization", "s3cret")]), remote), Access::Unauthorized);
        // The token applies also to a local client.
        assert_eq!(metrics.access(&headers(&[]), Some([127, 0, 0, 1].into())), Access::Unauthorized);
    }

    #[test]
    fn a_scrape_adds_the_files_of_the_other_processes() {
        let dir = tempfile::tempdir().unwrap();
        let own = "metrics-1-1.json".to_string();
        let mut cell = Cell::default();
        cell.add(0.002);
        let other = ProcessFile {
            pid: 2,
            time: super::now_seconds(),
            in_flight: 3,
            series: vec![super::FileSeries { route: "/a".into(), method: "GET".into(), status: 200, cell }],
        };
        let gone = ProcessFile { pid: 3, time: 1.0, in_flight: 9, series: Vec::new() };
        std::fs::write(dir.path().join("metrics-2-1.json"), serde_json::to_vec(&other).unwrap()).unwrap();
        std::fs::write(dir.path().join("metrics-3-1.json"), serde_json::to_vec(&gone).unwrap()).unwrap();
        std::fs::write(dir.path().join("metrics-4-1.json"), b"{half").unwrap();
        std::fs::write(dir.path().join("other.json"), b"{}").unwrap();
        assert_eq!(read_process_files(dir.path(), &own).len(), 2);

        let metrics = Metrics::new("/metrics".into(), None, Some((dir.path().to_path_buf(), own.clone())));
        let text = metrics.render();
        assert!(text.contains(r#"http_server_requests_total{method="GET",route="/a",status="200"} 1"#), "{text}");
        // The process that stopped adds its counts, but not its requests in flight.
        assert!(text.contains("http_server_active_requests 3\n"), "{text}");
        assert!(text.contains(r#"freeman_metrics_processes{state="live"} 2"#), "{text}");
        assert!(text.contains(r#"freeman_metrics_processes{state="exited"} 1"#), "{text}");

        metrics.write_file(0);
        let written: ProcessFile = serde_json::from_slice(&std::fs::read(dir.path().join(&own)).unwrap()).unwrap();
        assert_eq!((written.pid, written.in_flight), (std::process::id(), 0));
    }
}
