#!/usr/bin/env python3
"""ArmyEye performance benchmark harness (Step 0).

ALL statistics live here. The production runtime exposes only primitive values
(capture_timestamp, read_wait_ms, failed_read_count, effective_device, thread_count);
every percentile, rate and rollup is computed in this script so the hot path stays free
of measurement work and the metric definitions can change without touching production.

What it does
------------
  create N benchmark pipelines  ->  start them  ->  sample the API at a fixed interval
  ->  stop and delete ONLY what it created  ->  write CSV + markdown report

Safety
------
  * Pipelines it creates are named with BENCH_PREFIX and are the only ones it ever
    starts, stops or deletes. Pre-existing pipelines are never touched.
  * It refuses to run if a pipeline with the bench prefix already exists unless
    --force-clean is given (prevents double-counting a previous aborted run).
  * Destinations default to `null` so no data leaves the node and no external
    dependency skews the numbers. Use --destination webhook to include publish cost.

Two benchmark modes (see the plan):
  A (compute)  --source-mode file   looped local video files. Sizes the inference budget.
  B (streaming) --source-mode rtsp  real RTSP. The only mode that validates decode,
                                    buffering, freshness and reconnect. A file-based pass
                                    is NOT production readiness.

Usage
-----
  python scripts/benchmark.py --cameras 1,5,10 --duration 120 --out baseline_cpu.md
  python scripts/benchmark.py --cameras 10,30,60 --duration 300 --source-mode rtsp \
      --rtsp-url rtsp://host/stream --out bench_b.md
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import statistics
import sys
import time
import urllib.parse
from typing import Any, Dict, List, Optional

try:
    import requests
except ImportError:  # pragma: no cover
    sys.exit("benchmark.py requires `requests` (pip install requests)")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BENCH_PREFIX = "BENCH_"
DEFAULT_BASE = os.environ.get("ARMYEYE_BASE_URL", "http://localhost:5555")


# --------------------------------------------------------------------------- env / auth
def load_env_file() -> Dict[str, str]:
    out: Dict[str, str] = {}
    path = os.path.join(REPO, ".env")
    if os.path.isfile(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k] = v
    return out


def login(base: str, user: str, password: str) -> requests.Session:
    s = requests.Session()
    r = s.get(f"{base}/login", timeout=30)
    r.raise_for_status()
    m = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', r.text)
    r = s.post(f"{base}/login",
               data={"username": user, "password": password, "csrf_token": m.group(1) if m else ""},
               allow_redirects=False, timeout=30)
    if r.status_code not in (302, 303):
        raise SystemExit(f"login failed ({r.status_code}) - check ADMIN_USERNAME/ADMIN_PASSWORD")
    page = s.get(f"{base}/", timeout=30)
    tok = re.search(r'name="csrf-token" content="([^"]+)"', page.text)
    s.headers["X-CSRFToken"] = tok.group(1) if tok else ""
    return s


# --------------------------------------------------------------------------- statistics
def pct(values: List[float], q: float) -> float:
    """Nearest-rank percentile: rank = ceil(q/100 * N), value at rank-1.
    Defined here, not in the runtime."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = math.ceil(q / 100.0 * len(ordered))
    idx = max(0, min(len(ordered) - 1, rank - 1))
    return ordered[idx]


def rate(first: Optional[Dict], last: Optional[Dict], key: str) -> float:
    """Counter delta / wall time between two samples -> per-second rate."""
    if not first or not last:
        return 0.0
    dt = last["t"] - first["t"]
    if dt <= 0:
        return 0.0
    return max(0.0, (last.get(key, 0) - first.get(key, 0))) / dt


# --------------------------------------------------------------------------- API helpers
def api(s: requests.Session, base: str, method: str, path: str, **kw) -> requests.Response:
    return getattr(s, method.lower())(f"{base}{path}", timeout=kw.pop("timeout", 60), **kw)


def list_pipelines(s: requests.Session, base: str) -> List[Dict[str, Any]]:
    r = api(s, base, "GET", "/api/pipelines")
    r.raise_for_status()
    data = r.json()
    items = data.get("pipelines", data)
    if isinstance(items, dict):
        return [dict(v, id=v.get("id", k)) for k, v in items.items()]
    return items or []


def pick_model(s: requests.Session, base: str, wanted: Optional[str]) -> str:
    r = api(s, base, "GET", "/api/models")
    r.raise_for_status()
    models = r.json().get("models", {})
    available = [mid for mid, m in models.items() if (m or {}).get("status") == "AVAILABLE"]
    if not available:
        raise SystemExit("no AVAILABLE model in the registry - cannot benchmark")
    if wanted:
        if wanted not in available:
            raise SystemExit(f"model {wanted!r} not AVAILABLE. Available: {available}")
        return wanted
    for mid in available:                       # prefer a general detector
        if "yolov8" in mid.lower():
            return mid
    return available[0]


def pick_media(s: requests.Session, base: str, wanted: Optional[str]) -> str:
    r = api(s, base, "GET", "/api/media/sources")
    r.raise_for_status()
    sources = r.json().get("sources", [])
    if not sources:
        raise SystemExit("no media files registered - add a video before benchmarking")
    if wanted:
        for src in sources:
            if src["relative_path"] == wanted:
                return wanted
        raise SystemExit(f"media {wanted!r} not found")
    return max(sources, key=lambda x: x.get("size_bytes", 0))["relative_path"]


def make_frame_source(args, media: str) -> Dict[str, Any]:
    """capture_type is the key the RUNTIME reads (pipeline_manager reads capture_type);
    sending `type` instead would silently fall back to webcam."""
    if args.source_mode == "rtsp":
        return {"capture_type": "ip_camera", "config": {"source": args.rtsp_url}}
    return {"capture_type": "video_file",
            "config": {"relative_source": media, "loop": True, "real_time": True}}


def create_pipelines(s, base, n, model_id, media, args) -> List[str]:
    ids = []
    frame_source = make_frame_source(args, media)
    destinations = ([] if args.destination == "none"
                    else [{"type": args.destination, "config": {}}])
    for i in range(n):
        cfg = {"name": f"{BENCH_PREFIX}{i:03d}",
               "description": "benchmark harness - safe to delete",
               "frame_source": frame_source,
               "model": {"id": model_id, "engine_type": "ultralytics", "device": args.device},
               "destinations": destinations}
        r = api(s, base, "POST", "/api/pipeline/create", json=cfg)
        if r.status_code not in (200, 201):
            cleanup(s, base, ids)
            raise SystemExit(f"create failed for camera {i}: {r.status_code} {r.text[:300]}")
        ids.append(r.json()["pipeline_id"])
    return ids


def start_pipelines(s, base, ids) -> List[str]:
    """First start after a container recreation is a COLD start - the model has never been
    loaded in that process. The server's start budget is ~10s, so we retry once before
    giving up; a cold-start timeout is not the same finding as a pipeline that cannot run."""
    started, failures = [], []
    for pid in ids:
        for attempt in (1, 2):
            r = api(s, base, "POST", f"/api/pipeline/{pid}/start", timeout=180)
            if r.status_code in (200, 201):
                started.append(pid)
                break
            if attempt == 1:
                print(f"  ~ start attempt 1 failed {pid[:8]}: {r.status_code} "
                      f"{r.text[:120]} - retrying (cold start?)")
                time.sleep(10)
            else:
                msg = f"{r.status_code} {r.text[:160]}"
                print(f"  ! start FAILED {pid[:8]}: {msg}")
                failures.append(msg)
    if failures:
        print(f"  ! {len(failures)} of {len(ids)} pipeline(s) never started")
    return started


def cleanup(s, base, ids) -> None:
    for pid in ids:
        try:
            api(s, base, "POST", f"/api/pipeline/{pid}/stop", timeout=60)
        except Exception:
            pass
    for pid in ids:
        try:
            api(s, base, "DELETE", f"/api/pipeline/{pid}", timeout=60)
        except Exception as e:
            print(f"  ! could not delete {pid[:8]}: {e}")


# --------------------------------------------------------------------------- sampling
def sample(s, base, own_ids: Optional[set] = None) -> Optional[Dict[str, Any]]:
    """Samples ONLY the pipelines this run created. Anything an operator starts in the UI
    mid-run must not enter the measurement - otherwise a foreign pipeline's device, latency
    and frame age silently contaminate the report."""
    try:
        m = api(s, base, "GET", "/api/pipelines/metrics", timeout=30).json()
        t = api(s, base, "GET", "/api/telemetry", timeout=30).json()
    except Exception as e:
        print(f"  ! sample failed: {e}")
        return None
    now = time.time()
    per_pipeline = {}
    for pid, pm in (m.get("running_pipelines") or {}).items():
        if own_ids is not None and pid not in own_ids:
            continue
        cap_ts = pm.get("capture_timestamp") or 0
        per_pipeline[pid] = {
            "frame_count": pm.get("frame_count", 0),
            "inference_count": pm.get("inference_count", 0),
            "latency_ms": pm.get("latency_ms", 0),
            "read_wait_ms": pm.get("read_wait_ms", 0),
            "failed_read_count": pm.get("failed_read_count", 0),
            "effective_device": pm.get("effective_device"),
            # frame age is DERIVED here from the raw capture timestamp
            "frame_age_ms": (now - cap_ts) * 1000.0 if cap_ts else None,
        }
    gpus = ((t.get("gpu") or {}).get("devices")) or []
    return {
        "t": now,
        "thread_count": m.get("thread_count", 0),
        "cpu": (t.get("metrics") or {}).get("cpu", 0),
        "ram": (t.get("metrics") or {}).get("memory", 0),
        "gpus": [{"id": g.get("id"), "name": g.get("name"),
                  "util": g.get("gpu_utilization_percent"),
                  "mem_used_gb": g.get("memory_used_gb"),
                  "mem_total_gb": g.get("memory_total_gb")} for g in gpus],
        "pipelines": per_pipeline,
    }


def summarize(samples: List[Dict], n_requested: int, n_started: int, duration: float) -> Dict[str, Any]:
    """Every statistic in the report is computed here."""
    # A level that produced nothing must still return the FULL key set, otherwise one
    # failed level destroys the report for the levels that did work.
    empty = {"cameras": n_requested, "started": n_started, "alive_at_end": 0,
             "capture_fps_per_cam": 0.0, "ai_fps_per_cam": 0.0, "aggregate_ai_fps": 0.0,
             "frame_age_p50": 0.0, "frame_age_p95": 0.0, "frame_age_p99": 0.0,
             "infer_latency_p95": 0.0, "read_wait_p50": 0.0, "cpu": 0.0, "ram": 0.0,
             "threads": 0, "drops": 0, "failures": n_requested, "devices": "n/a",
             "device_transitions": "",
             "per_gpu": {}, "stable": "NO DATA (no pipeline started)",
             "samples": 0, "duration_s": duration}
    live = [s for s in samples if s and s["pipelines"]]
    if not live:
        return empty
    first, last = live[0], live[-1]
    pids = sorted(set(first["pipelines"]) & set(last["pipelines"]))

    cap_rates, inf_rates = [], []
    for pid in pids:
        f = dict(first["pipelines"][pid], t=first["t"])
        l = dict(last["pipelines"][pid], t=last["t"])
        cap_rates.append(rate(f, l, "frame_count"))
        inf_rates.append(rate(f, l, "inference_count"))

    ages = [p["frame_age_ms"] for s in live for p in s["pipelines"].values()
            if p.get("frame_age_ms") is not None]
    lats = [p["latency_ms"] for s in live for p in s["pipelines"].values() if p.get("latency_ms")]
    waits = [p["read_wait_ms"] for s in live for p in s["pipelines"].values()
             if p.get("read_wait_ms") is not None]
    drops = sum(max(0, last["pipelines"][p]["failed_read_count"] -
                    first["pipelines"][p]["failed_read_count"]) for p in pids)
    # Steady state is what characterises the run; anything else observed is reported
    # separately as a transition rather than blended into one ambiguous cell.
    steady = sorted({str(p.get("effective_device")) for p in last["pipelines"].values()
                     if p.get("effective_device")})
    all_seen = sorted({str(p.get("effective_device")) for s in live
                       for p in s["pipelines"].values() if p.get("effective_device")})
    devices = ",".join(steady) or "unknown"
    device_transitions = [d for d in all_seen if d not in steady]

    per_gpu = {}
    for s in live:
        for g in s["gpus"]:
            per_gpu.setdefault(g["id"], {"name": g["name"], "util": [], "mem": [],
                                         "total": g["mem_total_gb"]})
            if g["util"] is not None:
                per_gpu[g["id"]]["util"].append(g["util"])
            if g["mem_used_gb"] is not None:
                per_gpu[g["id"]]["mem"].append(g["mem_used_gb"])

    # "Stable" = every started pipeline still reporting at the end, and frame age did not
    # trend upward across the run (a growing backlog is a failure even at good FPS).
    half = len(live) // 2 or 1
    early = [p["frame_age_ms"] for s in live[:half] for p in s["pipelines"].values()
             if p.get("frame_age_ms") is not None]
    late = [p["frame_age_ms"] for s in live[half:] for p in s["pipelines"].values()
            if p.get("frame_age_ms") is not None]
    growing = bool(early and late and pct(late, 95) > max(2 * pct(early, 95), pct(early, 95) + 1000))
    alive = len(last["pipelines"])

    return {
        "cameras": n_requested, "started": n_started, "alive_at_end": alive,
        "capture_fps_per_cam": statistics.mean(cap_rates) if cap_rates else 0.0,
        "ai_fps_per_cam": statistics.mean(inf_rates) if inf_rates else 0.0,
        "aggregate_ai_fps": sum(inf_rates),
        "frame_age_p50": pct(ages, 50), "frame_age_p95": pct(ages, 95), "frame_age_p99": pct(ages, 99),
        "infer_latency_p95": pct(lats, 95),
        "read_wait_p50": pct(waits, 50),
        "cpu": statistics.mean([s["cpu"] for s in live]) if live else 0,
        "ram": statistics.mean([s["ram"] for s in live]) if live else 0,
        "threads": max(s["thread_count"] for s in live),
        "drops": drops,
        "failures": max(0, n_started - alive),
        "devices": devices,
        "device_transitions": ",".join(device_transitions),
        "per_gpu": {gid: {"name": v["name"],
                          "util_mean": statistics.mean(v["util"]) if v["util"] else None,
                          "mem_used_max": max(v["mem"]) if v["mem"] else None,
                          "mem_total": v["total"]} for gid, v in per_gpu.items()},
        "stable": "no (frame age growing)" if growing else ("no (pipelines died)" if alive < n_started else "yes"),
        "samples": len(live), "duration_s": duration,
    }


# --------------------------------------------------------------------------- reporting
def gpu_cell(row: Dict, field: str) -> str:
    if not row.get("per_gpu"):
        return "n/a"
    parts = []
    for gid, g in sorted(row["per_gpu"].items(), key=lambda kv: (kv[0] is None, kv[0])):
        if field == "util":
            v = g["util_mean"]
            parts.append(f"gpu{gid}:{v:.0f}%" if v is not None else f"gpu{gid}:n/a")
        else:
            u, t = g["mem_used_max"], g["mem_total"]
            parts.append(f"gpu{gid}:{u:.1f}/{t:.1f}G" if u is not None else f"gpu{gid}:n/a")
    return " ".join(parts)


def write_report(rows: List[Dict], args, meta: Dict, out_path: str) -> None:
    hdr = ("| Cameras | Capture FPS/cam | AI FPS/cam | Aggregate AI FPS | Frame age p50 | **Frame age p95** | "
           "Frame age p99 | Infer latency p95 | GPU util | VRAM | CPU % | RAM % | Drops | Failures | Threads | "
           "Device actually used | Stable? |")
    sep = "|" + "---|" * 17
    lines = [
        f"# ArmyEye benchmark — {meta['label']}",
        "",
        f"- Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Mode: **{'B (production streaming, RTSP)' if args.source_mode == 'rtsp' else 'A (compute, looped local files)'}**",
        f"- Host: `{args.base}` · device requested: `{args.device}` · model: `{meta['model']}`",
        f"- Source: `{meta['source']}` · destination: `{args.destination}`",
        f"- Duration per level: {args.duration}s · sample interval: {args.interval}s",
        "",
    ]
    if args.source_mode != "rtsp":
        lines += ["> **Mode A only sizes the inference budget.** Local files bypass the RTSP network path "
                  "entirely — no decode jitter, packet loss, reconnects or buffering. A pass here is NOT "
                  "production readiness; that requires Mode B.", ""]
    lines += [hdr, sep]
    for r in rows:
        lines.append(
            f"| {r['cameras']} | {r['capture_fps_per_cam']:.1f} | {r['ai_fps_per_cam']:.1f} | "
            f"{r['aggregate_ai_fps']:.1f} | {r['frame_age_p50']:.0f} ms | **{r['frame_age_p95']:.0f} ms** | "
            f"{r['frame_age_p99']:.0f} ms | {r['infer_latency_p95']:.1f} ms | {gpu_cell(r,'util')} | "
            f"{gpu_cell(r,'mem')} | {r['cpu']:.0f} | {r['ram']:.0f} | {r['drops']} | {r['failures']} | "
            f"{r['threads']} | `{r['devices']}` | {r['stable']} |")
    lines += ["", "## Notes", "",
              "- All percentiles/rates computed in `scripts/benchmark.py`; the runtime exposes only raw primitives.",
              "- `frame_age` = sample time − last capture timestamp. It measures pipeline staleness.",
              "- `read_wait` p50 per level: " + ", ".join(f"{r['cameras']}cam={r['read_wait_p50']:.1f}ms" for r in rows),
              "  A read that returns ~instantly while the source is live means a buffered backlog is being",
              "  drained (stale frames); a read that blocks ≈1/fps means we are at the live edge.",
              "- `Device actually used` is the STEADY-STATE device read from the loaded model, "
              "not from configuration; only this run's own pipelines are sampled.",
              "- Other device values seen transiently: " +
              (", ".join(f"{r['cameras']}cam=[{r['device_transitions']}]" for r in rows
                         if r.get("device_transitions")) or "none"),
              ""]
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    csv_path = os.path.splitext(out_path)[0] + ".csv"
    keys = [k for k in rows[0] if k != "per_gpu"] if rows else []
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for r in rows:
            w.writerow({k: r[k] for k in keys})
    print(f"\nReport : {out_path}\nCSV    : {csv_path}")


# --------------------------------------------------------------------------- main
def run_level(s, base, n, model_id, media, args) -> Dict[str, Any]:
    print(f"\n=== {n} camera(s) ===")
    ids = create_pipelines(s, base, n, model_id, media, args)
    try:
        started = start_pipelines(s, base, ids)
        print(f"  started {len(started)}/{n}; warmup {args.warmup}s")
        time.sleep(args.warmup)
        samples, t_end = [], time.time() + args.duration
        while time.time() < t_end:
            snap = sample(s, base, own_ids=set(ids))
            if snap:
                samples.append(snap)
                ages = [p["frame_age_ms"] for p in snap["pipelines"].values() if p.get("frame_age_ms")]
                print(f"  t+{int(time.time()-(t_end-args.duration)):>4}s  live={len(snap['pipelines'])} "
                      f"cpu={snap['cpu']:.0f}% threads={snap['thread_count']} "
                      f"age_p95={pct(ages,95):.0f}ms", flush=True)
            time.sleep(args.interval)
        return summarize(samples, n, len(started), args.duration)
    finally:
        print("  cleaning up…")
        cleanup(s, base, ids)


def main() -> int:
    ap = argparse.ArgumentParser(description="ArmyEye benchmark harness")
    ap.add_argument("--cameras", default="1,5,10", help="comma-separated camera counts")
    ap.add_argument("--duration", type=int, default=120, help="measurement seconds per level")
    ap.add_argument("--warmup", type=int, default=15, help="seconds to settle before measuring")
    ap.add_argument("--interval", type=float, default=2.0, help="sample interval seconds")
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--device", default="cpu", help="cpu | cuda:0 | GPU …")
    ap.add_argument("--model", default=None)
    ap.add_argument("--media", default=None, help="relative media path (mode A)")
    ap.add_argument("--source-mode", choices=("file", "rtsp"), default="file")
    ap.add_argument("--rtsp-url", default=None)
    ap.add_argument("--destination", choices=("none", "null", "webhook"), default="null")
    ap.add_argument("--out", default="benchmark.md")
    ap.add_argument("--label", default="baseline")
    ap.add_argument("--force-clean", action="store_true", help="remove leftover BENCH_ pipelines first")
    args = ap.parse_args()

    if args.source_mode == "rtsp" and not args.rtsp_url:
        return int(bool(print("--source-mode rtsp requires --rtsp-url")))

    env = load_env_file()
    user = os.environ.get("ARMYEYE_ADMIN_USERNAME") or env.get("ADMIN_USERNAME")
    pw = os.environ.get("ARMYEYE_ADMIN_PASSWORD") or env.get("ADMIN_PASSWORD")
    if not user or not pw:
        return int(bool(print("set ADMIN_USERNAME/ADMIN_PASSWORD (in .env or environment)")))

    s = login(args.base, user, pw)
    leftovers = [p for p in list_pipelines(s, args.base) if str(p.get("name", "")).startswith(BENCH_PREFIX)]
    if leftovers:
        if not args.force_clean:
            return int(bool(print(f"{len(leftovers)} leftover {BENCH_PREFIX}* pipeline(s) exist. "
                                  f"Re-run with --force-clean to remove them first.")))
        print(f"removing {len(leftovers)} leftover benchmark pipeline(s)…")
        cleanup(s, args.base, [p["id"] for p in leftovers])

    model_id = pick_model(s, args.base, args.model)
    media = pick_media(s, args.base, args.media) if args.source_mode == "file" else args.rtsp_url
    print(f"model={model_id}  source={media}  device={args.device}  mode={args.source_mode}")

    rows = []
    for n in [int(x) for x in args.cameras.split(",") if x.strip()]:
        rows.append(run_level(s, args.base, n, model_id, media, args))
    write_report(rows, args, {"label": args.label, "model": model_id, "source": media}, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
