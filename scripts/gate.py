"""Throughput and memory gates for the engine.

Both start the example project as one process with one worker thread, drive it
with bombardier, and exit with status 1 when a limit is crossed.

    python scripts/gate.py perf --record     # write the baseline for this machine
    python scripts/gate.py perf              # fail under 90% of the baseline
    python scripts/gate.py memory            # fail when resident memory keeps growing

Throughput depends on the machine, so the baseline is per machine and is not
committed. CI compares two builds on one runner: record on the base revision,
then check on the head revision.
"""

import argparse
import contextlib
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "python" / "example"
BASELINE = ROOT / ".gate-baseline.json"
PORT = int(os.environ.get("GATE_PORT", "8791"))
BASE_URL = f"http://127.0.0.1:{PORT}"
# Path, connections. The first is the framework floor; the others add encoding and the error path.
PERF_CASES = {"root": ("/", 100), "json-10k": ("/10k-json", 100), "error": ("/exc", 100)}
MEMORY_PATHS = ["/", "/10k-json", "/exc", "/header", "/gate-missing"]


def bombardier() -> str:
    found = shutil.which("bombardier") or next(
        (str(p) for p in (Path.home() / "go/bin/bombardier", Path.home() / ".local/bin/bombardier") if p.exists()), None
    )
    if not found:
        sys.exit(
            "bombardier is not installed: brew install bombardier, or go install github.com/codesenberg/bombardier@latest"
        )
    return found


class Server:
    def __enter__(self):
        self.process = subprocess.Popen(
            [
                sys.executable,
                "manage.py",
                "runbolt",
                "--host",
                "127.0.0.1",
                "--port",
                str(PORT),
                "--processes",
                "1",
                "--workers",
                "1",
            ],
            cwd=EXAMPLE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        for _ in range(120):
            try:
                if urllib.request.urlopen(BASE_URL + "/", timeout=2).status == 200:
                    return self
            except OSError:
                time.sleep(0.25)
        self.__exit__()
        sys.exit("the example server did not start")

    def __exit__(self, *_):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
        self.process.wait()

    def rss_mb(self) -> float:
        out = subprocess.run(
            ["ps", "-o", "rss=", "-g", str(os.getpgid(self.process.pid))], capture_output=True, text=True
        )
        return sum(int(x) for x in out.stdout.split()) / 1024


def load(path: str, connections: int, duration: str) -> dict:
    out = subprocess.run(
        [bombardier(), "-c", str(connections), "-d", duration, "-o", "json", "-p", "r", BASE_URL + path],
        capture_output=True,
        text=True,
    )
    return json.loads(out.stdout)["result"]


def perf(args) -> int:
    results = {}
    with Server():
        for name, (path, connections) in PERF_CASES.items():
            load(path, connections, "2s")
            results[name] = max(
                round(load(path, connections, args.duration)["rps"]["mean"]) for _ in range(args.rounds)
            )
    if args.record:
        BASELINE.write_text(
            json.dumps({"machine": platform.node(), "python": platform.python_version(), "rps": results}, indent=2)
        )
        print(f"baseline written: {results}")
        return 0
    if not BASELINE.exists():
        sys.exit(f"no baseline at {BASELINE}; run with --record on the base revision first")
    base = json.loads(BASELINE.read_text())["rps"]
    failed = False
    for name, rps in results.items():
        ratio = rps / base[name]
        ok = ratio >= args.floor
        failed |= not ok
        print(f"{'ok  ' if ok else 'FAIL'} {name:<9} {rps:>8} rps, baseline {base[name]:>8}, {ratio:.0%}")
    return 1 if failed else 0


def memory(args) -> int:
    with Server() as server:
        procs = [
            subprocess.Popen(
                [bombardier(), "-c", "20", "-d", f"{args.seconds}s", "-p", "r", "-o", "json", BASE_URL + path],
                stdout=subprocess.DEVNULL,
            )
            for path in MEMORY_PATHS
        ]
        samples = []
        while any(p.poll() is None for p in procs):
            time.sleep(args.interval)
            samples.append(server.rss_mb())
            print(f"{len(samples) * args.interval:>5}s {samples[-1]:8.1f} MB", flush=True)
    if len(samples) < 6:
        sys.exit("too few samples; raise --seconds")
    # Warm-up allocations land in the first third. Growth is judged after it.
    third = len(samples) // 3
    middle, last = max(samples[third : 2 * third]), max(samples[2 * third :])
    growth = (last - middle) / middle
    ok = growth <= args.limit
    print(
        f"{'ok  ' if ok else 'FAIL'} middle third peak {middle:.1f} MB, last third peak {last:.1f} MB, growth {growth:+.1%} (limit {args.limit:+.0%})"
    )
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("perf")
    p.add_argument("--record", action="store_true")
    p.add_argument("--duration", default="8s")
    p.add_argument("--rounds", type=int, default=2)
    p.add_argument("--floor", type=float, default=0.9)
    p.set_defaults(run=perf)
    m = commands.add_parser("memory")
    m.add_argument("--seconds", type=int, default=180)
    m.add_argument("--interval", type=int, default=10)
    m.add_argument("--limit", type=float, default=0.05)
    m.set_defaults(run=memory)
    args = parser.parse_args()
    return args.run(args)


if __name__ == "__main__":
    sys.exit(main())
