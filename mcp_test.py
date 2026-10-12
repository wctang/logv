"""Contract test for the logplot MCP server: drives a *running* instance over HTTP.

Covers the load transparency (W10), the window housekeeping (W7), the backpressure
(W8) and the contract smoke over the real logs (W11) of TODO.md's Phase 2.5, plus the
GUI control tools (M4 / W9).

Start the server first, then run this:

    logplot.bat --mcp-port 8791 --mcp-token smoke-token
    uv run mcp_test.py 8791 smoke-token logs/log-*.csv.zst

Needs no dependency beyond the stdlib; exits non-zero if any check fails.
"""
import itertools
import base64
import json
import os
import re
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

    # M3 turned t1 == t2 into a snapshot (the last sample at or before t2), so asking for
    # segments explicitly is now the only way to see the old "one instant is too small" answer.
    explicit = call("get_window", {"file": f["file"], "column": "mpc_ctrl_mode",
                                   "t1": f["end"], "t2": f["end"], "mode": "segments"})
    check("explicit segments still flags a one-instant window",
          explicit.get("window_too_small") is True, json.dumps(explicit)[:300])

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

    print("\n[W3] snapshot and multi-column reads")
    aborts = [row for row in whole_window(f105, mode="transitions")["transitions"]
              if row[1] == "playing" and row[2] == "halt"]
    check("105 has three playing->halt rows", len(aborts) == 3, len(aborts))
    instant = aborts[0][0] + 0.001        # 1 ms past the halt, safely inside that sample

    def find_name(suffix):
        found = sorted(n for n in facts105 if n == suffix or n.endswith("_" + suffix))
        return found[0] if found else None

    fingerprint = [name for name in ("mpc_ctrl_mode", "mpc_ctrl_state", find_name("t_play"),
                                     find_name("run_1"), find_name("st_1"), find_name("en_1"),
                                     "alarm_data") if name]
    check("the abort fingerprint signals all exist", len(fingerprint) == 7, fingerprint)
    print(f"    -> snapshot columns: {fingerprint}")

    snap = call("get_window", {"file": f105["file"], "columns": fingerprint,
                              "t1": instant, "t2": instant})
    entries = {entry["column"]: entry for entry in snap["columns"]}
    check("one call reads all seven", len(entries) == len(fingerprint), list(entries))
    check("t1 == t2 gives a snapshot",
          all(entry["mode"] == "snapshot" for entry in entries.values()),
          [entry["mode"] for entry in entries.values()])
    check("every column reports the same sample",
          len({entry["sample_time"] for entry in entries.values()}) == 1,
          {e["column"]: e["sample_time"] for e in entries.values()})
    check("mode is halt and state is 12 at the abort",
          entries["mpc_ctrl_mode"]["value"] == "halt" and entries["mpc_ctrl_state"]["value"] == 12,
          {name: entries[name]["value"] for name in ("mpc_ctrl_mode", "mpc_ctrl_state")})
    check("alarm_data carries the halt command",
          "Halt command received" in str(entries["alarm_data"]["value"]),
          str(entries["alarm_data"]["value"])[:160])
    check("t_play is back to zero at the abort",
          entries[find_name("t_play")]["value"] == 0, entries[find_name("t_play")]["value"])
    print(f"    -> {json.dumps({name: entry['value'] for name, entry in entries.items()})[:400]}")

    check("an explicit snapshot mode works away from t1 == t2",
          call("get_window", {"file": f105["file"], "column": "mpc_ctrl_mode", "mode": "snapshot",
                              "t1": instant - 600, "t2": instant})["value"] == "halt", "")
    check("a snapshot before the first sample says so",
          call("get_window", {"file": f105["file"], "column": "mpc_ctrl_mode",
                              "t1": f105["start"] - 3600, "t2": f105["start"] - 3600}
               ).get("no_sample") is True, "")
    check("giving both column and columns is an error",
          "__error__" in call("get_window", {"file": f105["file"], "column": "mpc_ctrl_mode",
                                             "columns": ["mpc_ctrl_state"],
                                             "t1": instant, "t2": instant}), "")
    check("an unknown name in columns is an error",
          "__error__" in call("get_window", {"file": f105["file"], "columns": ["mpc_ctrl_mode", "nope"],
                                             "t1": instant, "t2": instant}), "")
    multi = call("get_window", {"file": f105["file"],
                                "columns": ["mpc_ctrl_mode", "mpc_ctrl_state"],
                                "t1": f105["start"] - 1, "t2": f105["end"] + 1,
                                "mode": "summary", "group_by": "value"})
    check("columns works for other modes too",
          len(multi["columns"]) == 2
          and all(entry["group_count"] == 7 for entry in multi["columns"]),
          [entry.get("group_count") for entry in multi["columns"]])

    print("\n[W5] mode=aggregate")
    t_play = find_name("t_play")
    # A play runs about 350 s and the counter keeps its value when one completes normally, so
    # the shape to look for is the abort: five minutes spanning abort C's whole play, where the
    # counter climbs to 81.6 % of the full length and the halt zeroes it.
    instant_c = aborts[2][0]
    rows = whole_window(f105, mode="transitions")["transitions"]
    c_start = max(row[0] for row in rows if row[2] == "playing" and row[0] < instant_c)
    aggregate = call("get_window", {"file": f105["file"], "column": t_play, "mode": "aggregate",
                                    "every": 10, "t1": c_start + 10, "t2": c_start + 310})
    buckets = aggregate["buckets"]
    maxima = [row["max"] for row in buckets]
    check("buckets span the window in one step",
          29 <= aggregate["bucket_count"] <= 31, aggregate["bucket_count"])
    check("bucket counts add up to the window's samples",
          sum(row["count"] for row in buckets) == aggregate["samples"],
          f"{sum(row['count'] for row in buckets)} vs {aggregate['samples']}")
    check("buckets start on multiples of every since the epoch",
          all(row["t"] % 10 == 0 for row in buckets), [row["t"] for row in buckets[:4]])
    check("every aggregate brackets the first and last sample of its bucket",
          all(row["min"] <= row[key] <= row["max"]
              for row in buckets for key in ("first", "last") if key in row),
          json.dumps([row for row in buckets
                      if not (row["min"] <= row["last"] <= row["max"])])[:300])
    check("the climb starts from near zero", buckets[0]["min"] < 20000, buckets[0])
    peak = max(range(len(buckets)), key=lambda i: maxima[i])
    check("the counter climbs without ever falling back",
          all(maxima[i] <= maxima[i + 1] for i in range(peak)),
          [(maxima[i], maxima[i + 1]) for i in range(peak) if maxima[i] > maxima[i + 1]][:3])
    check("it reaches 81.6 % of the full length, not the full 346 s",
          280000 <= maxima[peak] <= 295000, maxima[peak])
    check("the halt zeroes it by the end of the window",
          buckets[-1]["last"] == 0 and buckets[-1]["max"] == 0, buckets[-1])
    print(f"    -> {aggregate['bucket_count']} buckets of {aggregate['every']}s: "
          f"first {buckets[0]}, peak {maxima[peak]} at bucket {peak}, last {buckets[-1]}")

    check("agg picks which aggregates come back",
          set(call("get_window", {"file": f105["file"], "column": t_play, "mode": "aggregate",
                                  "every": 30, "agg": ["last"],
                                  "t1": play_start + 60, "t2": play_start + 360})["buckets"][0])
          == {"t", "count", "last"}, "")
    check("aggregate needs every",
          "__error__" in call("get_window", {"file": f105["file"], "column": t_play,
                                             "mode": "aggregate", "t1": 0, "t2": 4e9}), "")
    check("every only applies to aggregate",
          "__error__" in call("get_window", {"file": f105["file"], "column": t_play,
                                             "every": 10, "t1": 0, "t2": 4e9}), "")
    check("agg is validated",
          "__error__" in call("get_window", {"file": f105["file"], "column": t_play,
                                             "mode": "aggregate", "every": 10, "agg": ["mean"],
                                             "t1": 0, "t2": 4e9}), "")
    check("aggregate is numeric only",
          "__error__" in call("get_window", {"file": f105["file"], "column": "mpc_ctrl_mode",
                                             "mode": "aggregate", "every": 10, "t1": 0, "t2": 4e9}), "")
    check("min_duration does not apply to aggregate",
          "__error__" in call("get_window", {"file": f105["file"], "column": t_play,
                                             "mode": "aggregate", "every": 10, "min_duration": 1,
                                             "t1": 0, "t2": 4e9}), "")
    too_fine = call("get_window", {"file": f105["file"], "column": t_play, "mode": "aggregate",
                                   "every": 0.5, "t1": play_start, "t2": play_start + 360})
    check("too many buckets is an argument error, not a silent truncation",
          "__error__" in too_fine and "every" in too_fine["__error__"], too_fine)

    print("\n[W6'] find_text")
    halt = call("find_text", {"pattern": "Halt command received"})
    hits = {(os.path.basename(entry["file"]), entry["column"]): entry for entry in halt["series"]}
    check("the halt text is found in alarm_data of both controllers",
          ("log-20260101.log_mpc_ctrl_105.csv.zst", "alarm_data") in hits
          and ("log-htb-20260101.log_mpc_ctrl_101.csv.zst", "alarm_data") in hits, sorted(hits))
    alarm = hits.get(("log-20260101.log_mpc_ctrl_105.csv.zst", "alarm_data"), {})
    check("the matched value is the text that was asked for",
          any("Halt command received" in value for value in alarm.get("values", [])),
          str(alarm.get("values"))[:200])
    check("a pattern nothing contains finds nothing",
          call("find_text", {"pattern": "no-such-text-anywhere-42"})["matches_found"] == 0, "")

    # The message log holds no "Halt command received" (pandas says 0 rows, in any case), but
    # it does say "halt" 37 times -- so a case-insensitive search has to find exactly those.
    speech = call("find_text", {"pattern": "halt", "ignore_case": True,
                                "file": files["log-20260101.log_msg_105.csv.zst"]["file"],
                                "columns": ["msg"]})
    check("a case-insensitive search finds the 37 halt lines pandas counts",
          speech["matches_found"] == 37 and speech["series"][0]["column"] == "msg",
          speech["matches_found"])
    check("case folding is what makes them match",
          call("find_text", {"pattern": "Halt", "file": files["log-20260101.log_msg_105.csv.zst"]["file"],
                             "columns": ["msg"]})["matches_found"] == 0, "")
    print(f"    -> msg log: {speech['matches_found']} hits, first {str(speech['matches'][0])[:140]}")

    # The same question through a different code path: dense's value distribution counts the
    # samples holding each distinct value, and an anchored regex must agree with it.
    msg_file = files["log-20260101.log_msg_105.csv.zst"]
    dense = call("get_window", {"file": msg_file["file"], "column": "msg",
                                "t1": msg_file["start"] - 1, "t2": msg_file["end"] + 1})
    check("a whole-file window on msg comes back dense",
          dense["mode"] == "dense" and "values" in dense, sorted(dense))
    top_value, top_count = max(dense["values"].items(), key=lambda item: item[1])
    t0 = time.perf_counter()
    anchored = call("find_text", {"pattern": "^" + re.escape(top_value) + "$", "regex": True,
                                  "file": msg_file["file"], "columns": ["msg"]})
    print(f"    -> anchored search over {dense['samples']} msg samples: "
          f"{time.perf_counter() - t0:.2f} s")
    check("an anchored regex agrees with dense's own count of that value",
          anchored["matches_found"] == top_count,
          f"{anchored['matches_found']} vs {top_count} for {top_value[:60]!r}")
    check("find_text lists the hits when they fit under the limit",
          len(anchored["matches"]) == min(anchored["matches_found"], 50),
          f"{len(anchored['matches'])} vs {anchored['matches_found']}")

    strings = set(call("list_series", {"file": f105["file"]})["files"][0]["string_series"])
    named = call("find_text", {"pattern": "Halt", "file": f105["file"]})
    check("only string signals are searched unless told otherwise",
          named["series"] and all(entry["column"] in strings for entry in named["series"]),
          sorted({entry["column"] for entry in named["series"]} - strings))
    check("naming a column searches exactly that one",
          call("find_text", {"pattern": "Halt", "file": f105["file"],
                             "columns": ["alarm_data"]})["files"][0]["searched_columns"] == 1, "")
    check("ignore_case matches other spellings",
          call("find_text", {"pattern": "halt command", "ignore_case": True,
                             "file": f105["file"]})["matches_found"] > 0, "")
    check("a regex is a regex",
          call("find_text", {"pattern": r"Halt\s+command", "regex": True,
                             "file": f105["file"]})["matches_found"] > 0, "")
    limited = call("find_text", {"pattern": "halt", "ignore_case": True,
                                 "file": files["log-20260101.log_msg_105.csv.zst"]["file"],
                                 "columns": ["msg"], "limit": 5})
    check("limit truncates the hit list but not the count",
          len(limited["matches"]) == 5 and limited["truncated"] is True
          and limited["matches_found"] == 37, json.dumps(limited)[:200])
    check("a literal pattern may contain regex characters",
          "matches_found" in call("find_text", {"pattern": "Halt(", "regex": False}), "")
    check("a broken regex is a clear error",
          "__error__" in call("find_text", {"pattern": "Halt(", "regex": True}), "")
    check("nested quantifiers are refused up front",
          "__error__" in call("find_text", {"pattern": "(a+)+", "regex": True}), "")
    check("an over-long pattern is refused",
          "__error__" in call("find_text", {"pattern": "a" * 201}), "")
    check("a missing pattern is an error", "__error__" in call("find_text", {}), "")

    print("\n[M4] plot_series / unplot_series")
    analog, enum, text_col = "mpc_ctrl_t_play", "mpc_ctrl_mode", "msg"
    msg = files["log-20260101.log_msg_105.csv.zst"]

    plotted = call("plot_series", {"file": f105["file"], "column": analog})
    check("plot an analog series", plotted["columns"][0]["status"] == "plotted",
          json.dumps(plotted)[:200])
    check("list_series agrees it is plotted",
          analog in call("list_series", {"file": f105["file"]})["files"][0]["plotted"], "")

    plotted2 = call("plot_series", {"file": f105["file"], "column": enum})
    check("plot an enum series", plotted2["columns"][0]["status"] == "plotted",
          json.dumps(plotted2)[:200])

    tracked = call("plot_series", {"file": msg["file"], "column": text_col})
    check("plot a text series tracks it instead of plotting it",
          tracked["columns"][0]["status"] == "tracked", json.dumps(tracked)[:200])
    check("list_series agrees it is tracked",
          text_col in call("list_series", {"file": msg["file"]})["files"][0].get("text_tracked", []), "")

    again = call("plot_series", {"file": f105["file"], "column": analog})
    check("plotting again is a no-op with a clear status",
          again["columns"][0]["status"] == "already_plotted", json.dumps(again)[:200])
    again_t = call("plot_series", {"file": msg["file"], "column": text_col})
    check("tracking again is a no-op with a clear status",
          again_t["columns"][0]["status"] == "already_tracked", json.dumps(again_t)[:200])

    both = call("plot_series", {"file": f105["file"], "columns": [analog, enum]})
    check("several columns at once", len(both["columns"]) == 2, json.dumps(both)[:200])

    absent = call("plot_series", {"file": f105["file"], "column": "no-such-column"})
    check("an unknown column comes back absent, not an error",
          absent["columns"][0]["status"] == "absent" and "__error__" not in absent,
          json.dumps(absent)[:200])

    # No 'file' means every loaded file: the column exists in exactly the two mpc_ctrl logs.
    fleet = call("plot_series", {"column": analog})
    by_status = {}
    for entry in fleet["columns"]:
        by_status.setdefault(entry["status"], []).append(os.path.basename(entry["file"]))
    check("omitting file applies to every loaded file",
          sorted(by_status.get("plotted", []) + by_status.get("already_plotted", []))
          == ["log-20260101.log_mpc_ctrl_105.csv.zst",
              "log-htb-20260101.log_mpc_ctrl_101.csv.zst"],
          json.dumps(by_status))
    check("files without the column come back absent",
          len(by_status.get("absent", [])) == len(LOGS) - 2, json.dumps(by_status))

    un = call("unplot_series", {"column": analog})
    check("unplotting applies to every loaded file too",
          sum(1 for e in un["columns"] if e["status"] == "unplotted") == 2
          and all(e["status"] in ("unplotted", "absent") for e in un["columns"]),
          json.dumps(un)[:300])
    check("the curves are gone from both files",
          analog not in call("list_series", {"file": f105["file"]})["files"][0]["plotted"]
          and analog not in call("list_series", {"file": f101["file"]})["files"][0]["plotted"], "")
    un2 = call("unplot_series", {"file": f105["file"], "column": enum})
    check("unplot an enum series", un2["columns"][0]["status"] == "unplotted", json.dumps(un2)[:200])
    un3 = call("unplot_series", {"file": msg["file"], "column": text_col})
    check("unplotting a tracked series untracks it",
          un3["columns"][0]["status"] == "untracked", json.dumps(un3)[:200])
    un4 = call("unplot_series", {"file": msg["file"], "column": text_col})
    check("unplotting again is a no-op",
          un4["columns"][0]["status"] == "already_unplotted", json.dumps(un4)[:200])

    print("\n[M4] zoom_to / get_view_range")
    call("plot_series", {"file": f105["file"], "column": analog})   # keep a curve for the y range
    zt1, zt2 = f105["start"] + 100, f105["start"] + 700
    zoom = call("zoom_to", {"t1": zt1, "t2": zt2})
    check("zoom_to reports the x range it set", zoom["x"] == [zt1, zt2], json.dumps(zoom)[:200])
    check("zoom_to reports a y range while a curve is plotted",
          "y" in zoom and zoom["y"][0] <= zoom["y"][1], json.dumps(zoom)[:200])
    view = call("get_view_range", {})
    check("get_view_range reads back the zoomed x",
          close(view["main"]["x"][0], zt1, 1e-9) and close(view["main"]["x"][1], zt2, 1e-9),
          json.dumps(view)[:300])
    check("get_view_range carries both axes and the tz",
          all(isinstance(v, float) for v in view["main"]["x"] + view["main"]["y"]
              + view["right"]["x"] + view["right"]["y"]) and view["tz"], json.dumps(view)[:300])
    check("a backwards window is an error",
          "__error__" in call("zoom_to", {"t1": zt2, "t2": zt1}), "")

    print("\n[M4] highlight_range / measure_delta / screenshot_plot")
    h1 = call("highlight_range", {"ranges": [[zt1, zt2], [zt2 + 10, zt2 + 90]]})
    check("two bands are drawn and reported",
          h1["ranges"] == [[zt1, zt2], [zt2 + 10, zt2 + 90]], json.dumps(h1)[:200])
    h2 = call("highlight_range", {"ranges": [[zt1, zt1 + 30]]})
    check("a later call replaces the earlier bands", h2["ranges"] == [[zt1, zt1 + 30]],
          json.dumps(h2)[:200])
    check("an empty list is an error", "__error__" in call("highlight_range", {"ranges": []}), "")
    check("a malformed pair is an error",
          "__error__" in call("highlight_range", {"ranges": [[zt1]]}), "")
    check("a backwards band is an error",
          "__error__" in call("highlight_range", {"ranges": [[zt2, zt1]]}), "")
    check("more than 64 bands is refused",
          "__error__" in call("highlight_range", {"ranges": [[zt1, zt2]] * 65}), "")

    m1 = call("measure_delta", {"t1": zt1, "t2": zt2})
    check("measure_delta reports the exact delta", close(m1["delta_s"], zt2 - zt1, 1e-9),
          json.dumps(m1)[:200])
    check("measure_delta says it enabled the measurement mode",
          m1["measure_mode"] is True and "was_enabled" in m1, json.dumps(m1)[:200])

    shot = rpc("tools/call", {"name": "screenshot_plot", "arguments": {}})["result"]
    image = shot["content"][0]
    check("screenshot_plot returns image content",
          image.get("type") == "image" and image.get("mimeType") == "image/png",
          json.dumps(shot)[:200])
    png = base64.b64decode(image.get("data", ""))
    check("the screenshot is a PNG of a real size",
          png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 1000, f"{len(png)} bytes")

    print("\n[M4] set_timezone")
    orig = view["tz"]
    sign = 1 if orig.startswith("+") else -1
    orig_hours = sign * int(orig[1:3])
    tz0 = call("set_timezone", {"offset": 0})
    check("set_timezone to UTC", tz0["tz"] == "+00:00", json.dumps(tz0)[:200])
    probe = call("get_window", {"file": f105["file"], "column": enum,
                                "t1": f105["start"], "t2": f105["start"] + 60,
                                "time_format": "iso"})
    check("window replies pick up the new offset", probe.get("tz") == "+00:00",
          repr(probe.get("tz")))
    back = call("set_timezone", {"offset": orig_hours})
    check("the timezone is restored", back["tz"] == orig, json.dumps(back)[:200])
    check("an out-of-range offset is an error",
          "__error__" in call("set_timezone", {"offset": 13}), "")
    check("a boolean offset is an error",
          "__error__" in call("set_timezone", {"offset": True}), "")

    print("\n[M4] compute_series")
    c1 = call("compute_series", {"file": f105["file"], "column": analog, "mode": "abs"})
    check("abs creates and names the derived column", c1["column"] == analog + "_abs",
          json.dumps(c1)[:200])
    check("the derived column is drawn and listed",
          c1["column"] in call("list_series", {"file": f105["file"]})["files"][0]["plotted"], "")
    cw = call("get_window", {"file": f105["file"], "column": c1["column"],
                             "t1": zt1, "t2": zt2})
    check("the derived column is readable", cw.get("samples", 0) > 0, json.dumps(cw)[:200])
    c2 = call("compute_series", {"file": f105["file"], "column": analog, "mode": "1nd"})
    check("1nd creates the first derivative", c2["column"] == analog + "_1nd",
          json.dumps(c2)[:200])
    ref = call("compute_series", {"file": msg["file"], "column": text_col, "mode": "abs"})
    check("a string series is refused fast, without a modal",
          "__error__" in ref and "cannot calculate" in ref["__error__"], json.dumps(ref)[:200])
    check("a bad mode is an error",
          "__error__" in call("compute_series", {"file": f105["file"], "column": analog,
                                                 "mode": "mean"}), "")
    check("an unknown column is an error",
          "__error__" in call("compute_series", {"file": f105["file"], "column": "nope",
                                                 "mode": "abs"}), "")

    print("\n[M4] search_text")
    none = call("search_text", {"q": "halt"})
    check("with nothing checked the error names the fix",
          "__error__" in none and "no free-text series" in none["__error__"],
          json.dumps(none)[:200])

    call("plot_series", {"file": msg["file"], "column": text_col})
    s1 = call("search_text", {"q": "halt"})
    check("the GUI search finds the same 37 halt lines find_text counts",
          s1["matches_found"] == 37, json.dumps(s1)[:200])
    check("the hits are [t, file, column, value] in time order",
          len(s1["matches"]) == 37 and all(len(row) == 4 for row in s1["matches"])
          and [row[0] for row in s1["matches"]] == sorted(row[0] for row in s1["matches"]),
          json.dumps(s1["matches"][:2])[:200])
    s2 = call("search_text", {"q": "halt", "limit": 5})
    check("limit truncates the list but not the count",
          len(s2["matches"]) == 5 and s2["matches_found"] == 37 and s2["truncated"] is True,
          json.dumps(s2)[:200])
    s3 = call("search_text", {"q": "halt", "time_format": "iso"})
    check("iso times come back as strings",
          isinstance(s3["matches"][0][0], str) and "T" in s3["matches"][0][0],
          json.dumps(s3["matches"][0])[:200])
    bad = call("search_text", {"q": "halt("})
    check("a broken regex is a clear error",
          "__error__" in bad and "regular expression" in bad["__error__"], json.dumps(bad)[:200])
    un5 = call("unplot_series", {"file": msg["file"], "column": text_col})
    check("untracking brings the error back",
          un5["columns"][0]["status"] == "untracked"
          and "__error__" in call("search_text", {"q": "halt"}), json.dumps(un5)[:200])

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
