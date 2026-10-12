"""Contract test for the logplot MCP server: drives a *running* instance over HTTP.

Covers the load transparency (W10), the window housekeeping (W7), the backpressure
(W8) and the contract smoke over the real logs (W11) of TODO.md's Phase 2.5.

Start the server first, then run this:

    logplot.bat --mcp-port 8791 --mcp-token smoke-token
    uv run mcp_test.py 8791 smoke-token logs/log-*.csv.zst

Needs no dependency beyond the stdlib; exits non-zero if any check fails.
"""
import itertools
import json
import os
import sys
import threading
import urllib.request

PORT, TOKEN = int(sys.argv[1]), sys.argv[2]
LOGS = sys.argv[3:]
BASE = f"http://127.0.0.1:{PORT}/mcp"
ROOT = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(ROOT, "_tmp")
_ids = itertools.count(1)

FAILURES = []
CHECKS = 0


def check(label, ok, detail=""):
    global CHECKS
    CHECKS += 1
    if ok:
        print(f"  ok   {label}")
    else:
        FAILURES.append(f"{label} -- {detail}")
        print(f"  FAIL {label} -- {detail}")


def rpc(method, params=None):
    body = json.dumps({"jsonrpc": "2.0", "id": next(_ids), "method": method,
                       "params": params or {}}).encode()
    req = urllib.request.Request(BASE, data=body, headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"})
    with urllib.request.urlopen(req, timeout=300) as resp:
        return json.loads(resp.read().decode())


def call(name, args):
    """One tools/call; returns the parsed payload or {'__error__': text}."""
    result = rpc("tools/call", {"name": name, "arguments": args})["result"]
    text = result["content"][0]["text"]
    if result.get("isError"):
        return {"__error__": text}
    return json.loads(text)


def close(a, b, tol=1e-6):
    return abs(a - b) <= tol


def write_fixture(name, text):
    os.makedirs(FIXTURES, exist_ok=True)
    path = os.path.join(FIXTURES, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def main():
    init = rpc("initialize", {"protocolVersion": "2025-06-18",
                              "capabilities": {}, "clientInfo": {"name": "m1-smoke", "version": "1"}})
    check("initialize", "result" in init, json.dumps(init)[:200])

    ts_only = write_fixture("ts_only.csv", "timestamp\n2026-01-01 00:00:00.000\n2026-01-01 00:00:01.000\n")
    ts_only_named = write_fixture("ts_only_named.csv", "ts\n2026-01-01 00:00:00.000\n2026-01-01 00:00:01.000\n")

    print("\n[W10] load_files / list_files")
    loaded = call("load_files", {"paths": LOGS})
    check("load_files loaded all", len(loaded.get("loaded", [])) == len(LOGS),
          json.dumps(loaded.get("failed"))[:400])
    check("load_files nothing failed", not loaded.get("failed"), json.dumps(loaded.get("failed"))[:400])

    files = {f["name"]: f for f in call("list_files", {})["files"]}
    check("list_files counts", len(files) == len(LOGS), f"{sorted(files)}")

    # Independently measured (see _tmp/measure.py). The TODO table rounds sampling to 1 ms
    # and the largest gap to the second, so compare at that precision.
    expected = {
        "log-20260101.log_mpc_ctrl_105.csv.zst": (125, 328746, 0.1, 22482, 34),
        "log-20260814_mpc_104.csv.zst": (205, 138470, 1.0, 1, 60),
        "log-fujiq-20260706_mpc_101.csv.zst": (210, 146760, 0.2, 1, 44),
        "log-20260101.log_msg_105.csv.zst": (1, 287960, 0.1, 22462, 0),
        "log-htb-20260101.log_mpc_ctrl_101.csv.zst": (125, 337010, 0.1, 22481, 31),
    }
    for name, want in expected.items():
        got = files.get(name)
        if got is None:
            check(f"{name} present", False, "not loaded")
            continue
        cols, rows, sampling, gap, constant = want
        check(f"{name} columns/rows", (got["columns"], got["rows"]) == (cols, rows),
              f"got {got['columns']}x{got['rows']}, want {cols}x{rows}")
        check(f"{name} sampling_median_s", round(got["sampling_median_s"], 3) == sampling,
              f"got {got['sampling_median_s']}, want ~{sampling}")
        check(f"{name} largest_gap_s", round(got["largest_gap_s"]) == gap,
              f"got {got['largest_gap_s']}, want ~{gap}")
        check(f"{name} constant_count", got["constant_count"] == constant,
              f"got {got['constant_count']}, want {constant}")
        check(f"{name} constant_columns length", len(got["constant_columns"]) == constant,
              f"got {len(got['constant_columns'])}")
        check(f"{name} monotonic/duplicates",
              got["monotonic"] is True and got["duplicate_timestamps"] == 0,
              f"monotonic={got['monotonic']} dup={got['duplicate_timestamps']}")
        check(f"{name} timestamp_parse", got["timestamp_parse"].endswith("%Y-%m-%d %H:%M:%S.%f"),
              got["timestamp_parse"])
        check(f"{name} timestamp_column", isinstance(got.get("timestamp_column"), str)
              and bool(got["timestamp_column"]), repr(got.get("timestamp_column")))
        check(f"{name} not ambiguous", "timestamp_ambiguous" not in got,
              f"flagged: {got.get('timestamp_ambiguous')}")
        print(f"    -> parse={got['timestamp_parse']} column={got['timestamp_column']!r} "
              f"sampling={got['sampling_median_s']} gap={got['largest_gap_s']} "
              f"constant={got['constant_count']}")

    print("\n[W10] failure reasons carry the real error")
    bad = call("load_files", {"paths": ["C:/nope/missing.csv", ts_only, ts_only_named]})
    reasons = {f["file"]: f["reason"] for f in bad.get("failed", [])}
    check("missing file reason", "FileNotFoundError" in reasons.get("C:/nope/missing.csv", ""),
          reasons.get("C:/nope/missing.csv"))
    check("timestamp-only reason (already named timestamp)",
          "no data columns" in reasons.get(ts_only, ""), reasons.get(ts_only))
    check("timestamp-only reason (renamed to timestamp)",
          "no data columns" in reasons.get(ts_only_named, ""), reasons.get(ts_only_named))

    print("\n[W10/W11] whole-file unchanged count agrees with the constant scan")
    for name, f in files.items():
        fc = call("find_changes", {"file": f["file"], "t1": f["start"] - 1, "t2": f["end"] + 1})
        check(f"{name} unchanged_series == constant_count",
              fc["unchanged_series"] == f["constant_count"],
              f"find_changes {fc['unchanged_series']} vs scan {f['constant_count']}")

    print("\n[W7] unchanged_columns is opt-in")
    target = files["log-htb-20260101.log_mpc_ctrl_101.csv.zst"]
    plain = call("find_changes", {"file": target["file"], "t1": target["start"], "t2": target["end"]})
    check("count always returned", plain["unchanged_series"] == target["constant_count"],
          plain["unchanged_series"])
    check("names absent by default", "unchanged_columns" not in plain,
          json.dumps(list(plain))[:200])
    asked = call("find_changes", {"file": target["file"], "t1": target["start"], "t2": target["end"],
                                  "include_unchanged": True})
    check("names on request", len(asked.get("unchanged_columns", [])) == asked["unchanged_series"],
          json.dumps(asked.get("unchanged_columns"))[:200])
    check("names are the constant ones",
          sorted(asked["unchanged_columns"]) == sorted(target["constant_columns"]),
          f"{asked.get('unchanged_columns')} vs {target['constant_columns']}")

    print("\n[W7] held_seconds is clamped to the last sample")
    f = files["log-20260101.log_mpc_ctrl_105.csv.zst"]
    t1, t2 = f["end"] - 5, f["end"] + 1800
    w = call("get_window", {"file": f["file"], "column": "mpc_ctrl_mode", "t1": t1, "t2": t2,
                            "mode": "transitions"})
    check("held_truncated flagged", w.get("held_truncated") is True, json.dumps(w)[:300])
    check("sample_end is the last sample", close(w["sample_end"], f["end"]), w["sample_end"])
    last = w["transitions"][-1]
    check("last held_seconds stops at the data end", close(last[3], f["end"] - last[0], 1e-3),
          f"held={last[3]}, data end - start = {f['end'] - last[0]}")
    check("last held_seconds not inflated towards t2", last[3] < 1800, f"held={last[3]}")

    w2 = call("get_window", {"file": f["file"], "column": "mpc_ctrl_mode",
                             "t1": f["end"] - 1800, "t2": f["end"], "mode": "transitions"})
    check("held_truncated absent inside the data", "held_truncated" not in w2,
          json.dumps(w2)[:300])
    if w2.get("transitions"):
        last2 = w2["transitions"][-1]
        check("last held_seconds reaches the window end", close(last2[3], f["end"] - last2[0], 1e-3),
              f"held={last2[3]}, want {f['end'] - last2[0]}")

    print("\n[W7] window_too_small")
    empty = call("get_window", {"file": f["file"], "column": "mpc_ctrl_mode",
                                "t1": f["end"] - 0.05, "t2": f["end"] - 0.02})
    check("no samples -> window_too_small",
          empty.get("window_too_small") is True and empty["samples"] == 0, json.dumps(empty)[:300])

    # A 1.0 s file: [end - 0.4, end] holds exactly one sample, the last one.
    slow = files["log-20260814_mpc_104.csv.zst"]
    slow_col = call("list_series", {"file": slow["file"]})["files"][0]["numeric_series"][0]
    single = call("get_window", {"file": slow["file"], "column": slow_col,
                                 "t1": slow["end"] - 0.4, "t2": slow["end"]})
    check("one sample -> window_too_small",
          single.get("window_too_small") is True and single["samples"] == 1, json.dumps(single)[:300])
    check("one sample still reports the value",
          len(single["segments"]) == 1, json.dumps(single)[:300])

    # Unix seconds are floats: a t1 == t2 probe can land beside the sample instead of on it.
    # Whatever happens, the reply has to say the window was too small.
    probe = call("get_window", {"file": f["file"], "column": "mpc_ctrl_mode",
                                "t1": f["end"], "t2": f["end"]})
    check("t1 == t2 -> window_too_small either way", probe.get("window_too_small") is True,
          json.dumps(probe)[:300])

    roomy = call("get_window", {"file": f["file"], "column": "mpc_ctrl_mode",
                                "t1": f["end"] - 60, "t2": f["end"]})
    check("a real window is not flagged", "window_too_small" not in roomy, json.dumps(roomy)[:300])

    print("\n[W7] min_duration still applies to transitions")
    deb = call("get_window", {"file": f["file"], "column": "mpc_ctrl_mode", "t1": 0, "t2": 4e9,
                              "mode": "transitions", "min_duration": 60})
    check("min_duration trims transitions", deb.get("segments_before", 0) > len(deb["transitions"]),
          f"before={deb.get('segments_before')} after={len(deb['transitions'])}")

    print("\n[W8] backpressure")
    busy_file = f["file"]
    results = []
    lock = threading.Lock()

    def hammer():
        out = call("find_changes", {"file": busy_file, "t1": 0, "t2": 4e9})
        with lock:
            results.append(out.get("__error__") or "ok")

    threads = [threading.Thread(target=hammer) for _ in range(24)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    busy = [r for r in results if "server busy" in r]
    served = [r for r in results if r == "ok"]
    other = sorted({r for r in results if r != "ok" and "server busy" not in r})
    check("some calls were served", len(served) >= 1, f"{len(served)} of {len(results)}")
    check("excess calls got a readable 'server busy'", len(busy) >= 1,
          f"busy={len(busy)} other={other}")
    print(f"    -> {len(served)} served, {len(busy)} rejected with 'server busy'")
    if busy:
        print(f"    -> {busy[0]}")

    print(f"\n{CHECKS - len(FAILURES)}/{CHECKS} checks passed")
    for line in FAILURES:
        print(f"FAILED: {line}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
