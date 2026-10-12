"""Contract test for the logplot MCP server: drives a *running* instance over HTTP.

Covers the load transparency (W10), the window housekeeping (W7), the backpressure
(W8), the analysis layer (W1/W3/W4/W5/W6'), the GUI control tools (M4 / W9) and the
cross-file fleet layer (M5) of TODO.md, over the case-C fleet: nine
log-20260928_mpc_ctrl_101..109 files (206 signals each, identical schema).

The per-file numbers (rows, columns, constant columns, the mpc_ctrl_mode run
counts, the abort times) were measured independently with pandas -- see
_tmp/measure_fleet.py and _tmp/fleet_ground_truth.json -- so the assertions below
compare the server against data that never passed through logplot.py.

Start the server first, then run this:

    logplot.bat --mcp-port 8791 --mcp-token smoke-token
    uv run mcp_test.py 8791 smoke-token C:/Users/viking/test-log/log-20260928_mpc_ctrl_*.csv.zst

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

# Independently measured (see _tmp/measure_fleet.py):
# name -> (columns, rows, sampling_median_s, largest_gap_s, constant_count,
#          mpc_ctrl_mode whole-file segment count)
EXPECTED = {
    "log-20260928_mpc_ctrl_101.csv.zst": (206, 48776, 0.1, 1, 15, 100),
    "log-20260928_mpc_ctrl_102.csv.zst": (206, 50030, 0.1, 1, 15, 89),
    "log-20260928_mpc_ctrl_103.csv.zst": (206, 43859, 0.1, 1, 15, 92),
    "log-20260928_mpc_ctrl_104.csv.zst": (206, 49786, 0.1, 1, 15, 82),
    "log-20260928_mpc_ctrl_105.csv.zst": (206, 49789, 0.1, 1, 15, 82),
    "log-20260928_mpc_ctrl_106.csv.zst": (206, 48484, 0.1, 1, 15, 88),
    "log-20260928_mpc_ctrl_107.csv.zst": (206, 44670, 0.1, 1, 15, 78),
    "log-20260928_mpc_ctrl_108.csv.zst": (206, 51339, 0.1, 1, 15, 81),
    "log-20260928_mpc_ctrl_109.csv.zst": (206, 49701, 0.1, 1, 15, 81),
}
# The seven abort events of the case-C narrative: (halt start, n_files).
EVENT_HALTS = [
    (1790607684.473, 3),
    (1790608147.132, 3),
    (1790608322.411, 3),
    (1790608652.732, 3),
    (1790608972.417, 9),     # the all-fleet abort, 23:22:52 +08:00
    (1790612886.240, 7),
    (1790613441.396, 8),
]
EVENT_E_PLAYING = 1790608949.224


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
    other = write_fixture(
        "fleet_other.csv",
        "timestamp,alpha,beta\n2026-09-28 12:00:00.000,1,x\n2026-09-28 12:00:01.000,2,y\n"
        "2026-09-28 12:00:02.000,2,x\n2026-09-28 12:00:03.000,3,y\n")

    f101 = {os.path.basename(p): p for p in LOGS}["log-20260928_mpc_ctrl_101.csv.zst"]

    print("\n[W10] load_files / list_files")
    t0 = time.perf_counter()
    first = call("load_files", {"paths": [f101]})
    print(f"    -> load_files first file: {time.perf_counter() - t0:.2f} s")
    check("load_files loaded the first file", len(first.get("loaded", [])) == 1
          and not first.get("failed"), json.dumps(first.get("failed"))[:400])

    # With exactly one file loaded, omitting 'file' answers in the single-file shape.
    single = call("get_window", {"column": "mpc_ctrl_mode",
                                 "t1": 1790593300, "t2": 1790593330})
    check("one file loaded: no 'file' gives the single-file shape",
          "file" in single and "files" not in single and single.get("column") == "mpc_ctrl_mode",
          json.dumps(single)[:200])

    rest = [p for p in LOGS if p != f101]
    loaded = call("load_files", {"paths": [f101] + rest})
    check("load_files loaded all", len(loaded.get("loaded", [])) == len(LOGS),
          json.dumps(loaded.get("failed"))[:400])
    check("load_files nothing failed", not loaded.get("failed"), json.dumps(loaded.get("failed"))[:400])
    check("re-loading the first file is a no-op",
          all(e.get("already_loaded") for e in loaded["loaded"]
              if e["file"] == f101), json.dumps(loaded["loaded"])[:200])

    t0 = time.perf_counter()
    listing = call("list_files", {})
    print(f"    -> list_files, first scan of every file: {time.perf_counter() - t0:.2f} s")
    files = {f["name"]: f for f in listing["files"]}
    check("list_files counts", len(files) == len(LOGS), f"{sorted(files)}")

    for name, want in EXPECTED.items():
        got = files.get(name)
        if got is None:
            check(f"{name} present", False, "not loaded")
            continue
        cols, rows, sampling, gap, constant, _segs = want
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
        check(f"{name} timestamp_parse", isinstance(got["timestamp_parse"], str)
              and bool(got["timestamp_parse"]), got["timestamp_parse"])
        check(f"{name} timestamp_column", isinstance(got.get("timestamp_column"), str)
              and bool(got["timestamp_column"]), repr(got.get("timestamp_column")))
        check(f"{name} not ambiguous", "timestamp_ambiguous" not in got,
              f"flagged: {got.get('timestamp_ambiguous')}")
        print(f"    -> {name[-16:-9]}: parse={got['timestamp_parse']} "
              f"sampling={got['sampling_median_s']} gap={got['largest_gap_s']} "
              f"constant={got['constant_count']}")

    print("\n[W10] failure reasons carry the real error")
    bad = call("load_files", {"paths": ["C:/nope/missing.csv", ts_only, ts_only_named]})
    reasons = {f["file"]: f["reason"] for f in bad.get("failed", [])}
    check("missing file reason", "FileNotFoundError" in reasons.get("C:/nope/missing.csv", ""),
          reasons.get("C:/nope/missing.csv"))
    check("timestamp-only reason (already named timestamp)",
          "no data columns" in reasons.get(ts_only, ""), reasons.get(ts_only))
    check("timestamp-only reason (renamed to ts)",
          "no data columns" in reasons.get(ts_only_named, ""), reasons.get(ts_only_named))

    print("\n[W10/W11] whole-file unchanged count agrees with the constant scan")
    for name, f in files.items():
        fc = call("find_changes", {"file": f["file"], "t1": f["start"] - 1, "t2": f["end"] + 1})
        check(f"{name} unchanged_series == constant_count",
              fc["unchanged_series"] == f["constant_count"],
              f"find_changes {fc['unchanged_series']} vs scan {f['constant_count']}")

    print("\n[W7] unchanged_columns is opt-in")
    target = files["log-20260928_mpc_ctrl_101.csv.zst"]
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
    t1, t2 = target["end"] - 5, target["end"] + 1800
    w = call("get_window", {"file": target["file"], "column": "mpc_ctrl_mode", "t1": t1, "t2": t2,
                            "mode": "transitions"})
    check("held_truncated flagged", w.get("held_truncated") is True, json.dumps(w)[:300])
    check("sample_end is the last sample", close(w["sample_end"], target["end"]), w["sample_end"])
    last = w["transitions"][-1]
    check("last held_seconds stops at the data end", close(last[3], target["end"] - last[0], 1e-3),
          f"held={last[3]}, data end - start = {target['end'] - last[0]}")
    check("last held_seconds not inflated towards t2", last[3] < 1800, f"held={last[3]}")

    w2 = call("get_window", {"file": target["file"], "column": "mpc_ctrl_mode",
                             "t1": target["end"] - 1800, "t2": target["end"], "mode": "transitions"})
    check("held_truncated absent inside the data", "held_truncated" not in w2,
          json.dumps(w2)[:300])
    if w2.get("transitions"):
        last2 = w2["transitions"][-1]
        check("last held_seconds reaches the window end", close(last2[3], target["end"] - last2[0], 1e-3),
              f"held={last2[3]}, want {target['end'] - last2[0]}")

    print("\n[W7] window_too_small")
    empty = call("get_window", {"file": target["file"], "column": "mpc_ctrl_mode",
                                "t1": target["end"] - 0.05, "t2": target["end"] - 0.02})
    check("no samples -> window_too_small",
          empty.get("window_too_small") is True and empty["samples"] == 0, json.dumps(empty)[:300])

    # 0.1 s sampling: [end - 0.05, end] holds exactly one sample, the last one.
    single_s = call("get_window", {"file": target["file"], "column": "mpc_ctrl_mode",
                                   "t1": target["end"] - 0.05, "t2": target["end"]})
    check("one sample -> window_too_small",
          single_s.get("window_too_small") is True and single_s["samples"] == 1,
          json.dumps(single_s)[:300])
    check("one sample still reports the value",
          len(single_s["segments"]) == 1, json.dumps(single_s)[:300])

    explicit = call("get_window", {"file": target["file"], "column": "mpc_ctrl_mode",
                                   "t1": target["end"], "t2": target["end"], "mode": "segments"})
    check("explicit segments still flags a one-instant window",
          explicit.get("window_too_small") is True, json.dumps(explicit)[:300])

    roomy = call("get_window", {"file": target["file"], "column": "mpc_ctrl_mode",
                                "t1": target["end"] - 60, "t2": target["end"]})
    check("a real window is not flagged", "window_too_small" not in roomy, json.dumps(roomy)[:300])

    print("\n[W7] min_duration still applies to transitions")
    deb = call("get_window", {"file": target["file"], "column": "mpc_ctrl_mode", "t1": 0, "t2": 4e9,
                              "mode": "transitions", "min_duration": 60})
    check("min_duration trims transitions", deb.get("segments_before", 0) > len(deb["transitions"]),
          f"before={deb.get('segments_before')} after={len(deb['transitions'])}")

    f107 = files["log-20260928_mpc_ctrl_107.csv.zst"]

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
        return {key: (len(rows), min(r[2] for r in rows),
                      statistics.median([r[2] for r in rows]), max(r[2] for r in rows))
                for key, rows in groups.items()}

    print("\n[W4] mode=summary group_by=transition")
    for label, f in (("101", target), ("107", f107)):
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
            count, lo, mid, hi = local[key]
            if (group["count"] != count or not close(group["held_min_s"], lo, 1e-6)
                    or not close(group["held_median_s"], mid, 1e-6)
                    or not close(group["held_max_s"], hi, 1e-6)):
                mismatched.append((key, group, count, lo, mid, hi))
        check(f"{label} every group matches the exact transitions", not mismatched,
              json.dumps(mismatched)[:400])
        check(f"{label} groups are not truncated", "groups_truncated" not in summary, "")

        halts = [g for g in summary["groups"] if g["from"] == "playing" and g["to"] == "halt"]
        check(f"{label} playing->halt count is 4", len(halts) == 1 and halts[0]["count"] == 4,
              json.dumps(halts)[:200])
        print(f"    -> {label}: {summary['group_count']} groups, "
              f"playing->halt {halts[0]['count'] if halts else '?'}")

    s101 = whole_window(target, mode="summary", group_by="transition")
    check("101 has 20 transition groups", s101["group_count"] == 20, s101["group_count"])

    print("\n[W4] mode=summary group_by=value")
    rows = whole_window(f107, column="alarm_data", mode="transitions")["transitions"]
    values = whole_window(f107, column="alarm_data", mode="summary", group_by="value")
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
    check("value seconds cover the data span",
          close(sum(g["seconds"] for g in values["groups"]),
                f107["end"] - f107["start"], 0.01),
          sum(g["seconds"] for g in values["groups"]))
    check("value counts cover the runs",
          sum(g["count"] for g in values["groups"]) == values["segments"], values["segments"])
    check("value groups are ranked by time held",
          all(values["groups"][i]["seconds"] >= values["groups"][i + 1]["seconds"]
              for i in range(len(values["groups"]) - 1)), "")
    check("the blank time is its own group and the largest",
          values["groups"][0]["value"] is None,
          json.dumps(values["groups"][0])[:200])
    print(f"    -> 107 alarm_data: {values['group_count']} value groups, "
          f"top {[(g['value'], g['count']) for g in values['groups'][:3]]}")

    v101 = whole_window(target, column="alarm_data", mode="summary", group_by="value")
    halt_group = [g for g in v101["groups"]
                  if str(g["value"]) == "(i:1 o:2 s:1.2 m:Halt command received.)"]
    check("101 halt-command group: 13 runs, 952.478 s",
          len(halt_group) == 1 and halt_group[0]["count"] == 13
          and close(halt_group[0]["seconds"], 952.478, 0.01),
          json.dumps(halt_group)[:200])

    print("\n[W4] summary argument validation")
    check("summary needs group_by",
          "__error__" in whole_window(f107, mode="summary"), "no error")
    check("group_by needs summary",
          "__error__" in whole_window(f107, mode="segments", group_by="value"), "no error")
    check("unknown group_by",
          "__error__" in whole_window(f107, mode="summary", group_by="pair"), "no error")

    print("\n[W1] list_series detail")
    for f in (target, f107):
        name = os.path.basename(f["file"])
        t0 = time.perf_counter()
        call("list_series", {"file": f["file"]})              # warms the distinct/kind caches
        warm = time.perf_counter() - t0
        t0 = time.perf_counter()
        listed = call("list_series", {"file": f["file"], "detail": True})["files"][0]
        first = time.perf_counter() - t0
        t0 = time.perf_counter()
        call("list_series", {"file": f["file"], "detail": True})
        again = time.perf_counter() - t0
        print(f"    -> {name[-16:-9]}: list_series {warm * 1000:.0f} ms, "
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
              all(n not in fact.get("same_timing_as", []) for n, fact in facts.items()), "")

    facts101 = call("list_series", {"file": target["file"], "detail": True})["files"][0]["facts"]
    check("state and mode share their timing",
          "mpc_ctrl_mode" in facts101["mpc_ctrl_state"].get("same_timing_as", []),
          facts101["mpc_ctrl_state"])
    check("mode and state share their timing too",
          "mpc_ctrl_state" in facts101["mpc_ctrl_mode"].get("same_timing_as", []),
          facts101["mpc_ctrl_mode"])
    pairs = facts101["mpc_ctrl_mode"].get("value_pairs", {})
    check("mode carries the one-to-one correspondence with state",
          pairs.get("with") == "mpc_ctrl_state"
          and dict(pairs.get("pairs", [])) == {"calibrating": 9, "halt": 10, "idle": 4,
                                               "initializing": 0, "maintenance": 1, "parking": 8,
                                               "playing": 6, "playing_done": 7, "ready": 5,
                                               "shutdown": 14},
          json.dumps(pairs)[:300])
    # Verified elementwise with pandas (see _tmp/fleet_ground_truth.json): in this file
    # mpc_ctrl_t_elapsed holds the very same values as mpc_ctrl_t_play, so it is a duplicate.
    check("t_elapsed is a duplicate of t_play",
          facts101["mpc_ctrl_t_elapsed"].get("duplicate_of") == "mpc_ctrl_t_play",
          facts101["mpc_ctrl_t_elapsed"])
    check("a duplicate is not also listed as merely synchronised",
          facts101["mpc_ctrl_t_elapsed"].get("duplicate_of")
          not in facts101["mpc_ctrl_t_elapsed"].get("same_timing_as", []), "")
    check("t_play itself has no duplicate claim",
          "duplicate_of" not in facts101["mpc_ctrl_t_play"], facts101["mpc_ctrl_t_play"])

    # A duplicate claim has to hold in the segments reply too, which is computed by an
    # entirely different path from the change signature that produced the claim.
    play_start = 1790600099.631        # first ready->playing of the file, measured
    segment_window = {"file": target["file"], "t1": play_start, "t2": play_start + 5}
    plays_seg = call("get_window", dict(segment_window, column="mpc_ctrl_t_play", max_points=5000))
    elapsed_seg = call("get_window", dict(segment_window, column="mpc_ctrl_t_elapsed",
                                          max_points=5000))
    check("the duplicate claim holds in a real window",
          plays_seg["mode"] == "segments" and len(plays_seg["segments"]) > 10
          and plays_seg["segments"] == elapsed_seg["segments"],
          f"{plays_seg['mode']}, {len(plays_seg.get('segments', []))} runs")

    print("\n[W3] snapshot and multi-column reads")
    # Event A of the case-C narrative: 107 goes playing->halt at 23:01:24 +08:00.
    instant = 1790607684.5             # the halt sample of 107 is at 1790607684.473
    fingerprint = ["mpc_ctrl_mode", "mpc_ctrl_state", "mpc_ctrl_t_play", "mpc_ctrl_run_1",
                   "mpc_ctrl_st_1", "mpc_ctrl_en_1", "alarm_data"]
    snap = call("get_window", {"file": f107["file"], "columns": fingerprint,
                               "t1": instant, "t2": instant})
    entries = {entry["column"]: entry for entry in snap["columns"]}
    check("one call reads all seven", len(entries) == len(fingerprint), list(entries))
    check("t1 == t2 gives a snapshot",
          all(entry["mode"] == "snapshot" for entry in entries.values()),
          [entry["mode"] for entry in entries.values()])
    check("every column reports the same sample",
          len({entry["sample_time"] for entry in entries.values()}) == 1,
          {e["column"]: e["sample_time"] for e in entries.values()})
    check("mode is halt and state is 10 at the abort",
          entries["mpc_ctrl_mode"]["value"] == "halt" and entries["mpc_ctrl_state"]["value"] == 10,
          {name: entries[name]["value"] for name in ("mpc_ctrl_mode", "mpc_ctrl_state")})
    check("the fingerprint matches the measured sample",
          entries["mpc_ctrl_run_1"]["value"] == 0
          and entries["mpc_ctrl_st_1"]["value"] == "deactuating"
          and entries["mpc_ctrl_en_1"]["value"] == 1
          and entries["mpc_ctrl_t_play"]["value"] == 3589,
          json.dumps({n: entries[n]["value"]
                      for n in ("mpc_ctrl_run_1", "mpc_ctrl_st_1",
                                "mpc_ctrl_en_1", "mpc_ctrl_t_play")})[:200])
    check("alarm_data carries the HMI STOP",
          "HMI STOP pressed" in str(entries["alarm_data"]["value"]),
          str(entries["alarm_data"]["value"])[:160])
    print(f"    -> {json.dumps({name: entry['value'] for name, entry in entries.items()})[:400]}")

    check("an explicit snapshot mode works away from t1 == t2",
          call("get_window", {"file": f107["file"], "column": "mpc_ctrl_mode", "mode": "snapshot",
                              "t1": instant - 600, "t2": instant})["value"] == "halt", "")
    check("a snapshot before the first sample says so",
          call("get_window", {"file": f107["file"], "column": "mpc_ctrl_mode",
                              "t1": f107["start"] - 3600, "t2": f107["start"] - 3600}
               ).get("no_sample") is True, "")
    check("giving both column and columns is an error",
          "__error__" in call("get_window", {"file": f107["file"], "column": "mpc_ctrl_mode",
                                             "columns": ["mpc_ctrl_state"],
                                             "t1": instant, "t2": instant}), "")
    check("an unknown name in columns is an error",
          "__error__" in call("get_window", {"file": f107["file"], "columns": ["mpc_ctrl_mode", "nope"],
                                             "t1": instant, "t2": instant}), "")
    multi = call("get_window", {"file": f107["file"],
                                "columns": ["mpc_ctrl_mode", "mpc_ctrl_state"],
                                "t1": f107["start"] - 1, "t2": f107["end"] + 1,
                                "mode": "summary", "group_by": "value"})
    check("columns works for other modes too",
          len(multi["columns"]) == 2
          and all(entry["group_count"] == 10 for entry in multi["columns"]),
          [entry.get("group_count") for entry in multi["columns"]])

    print("\n[W5] mode=aggregate")
    # Event E (all-fleet abort): 107's t_play climbs from zero to 18237 ms during the play
    # and is held at that value after the halt at 1790608972.417.
    aggregate = call("get_window", {"file": f107["file"], "column": "mpc_ctrl_t_play",
                                    "mode": "aggregate", "every": 5,
                                    "t1": 1790608950, "t2": 1790608985})
    buckets = aggregate["buckets"]
    maxima = [row["max"] for row in buckets]
    check("buckets span the window in one step",
          7 <= aggregate["bucket_count"] <= 8, aggregate["bucket_count"])
    check("bucket counts add up to the window's samples",
          sum(row["count"] for row in buckets) == aggregate["samples"],
          f"{sum(row['count'] for row in buckets)} vs {aggregate['samples']}")
    check("buckets start on multiples of every since the epoch",
          all(row["t"] % 5 == 0 for row in buckets), [row["t"] for row in buckets[:4]])
    check("every aggregate brackets the first and last sample of its bucket",
          all(row["min"] <= row[key] <= row["max"]
              for row in buckets for key in ("first", "last") if key in row),
          json.dumps([row for row in buckets
                      if not (row["min"] <= row["last"] <= row["max"])])[:300])
    check("the climb starts from zero", buckets[0]["min"] == 0, buckets[0])
    check("the counter climbs without ever falling back",
          all(maxima[i] <= maxima[i + 1] for i in range(len(maxima) - 1)),
          [(maxima[i], maxima[i + 1]) for i in range(len(maxima) - 1) if maxima[i] > maxima[i + 1]][:3])
    check("it reaches the measured 18237 ms at the halt",
          maxima[-1] == 18237, maxima)
    check("the halt holds the value, it does not zero it here",
          buckets[-1]["min"] == buckets[-1]["max"] == 18237, buckets[-1])
    print(f"    -> {aggregate['bucket_count']} buckets of {aggregate['every']}s: "
          f"first {buckets[0]}, last {buckets[-1]}")

    check("agg picks which aggregates come back",
          set(call("get_window", {"file": f107["file"], "column": "mpc_ctrl_t_play", "mode": "aggregate",
                                  "every": 30, "agg": ["last"],
                                  "t1": 1790608950, "t2": 1790609250})["buckets"][0])
          == {"t", "count", "last"}, "")
    check("aggregate needs every",
          "__error__" in call("get_window", {"file": f107["file"], "column": "mpc_ctrl_t_play",
                                             "mode": "aggregate", "t1": 0, "t2": 4e9}), "")
    check("every only applies to aggregate",
          "__error__" in call("get_window", {"file": f107["file"], "column": "mpc_ctrl_t_play",
                                             "every": 10, "t1": 0, "t2": 4e9}), "")
    check("agg is validated",
          "__error__" in call("get_window", {"file": f107["file"], "column": "mpc_ctrl_t_play",
                                             "mode": "aggregate", "every": 10, "agg": ["mean"],
                                             "t1": 0, "t2": 4e9}), "")
    check("aggregate is numeric only",
          "__error__" in call("get_window", {"file": f107["file"], "column": "mpc_ctrl_mode",
                                             "mode": "aggregate", "every": 10, "t1": 0, "t2": 4e9}), "")
    check("min_duration does not apply to aggregate",
          "__error__" in call("get_window", {"file": f107["file"], "column": "mpc_ctrl_t_play",
                                             "mode": "aggregate", "every": 10, "min_duration": 1,
                                             "t1": 0, "t2": 4e9}), "")
    too_fine = call("get_window", {"file": f107["file"], "column": "mpc_ctrl_t_play", "mode": "aggregate",
                                   "every": 0.05, "t1": f107["start"] - 1, "t2": f107["end"] + 1})
    check("too many buckets is an argument error, not a silent truncation",
          "__error__" in too_fine and "every" in too_fine["__error__"], json.dumps(too_fine)[:200])

    print("\n[W6'] find_text")
    halt = call("find_text", {"pattern": "Halt command received"})
    hits = {os.path.basename(entry["file"]): entry for entry in halt["series"]}
    check("the halt text is found in alarm_data of every node",
          set(hits) == {os.path.basename(p) for p in LOGS}
          and all(entry["column"] == "alarm_data" for entry in hits.values()), sorted(hits))
    measured = {
        "log-20260928_mpc_ctrl_101.csv.zst": 4357,
        "log-20260928_mpc_ctrl_102.csv.zst": 4340,
        "log-20260928_mpc_ctrl_103.csv.zst": 4331,
        "log-20260928_mpc_ctrl_104.csv.zst": 82,
        "log-20260928_mpc_ctrl_105.csv.zst": 83,
        "log-20260928_mpc_ctrl_106.csv.zst": 73,
        "log-20260928_mpc_ctrl_107.csv.zst": 2764,
        "log-20260928_mpc_ctrl_108.csv.zst": 2709,
        "log-20260928_mpc_ctrl_109.csv.zst": 2705,
    }
    check("per-file hit counts agree with the pandas census",
          all(hits[name]["count"] == n for name, n in measured.items()),
          {name: hits[name]["count"] for name, n in measured.items() if hits[name]["count"] != n})
    check("matches_found is the sum over files",
          halt["matches_found"] == sum(measured.values()),
          f"{halt['matches_found']} vs {sum(measured.values())}")
    check("a pattern nothing contains finds nothing",
          call("find_text", {"pattern": "no-such-text-anywhere-42"})["matches_found"] == 0, "")
    check("case sensitivity matters",
          call("find_text", {"pattern": "halt command received",
                             "file": f101, "columns": ["alarm_data"]})["matches_found"] == 0, "")
    check("ignore_case brings the hits back",
          call("find_text", {"pattern": "halt command received", "ignore_case": True,
                             "file": f101, "columns": ["alarm_data"]})["matches_found"] == 4357, "")

    # The same question through a different code path: dense's value distribution counts the
    # samples holding each distinct value, and an anchored regex must agree with it.
    dense = call("get_window", {"file": f107["file"], "column": "mpc_ctrl_t_play",
                                "t1": f107["start"] - 1, "t2": f107["end"] + 1})
    check("a whole-file window on a counter comes back dense",
          dense["mode"] == "dense" and dense["distinct"] == 23279
          and set(dense["stats"]) == {"min", "max", "mean", "std", "first", "last", "nan"}
          and dense["stats"]["min"] == 0 and dense["stats"]["nan"] == 0,
          json.dumps(dense.get("stats")))
    text_dense = call("get_window", {"file": f107["file"], "column": "alarm_data",
                                     "t1": f107["start"] - 1, "t2": f107["end"] + 1,
                                     "max_points": 10})
    check("a whole-file window on a text series comes back dense too",
          text_dense["mode"] == "dense" and text_dense["distinct"] == 18
          and "values" in text_dense, json.dumps(text_dense)[:200])
    counts = {k: v for k, v in text_dense["values"].items() if k not in ("nan", "None", "")}
    top_value, top_count = max(counts.items(), key=lambda item: item[1])
    t0 = time.perf_counter()
    anchored = call("find_text", {"pattern": "^" + re.escape(top_value) + "$", "regex": True,
                                  "file": f107["file"], "columns": ["alarm_data"]})
    print(f"    -> anchored search over {text_dense['samples']} samples: "
          f"{time.perf_counter() - t0:.2f} s")
    check("an anchored regex agrees with dense's own count of that value",
          anchored["matches_found"] == top_count,
          f"{anchored['matches_found']} vs {top_count} for {top_value[:60]!r}")
    check("find_text lists the hits when they fit under the limit",
          len(anchored["matches"]) == min(anchored["matches_found"], 50),
          f"{len(anchored['matches'])} vs {anchored['matches_found']}")

    strings = set(call("list_series", {"file": f101})["files"][0]["string_series"])
    named = call("find_text", {"pattern": "Halt", "file": f101})
    check("only string signals are searched unless told otherwise",
          named["series"] and all(entry["column"] in strings for entry in named["series"]),
          sorted({entry["column"] for entry in named["series"]} - strings))
    check("naming a column searches exactly that one",
          call("find_text", {"pattern": "Halt", "file": f101,
                             "columns": ["alarm_data"]})["files"][0]["searched_columns"] == 1, "")
    check("a regex is a regex",
          call("find_text", {"pattern": r"Halt\s+command", "regex": True,
                             "file": f101})["matches_found"] > 0, "")
    limited = call("find_text", {"pattern": "Halt command received",
                                 "file": f101, "columns": ["alarm_data"], "limit": 5})
    check("limit truncates the hit list but not the count",
          len(limited["matches"]) == 5 and limited["truncated"] is True
          and limited["matches_found"] == 4357, json.dumps(limited)[:200])
    check("a literal pattern may contain regex characters",
          "matches_found" in call("find_text", {"pattern": "Halt(", "regex": False}), "")
    check("a broken regex is a clear error",
          "__error__" in call("find_text", {"pattern": "Halt(", "regex": True}), "")
    check("nested quantifiers are refused up front",
          "__error__" in call("find_text", {"pattern": "(a+)+", "regex": True}), "")
    check("an over-long pattern is refused",
          "__error__" in call("find_text", {"pattern": "a" * 201}), "")
    check("a missing pattern is an error", "__error__" in call("find_text", {}), "")

    print("\n[M5] get_window over several files")
    t1 = min(files[name]["start"] for name in EXPECTED) - 1
    t2 = max(files[name]["end"] for name in EXPECTED) + 1
    allfiles = call("get_window", {"column": "mpc_ctrl_mode", "t1": t1, "t2": t2})
    check("omitting file with several loaded gives a per-file envelope",
          "files" in allfiles and "file" not in allfiles
          and allfiles["t1"] == t1 and allfiles["t2"] == t2,
          json.dumps(list(allfiles))[:200])
    check("the envelope carries the window and the tz",
          allfiles["t1"] == t1 and allfiles["t2"] == t2 and isinstance(allfiles["tz"], str),
          json.dumps({k: allfiles[k] for k in ("t1", "t2", "tz")})[:200])
    per_file = {e["file"]: e for e in allfiles["files"]}
    check("one entry per loaded file", len(per_file) == len(LOGS), len(per_file))
    seg_counts = {os.path.basename(e["file"]): len(e.get("segments", []))
                  for e in allfiles["files"]}
    check("every entry is a segments reply with the measured run count",
          all(e.get("mode") == "segments" for e in allfiles["files"])
          and all(seg_counts[n] == EXPECTED[n][5] for n in EXPECTED),
          json.dumps(seg_counts))
    one = call("get_window", {"file": f101, "column": "mpc_ctrl_mode", "t1": t1, "t2": t2})
    check("a per-file entry is the single-file reply, bit for bit",
          per_file[f101] == one, f"{json.dumps(per_file[f101])[:120]} vs {json.dumps(one)[:120]}")
    check("each entry keeps its own window fields",
          all(e["t1"] == t1 and e["t2"] == t2 and e["tz"] for e in allfiles["files"]), "")

    many = call("get_window", {"columns": ["mpc_ctrl_mode", "mpc_ctrl_state"], "t1": t1, "t2": t2})
    check("columns work over several files",
          all(len(e.get("columns", [])) == 2 for e in many["files"]),
          json.dumps([sorted(e) for e in many["files"][:2]])[:200])

    snap_all = call("get_window", {"columns": ["mpc_ctrl_mode", "mpc_ctrl_t_play"],
                                   "t1": instant, "t2": instant})
    check("t1 == t2 over several files is a per-file snapshot",
          all(e["columns"][0]["mode"] == "snapshot" for e in snap_all["files"]),
          json.dumps([e["columns"][0]["mode"] for e in snap_all["files"]])[:200])

    print("\n[M5] list_series over several files")
    ls = call("list_series", {})
    check("identical schemas are listed once, the rest named same_as",
          len(ls["files"]) == 1 and ls.get("same_as")
          and sorted(ls["same_as"]) == sorted(p for p in LOGS if p != f101),
          json.dumps({k: ls[k] for k in ls if k != "files"})[:200])
    entry = ls["files"][0]
    check("the kept list covers the whole schema",
          len(entry["numeric_series"]) + len(entry["string_series"]) == 206,
          f"{len(entry['numeric_series'])} + {len(entry['string_series'])}")
    ls_one = call("list_series", {"file": os.path.basename(f101)})
    check("one named file lists itself in full",
          len(ls_one["files"]) == 1 and "same_as" not in ls_one
          and len(ls_one["files"][0]["numeric_series"]) + len(ls_one["files"][0]["string_series"]) == 206,
          json.dumps(list(ls_one))[:200])
    check("the deduplicated list agrees with the single-file list",
          ls["files"][0]["numeric_series"] == ls_one["files"][0]["numeric_series"]
          and ls["files"][0]["string_series"] == ls_one["files"][0]["string_series"], "")

    print("\n[M5] enum values in list_series")
    ls103 = call("list_series", {"file": os.path.basename(
        [p for p in LOGS if p.endswith("103.csv.zst")][0])})
    values103 = ls103["files"][0].get("values", {})
    check("103's mpc_ctrl_mode lists GWD before it ever reaches a segment reply",
          "GWD" in values103.get("mpc_ctrl_mode", []),
          json.dumps(values103.get("mpc_ctrl_mode"))[:200])
    values101 = call("list_series", {"file": os.path.basename(f101)})["files"][0].get("values", {})
    check("101's mpc_ctrl_mode has the ten known values and no GWD",
          "GWD" not in values101.get("mpc_ctrl_mode", [])
          and len(values101.get("mpc_ctrl_mode", [])) == 10,
          json.dumps(values101.get("mpc_ctrl_mode"))[:200])
    check("alarm_data values are the measured eleven",
          len(values101.get("alarm_data", [])) == 11
          and any(v.startswith("(i:1 o:2 s:1.2 m:Halt command received.)")
                  for v in values101.get("alarm_data", [])),
          json.dumps(values101.get("alarm_data"))[:200])
    check("a numeric enum lists its values", values101.get("mpc_ctrl_en_1") == [0.0, 1.0],
          json.dumps(values101.get("mpc_ctrl_en_1")))
    check("a text series gets no value list", "alarm_data" not in
          call("list_series", {"file": os.path.basename(f107["file"])})["files"][0]
          .get("values", {}), "")

    print("\n[M5] kind conflicts across files")
    conflicts = ls.get("kind_conflicts", {})
    check("the seven conflicting names are reported",
          sorted(conflicts) == ["alarm_data", "servo_hour_1", "servo_hour_2", "servo_hour_3",
                                "servo_hour_4", "servo_hour_5", "servo_hour_6"],
          sorted(conflicts))
    alarm_conflict = conflicts.get("alarm_data", {})
    check("alarm_data: 107 is text, the other eight are enum",
          len(alarm_conflict.get("text", [])) == 1
          and os.path.basename(alarm_conflict["text"][0]).endswith("107.csv.zst")
          and len(alarm_conflict.get("enum", [])) == 8,
          json.dumps({k: [os.path.basename(x) for x in v] for k, v in alarm_conflict.items()})[:200])
    check("a non-conflicting name is not reported", "mpc_ctrl_mode" not in conflicts, "")

    print("\n[M5] fleet clustering")
    # A file with a different schema must not break the multi-file reply: its entry carries
    # an error, the others answer as usual.
    call("load_files", {"paths": [other]})
    mixed = call("get_window", {"column": "alpha", "t1": 0, "t2": 4e9})
    by_name = {os.path.basename(e["file"]): e for e in mixed["files"]}
    check("the odd file answers, the nine fleet files report their own error",
          "segments" in by_name["fleet_other.csv"]
          and all("__error__" in e or "error" in e for e in (by_name[n] for n in EXPECTED)),
          json.dumps({n: list(e) for n, e in list(by_name.items())[:2]})[:200])
    check("the per-file error names the missing column",
          all("alpha" in e.get("error", "") for e in (by_name[n] for n in EXPECTED)),
          json.dumps([by_name[n].get("error") for n in list(EXPECTED)[:2]])[:200])
    check("a column missing from every file says so per file",
          all("error" in e for e in
              call("get_window", {"columns": ["alpha", "mpc_ctrl_mode"], "t1": 0, "t2": 4e9}
                   )["files"]), "")

    event = call("get_window", {"column": "mpc_ctrl_mode", "fleet": True,
                                "t1": 1790608950, "t2": 1790608973})
    fleet = event.get("fleet", [])
    by_value = {row["value"]: row for row in fleet}
    check("the event window clusters to playing and halt, nine files each",
          set(by_value) == {"playing", "halt"}
          and by_value["playing"]["n_files"] == 9
          and by_value["halt"]["n_files"] == 9,
          json.dumps(fleet)[:300])
    check("cluster starts match the measured ones",
          close(by_value["halt"]["t_start"], 1790608972.417, 0.01)
          and by_value["playing"]["t_start"] >= 1790608950,
          json.dumps(fleet)[:300])
    check("the cluster names every file",
          sorted(os.path.basename(x) for x in by_value["halt"]["files"])
          == sorted(EXPECTED), json.dumps(by_value["halt"]["files"])[:200])
    check("the per-file entries still ride along",
          len(event.get("files", [])) == len(LOGS) + 1, len(event.get("files", [])))

    seven = call("get_window", {"column": "mpc_ctrl_mode", "fleet": True,
                                "t1": 1790607600, "t2": 1790613500})
    halts = [row for row in seven.get("fleet", []) if row["value"] == "halt"]
    check("all seven abort events come back with their node counts",
          all(any(close(row["t_start"], want, 1.0) and row["n_files"] == n
                  for row in halts)
              for want, n in EVENT_HALTS),
          json.dumps([(row["t_start"], row["n_files"]) for row in halts])[:400])
    seven_iso = call("get_window", {"column": "mpc_ctrl_mode", "fleet": True,
                                    "t1": 1790608950, "t2": 1790608973, "time_format": "iso"})
    check("iso format reaches the fleet rows too",
          bool(seven_iso.get("fleet"))
          and all(isinstance(row["t_start"], str) and "T" in row["t_start"]
                  for row in seven_iso["fleet"]),
          json.dumps([row["t_start"] for row in seven_iso.get("fleet", [])[:3]])[:200])

    capped = call("get_window", {"column": "mpc_ctrl_mode", "fleet": True,
                                 "t1": 1790607600, "t2": 1790613500, "max_points": 5})
    check("over budget the fleet rows are capped and flagged",
          len(capped.get("fleet", [])) == 5 and capped.get("fleet_truncated") is True,
          f"{len(capped.get('fleet', []))} rows, truncated={capped.get('fleet_truncated')}")

    tight = call("get_window", {"column": "mpc_ctrl_mode", "fleet": True, "tolerance_s": 0.0005,
                                "t1": 1790608950, "t2": 1790608973})
    halt_tight = [row for row in tight.get("fleet", []) if row["value"] == "halt"]
    check("a tighter tolerance splits the halt cluster",
          halt_tight and 0 < halt_tight[0]["n_files"] < 9,
          json.dumps(halt_tight)[:200])

    check("fleet needs several files",
          "__error__" in call("get_window", {"file": f101, "column": "mpc_ctrl_mode",
                                             "fleet": True, "t1": 0, "t2": 4e9}), "")
    check("fleet needs a single column",
          "__error__" in call("get_window", {"columns": ["mpc_ctrl_mode", "mpc_ctrl_state"],
                                             "fleet": True, "t1": 0, "t2": 4e9}), "")
    check("fleet needs segments or transitions",
          "__error__" in call("get_window", {"column": "mpc_ctrl_mode", "fleet": True,
                                             "mode": "summary", "group_by": "value",
                                             "t1": 0, "t2": 4e9}), "")

    print("\n[M4] plot_series / unplot_series")
    analog, enum, text_col = "mpc_ctrl_t_play", "mpc_ctrl_mode", "alarm_data"

    plotted = call("plot_series", {"file": f101, "column": analog})
    check("plot an analog series", plotted["columns"][0]["status"] == "plotted",
          json.dumps(plotted)[:200])
    check("list_series agrees it is plotted",
          analog in call("list_series", {"file": os.path.basename(f101)})["files"][0]["plotted"], "")

    plotted2 = call("plot_series", {"file": f101, "column": enum})
    check("plot an enum series", plotted2["columns"][0]["status"] == "plotted",
          json.dumps(plotted2)[:200])

    tracked = call("plot_series", {"file": f107["file"], "column": text_col})
    check("plot a text series tracks it instead of plotting it",
          tracked["columns"][0]["status"] == "tracked", json.dumps(tracked)[:200])
    check("list_series agrees it is tracked",
          text_col in call("list_series", {"file": os.path.basename(f107["file"])})["files"][0]
          .get("text_tracked", []), "")

    again = call("plot_series", {"file": f101, "column": analog})
    check("plotting again is a no-op with a clear status",
          again["columns"][0]["status"] == "already_plotted", json.dumps(again)[:200])
    again_t = call("plot_series", {"file": f107["file"], "column": text_col})
    check("tracking again is a no-op with a clear status",
          again_t["columns"][0]["status"] == "already_tracked", json.dumps(again_t)[:200])

    both = call("plot_series", {"file": f101, "columns": [analog, enum]})
    check("several columns at once", len(both["columns"]) == 2, json.dumps(both)[:200])

    absent = call("plot_series", {"file": f101, "column": "no-such-column"})
    check("an unknown column comes back absent, not an error",
          absent["columns"][0]["status"] == "absent" and "__error__" not in absent,
          json.dumps(absent)[:200])

    # No 'file' means every loaded file: the column exists in exactly the nine fleet files.
    fleet = call("plot_series", {"column": analog})
    by_status = {}
    for entry in fleet["columns"]:
        by_status.setdefault(entry["status"], []).append(os.path.basename(entry["file"]))
    check("omitting file applies to every loaded file",
          len(by_status.get("plotted", []) + by_status.get("already_plotted", [])) == len(LOGS),
          json.dumps(by_status))
    check("the odd file without the column comes back absent",
          len(by_status.get("absent", [])) == 1
          and by_status["absent"][0] == "fleet_other.csv", json.dumps(by_status))

    un = call("unplot_series", {"column": analog})
    check("unplotting applies to every loaded file too",
          sum(1 for e in un["columns"] if e["status"] == "unplotted") == len(LOGS)
          and all(e["status"] in ("unplotted", "absent") for e in un["columns"]),
          json.dumps(un)[:300])
    check("the curves are gone",
          analog not in call("list_series", {"file": os.path.basename(f101)})["files"][0]["plotted"],
          "")
    un2 = call("unplot_series", {"file": f101, "column": enum})
    check("unplot an enum series", un2["columns"][0]["status"] == "unplotted",
          json.dumps(un2)[:200])
    un3 = call("unplot_series", {"file": f107["file"], "column": text_col})
    check("unplotting a tracked series untracks it",
          un3["columns"][0]["status"] == "untracked", json.dumps(un3)[:200])
    un4 = call("unplot_series", {"file": f107["file"], "column": text_col})
    check("unplotting again is a no-op",
          un4["columns"][0]["status"] == "already_unplotted", json.dumps(un4)[:200])

    print("\n[M4] zoom_to / get_view_range")
    call("plot_series", {"file": f101, "column": analog})   # keep a curve for the y range
    zt1, zt2 = target["start"] + 100, target["start"] + 700
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
    probe = call("get_window", {"file": f101, "column": enum,
                                "t1": target["start"], "t2": target["start"] + 60,
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
    c1 = call("compute_series", {"file": f101, "column": analog, "mode": "abs"})
    check("abs creates and names the derived column", c1["column"] == analog + "_abs",
          json.dumps(c1)[:200])
    check("the derived column is drawn and listed",
          c1["column"] in call("list_series", {"file": os.path.basename(f101)})["files"][0]
          ["plotted"], "")
    cw = call("get_window", {"file": f101, "column": c1["column"],
                             "t1": zt1, "t2": zt2})
    check("the derived column is readable", cw.get("samples", 0) > 0, json.dumps(cw)[:200])
    c2 = call("compute_series", {"file": f101, "column": analog, "mode": "1nd"})
    check("1nd creates the first derivative", c2["column"] == analog + "_1nd",
          json.dumps(c2)[:200])
    ref = call("compute_series", {"file": f101, "column": text_col, "mode": "abs"})
    check("a string series is refused fast, without a modal",
          "__error__" in ref and "cannot calculate" in ref["__error__"], json.dumps(ref)[:200])
    check("a bad mode is an error",
          "__error__" in call("compute_series", {"file": f101, "column": analog,
                                                 "mode": "mean"}), "")
    check("an unknown column is an error",
          "__error__" in call("compute_series", {"file": f101, "column": "nope",
                                                 "mode": "abs"}), "")

    print("\n[M4] search_text")
    none = call("search_text", {"q": "Halt"})
    check("with nothing checked the error names the fix",
          "__error__" in none and "no free-text series" in none["__error__"],
          json.dumps(none)[:200])

    call("plot_series", {"file": f107["file"], "column": text_col})
    s1 = call("search_text", {"q": "Halt"})
    check("the GUI search finds the same 2764 hits find_text counts",
          s1["matches_found"] == 2764, json.dumps(s1)[:200])
    check("the hits are [t, file, column, value] in time order",
          s1["truncated"] is True and len(s1["matches"]) == 50
          and all(len(row) == 4 for row in s1["matches"])
          and [row[0] for row in s1["matches"]] == sorted(row[0] for row in s1["matches"]),
          json.dumps(s1["matches"][:2])[:200])
    s2 = call("search_text", {"q": "Halt", "limit": 5})
    check("limit truncates the list but not the count",
          len(s2["matches"]) == 5 and s2["matches_found"] == 2764 and s2["truncated"] is True,
          json.dumps(s2)[:200])
    s3 = call("search_text", {"q": "Halt", "time_format": "iso"})
    check("iso times come back as strings",
          isinstance(s3["matches"][0][0], str) and "T" in s3["matches"][0][0],
          json.dumps(s3["matches"][0])[:200])
    bad = call("search_text", {"q": "Halt("})
    check("a broken regex is a clear error",
          "__error__" in bad and "regular expression" in bad["__error__"], json.dumps(bad)[:200])
    un5 = call("unplot_series", {"file": f107["file"], "column": text_col})
    check("untracking brings the error back",
          un5["columns"][0]["status"] == "untracked"
          and "__error__" in call("search_text", {"q": "Halt"}), json.dumps(un5)[:200])

    print("\n[W8] backpressure")
    busy_file = f101
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
