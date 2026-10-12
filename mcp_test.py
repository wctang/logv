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
import statistics
import sys
import threading
import time
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
    t0 = time.perf_counter()
    loaded = call("load_files", {"paths": LOGS})
    print(f"    -> load_files {len(LOGS)} files: {time.perf_counter() - t0:.2f} s")
    check("load_files loaded all", len(loaded.get("loaded", [])) == len(LOGS),
          json.dumps(loaded.get("failed"))[:400])
    check("load_files nothing failed", not loaded.get("failed"), json.dumps(loaded.get("failed"))[:400])

    t0 = time.perf_counter()
    listing = call("list_files", {})
    print(f"    -> list_files, first scan of every file: {time.perf_counter() - t0:.2f} s")
    files = {f["name"]: f for f in listing["files"]}
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

    print("\n[W4] mode=summary group_by=transition")
    f105 = files["log-20260101.log_mpc_ctrl_105.csv.zst"]
    f101 = files["log-htb-20260101.log_mpc_ctrl_101.csv.zst"]

    def whole_window(f, column="mpc_ctrl_mode", **extra):
        args = {"file": f["file"], "column": column,
                "t1": f["start"] - 1, "t2": f["end"] + 1, "max_points": 5000}
        args.update(extra)
        return call("get_window", args)

    def reduce_transitions(rows):
        """The same reduction mode=summary promises, computed here from the exact rows."""
        groups = {}
        for _t, frm, to, held in rows:
            entry = groups.setdefault((str(frm), str(to)), [])
            entry.append((frm, to, held))
        return {key: ([(r[0], r[1], r[2]) for r in rows], len(rows),
                      min(r[2] for r in rows), statistics.median([r[2] for r in rows]),
                      max(r[2] for r in rows))
                for key, rows in groups.items()}

    for label, f in (("105", f105), ("101", f101)):
        rows = whole_window(f, mode="transitions")["transitions"]
        local = reduce_transitions(rows)
        summary = whole_window(f, mode="summary", group_by="transition")
        # The transitions reply has one row per run (the first one carrying from=null), so its
        # length is the run count, not the change count.
        check(f"{label} summary segments == exact rows",
              summary["segments"] == len(rows), f"{summary['segments']} vs {len(rows)}")
        check(f"{label} summary transitions == exact rows - 1",
              summary["transitions"] == len(rows) - 1, f"{summary['transitions']} vs {len(rows) - 1}")
        check(f"{label} summary group_count == local groups",
              summary["group_count"] == len(local), f"{summary['group_count']} vs {len(local)}")
        mismatched = []
        for group in summary["groups"]:
            key = (str(group["from"]), str(group["to"]))
            if key not in local:
                mismatched.append((key, "missing"))
                continue
            _r, count, lo, mid, hi = local[key]
            if (group["count"] != count or not close(group["held_min_s"], lo, 1e-6)
                    or not close(group["held_median_s"], mid, 1e-6)
                    or not close(group["held_max_s"], hi, 1e-6)):
                mismatched.append((key, group, count, lo, mid, hi))
        check(f"{label} every group matches the exact transitions", not mismatched,
              json.dumps(mismatched)[:400])
        check(f"{label} groups are not truncated", "groups_truncated" not in summary, "")

        halts = [g for g in summary["groups"] if g["from"] == "playing" and g["to"] == "halt"]
        check(f"{label} playing->halt count is 3", len(halts) == 1 and halts[0]["count"] == 3,
              json.dumps(halts)[:200])
        print(f"    -> {label}: {summary['group_count']} groups, "
              f"playing->halt {halts[0]['count'] if halts else '?'}")

    s101 = whole_window(f101, mode="summary", group_by="transition")
    check("101 has 14 transition groups", s101["group_count"] == 14, s101["group_count"])

    # suggest.md's three aborts and the normal play length, read off the exact transitions:
    # a transition's held_seconds is how long the *new* value lasted, so the play length is
    # the held_seconds of the row that entered 'playing'.
    plays = sorted(row[3] for row in whole_window(f105, mode="transitions")["transitions"]
                   if row[1] == "ready" and row[2] == "playing")
    check("105 has 76 plays", len(plays) == 76, len(plays))
    normal = plays[3:]
    check("105 three aborts match suggest.md",
          all(close(got, want, 0.01) for got, want in
              zip(plays[:3], (27.388927, 28.651019, 287.684))), plays[:3])
    check("105 normal play length 349.852-351.847",
          close(normal[0], 349.852, 0.01) and close(normal[-1], 351.847, 0.01),
          (normal[0], normal[-1]))
    print(f"    -> 105 aborts {[round(v, 6) for v in plays[:3]]}, "
          f"normal {normal[0]:.3f}-{normal[-1]:.3f} s")

    plays101 = sorted(row[3] for row in whole_window(f101, mode="transitions")["transitions"]
                      if row[1] == "ready" and row[2] == "playing")
    check("101 has 78 plays", len(plays101) == 78, len(plays101))
    check("101 normal play length 350.260-351.457",
          close(plays101[3], 350.260, 0.01) and close(plays101[-1], 351.457, 0.01),
          (plays101[3], plays101[-1]))
    print(f"    -> 101 aborts {[round(v, 6) for v in plays101[:3]]}, "
          f"normal {plays101[3]:.3f}-{plays101[-1]:.3f} s")

    print("\n[W4] mode=summary group_by=value")
    rows = whole_window(f105, mode="transitions")["transitions"]
    values = whole_window(f105, mode="summary", group_by="value")
    local = {}
    for _t, _frm, to, held in rows:
        entry = local.setdefault(str(to), {"count": 0, "seconds": 0.0})
        entry["count"] += 1
        entry["seconds"] += held
    check("value groups match the exact transitions",
          values["group_count"] == len(local)
          and all(close(g["seconds"], local[str(g["value"])]["seconds"], 1e-3)
                  and g["count"] == local[str(g["value"])]["count"] for g in values["groups"]),
          json.dumps([(g["value"], g["count"], g["seconds"],
                       local.get(str(g["value"]))) for g in values["groups"]])[:400])
    check("value seconds cover the window",
          close(sum(g["seconds"] for g in values["groups"]), f105["end"] - f105["start"], 0.01),
          sum(g["seconds"] for g in values["groups"]))
    check("value counts cover the runs",
          sum(g["count"] for g in values["groups"]) == values["segments"], values["segments"])
    check("value groups are ranked by time held",
          all(values["groups"][i]["seconds"] >= values["groups"][i + 1]["seconds"]
              for i in range(len(values["groups"]) - 1)), "")
    print(f"    -> value groups {[(g['value'], g['count'], round(g['seconds'], 1)) for g in values['groups']]}")

    print("\n[W4] summary argument validation")
    check("summary needs group_by",
          "__error__" in whole_window(f105, mode="summary"), "no error")
    check("group_by needs summary",
          "__error__" in whole_window(f105, mode="segments", group_by="value"), "no error")
    check("unknown group_by",
          "__error__" in whole_window(f105, mode="summary", group_by="pair"), "no error")

    print("\n[W1] list_series detail")
    for name in ("log-20260101.log_mpc_ctrl_105.csv.zst", "log-20260814_mpc_104.csv.zst"):
        f = files[name]
        t0 = time.perf_counter()
        call("list_series", {"file": f["file"]})              # warms the distinct/kind caches
        warm = time.perf_counter() - t0
        t0 = time.perf_counter()
        listed = call("list_series", {"file": f["file"], "detail": True})["files"][0]
        first = time.perf_counter() - t0
        t0 = time.perf_counter()
        call("list_series", {"file": f["file"], "detail": True})
        again = time.perf_counter() - t0
        print(f"    -> {name}: list_series {warm * 1000:.0f} ms, "
              f"detail {first * 1000:.0f} ms, detail again {again * 1000:.0f} ms")

        facts = listed["facts"]
        check(f"{name} facts cover every listed series",
              set(facts) == set(listed["numeric_series"]) | set(listed["string_series"]),
              f"{len(facts)} vs {len(listed['numeric_series']) + len(listed['string_series'])}")
        constant = {n for n, fact in facts.items() if fact.get("constant")}
        check(f"{name} constant facts are exactly constant_columns",
              constant == set(f["constant_columns"]),
              f"{len(constant)} vs {f['constant_count']}")
        check(f"{name} constants report no changes",
              all(facts[n].get("changes") == 0 for n in constant), "")
        related = set()
        for fact in facts.values():
            if "duplicate_of" in fact:
                related.add(fact["duplicate_of"])
            related.update(fact.get("same_timing_as", []))
        check(f"{name} a constant series is never called synchronised",
              not (related & constant), sorted(related & constant)[:5])
        check(f"{name} relations are not self-referential",
              all(name_ not in fact.get("same_timing_as", []) for name_, fact in facts.items()), "")

    facts105 = call("list_series", {"file": f105["file"], "detail": True})["files"][0]["facts"]
    check("mpc_ctrl_mode changes == 333", facts105["mpc_ctrl_mode"]["changes"] == 333,
          facts105["mpc_ctrl_mode"])
    check("mpc_ctrl_state changes == 333", facts105["mpc_ctrl_state"]["changes"] == 333,
          facts105["mpc_ctrl_state"])
    check("state and mode share their timing",
          "mpc_ctrl_mode" in facts105["mpc_ctrl_state"].get("same_timing_as", []),
          facts105["mpc_ctrl_state"])
    busiest = sorted(n for n, fact in facts105.items() if fact.get("changes") == 280140)
    print(f"    -> series with 280140 changes: {busiest}")
    check("the three 280140 counters are exactly the time counters",
          busiest == ["mpc_ctrl_t_elapsed", "mpc_ctrl_t_play", "mpc_ctrl_t_replay_left"],
          busiest)
    # Verified elementwise with pandas (see the M2 record in TODO.md): the three hold the very
    # same values in this file, so the earliest is the keeper and the other two are duplicates.
    check("t_elapsed and t_replay_left are duplicates of t_play",
          facts105["mpc_ctrl_t_elapsed"].get("duplicate_of") == "mpc_ctrl_t_play"
          and facts105["mpc_ctrl_t_replay_left"].get("duplicate_of") == "mpc_ctrl_t_play",
          {n: facts105[n] for n in ("mpc_ctrl_t_elapsed", "mpc_ctrl_t_replay_left")})
    check("mpc_ctrl_over_travel_2 is a duplicate of mpc_ctrl_over_travel_1",
          facts105["mpc_ctrl_over_travel_2"].get("duplicate_of") == "mpc_ctrl_over_travel_1",
          facts105["mpc_ctrl_over_travel_2"])
    check("mpc_ctrl_cali_2 is a duplicate of mpc_ctrl_cali_1",
          facts105["mpc_ctrl_cali_2"].get("duplicate_of") == "mpc_ctrl_cali_1",
          facts105["mpc_ctrl_cali_2"])
    check("hal_pose_home_1 is a duplicate of mpc_ctrl_home_1",
          facts105["hal_pose_home_1"].get("duplicate_of") == "mpc_ctrl_home_1",
          facts105["hal_pose_home_1"])
    check("a duplicate is not also listed as merely synchronised",
          all(facts105[n].get("duplicate_of") not in facts105[n].get("same_timing_as", [])
              for n in ("mpc_ctrl_t_elapsed", "mpc_ctrl_over_travel_2", "mpc_ctrl_cali_2")),
          "")
    # pandas says over_travel_1 and over_travel_3 differ (7 runs vs their own), so nothing
    # may claim they are aliases.
    check("a series that differs is not claimed as an alias",
          "duplicate_of" not in facts105["mpc_ctrl_over_travel_3"]
          and "same_timing_as" not in facts105["mpc_ctrl_over_travel_3"],
          facts105["mpc_ctrl_over_travel_3"])

    # A duplicate claim has to hold in the segments reply too, which is computed by an
    # entirely different path from the change signature that produced the claim.
    play_start = min(row[0] for row in whole_window(f105, mode="transitions")["transitions"]
                     if row[1] == "ready" and row[2] == "playing")
    segment_window = {"file": f105["file"], "t1": play_start, "t2": play_start + 5}
    plays_seg = call("get_window", dict(segment_window, column="mpc_ctrl_t_play", max_points=5000))
    elapsed_seg = call("get_window", dict(segment_window, column="mpc_ctrl_t_elapsed",
                                          max_points=5000))
    check("the duplicate claim holds in a real window",
          plays_seg["mode"] == "segments" and len(plays_seg["segments"]) > 10
          and plays_seg["segments"] == elapsed_seg["segments"],
          f"{plays_seg['mode']}, {len(plays_seg.get('segments', []))} runs")

    signature = facts105.get("mpc_ctrl_curr_motion_signature")
    check("one distinct value is not the same as never changing",
          signature is not None and signature["distinct"] == 1 and signature["changes"] > 0
          and "constant" not in signature, json.dumps(signature))

    msg_facts = call("list_series", {"file": files["log-20260101.log_msg_105.csv.zst"]["file"],
                                     "detail": True})["files"][0]
    check("msg distinct == 45273", msg_facts["facts"]["msg"]["distinct"] == 45273,
          msg_facts["facts"]["msg"])
    print(f"    -> msg column facts: {json.dumps(msg_facts['facts'])}")

    print("    -> relations of interest:")
    for column in ("mpc_ctrl_state", "mpc_ctrl_mode", "over_travel_1", "over_travel_2",
                   "mpc_ctrl_curr_motion_signature"):
        if column in facts105:
            print(f"       {column}: {json.dumps(facts105[column])}")

    print("\n[W8] backpressure")
    busy_file = f105["file"]
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
