---
name: logplot
description: Drive the running LogPlot desktop log viewer (CSV / CSV.ZST machine logs) over its localhost MCP endpoint — load logs, list signals, screen which signals changed, read time windows and snapshots. Use when the user names a logplot instance or a `*.csv.zst` / machine-controller log (`mpc_ctrl_mode`, `alarm_data`, `servo_alarm_*`, `t_play`, `st_*`), or asks which signal changed when / what happened at a timestamp. 當使用者要看 logplot、要載入或查詢某份機器 log 的訊號、找異常時點或某瞬間的值時使用。
---

# Driving LogPlot from the command line

`logplot.py` in the repository provides a stdlib-only command-line client for the MCP
endpoint of a **running LogPlot GUI**. Client commands run before GUI dependencies are
imported. Every call acts on the window the human is looking at, so **the user sees
every change** (files loaded, series plotted) immediately.

Nothing is started, connected, or loaded until you run a command — the client half of
MCP lives in `logplot.py`, not in the agent's session.

## Installation setup

When installing this skill, ask the user for the absolute path to their `log_tools`
directory. Use that answer as `<repo>` for the installed skill, and verify that
`<repo>/logplot.py` exists. If the user has already supplied the path, use it without
asking again. If the path is not recorded after installation, ask before running a
command that needs `<repo>`.

In the examples below, `<repo>` means that `log_tools` directory. Run the commands
from that directory or replace `<repo>` with its absolute path.

## Prerequisites

- Use Python 3.9+. Unless otherwise specified, the runtime is located in `<repo>`.
  Set `LOGPLOT_MCP_TOKEN` if the instance requires a Bearer token.
- Endpoint resolution order: `--url` / `$LOGPLOT_MCP_URL` → the GUI's own file
  `%TEMP%\logplot-mcp.json` → `http://127.0.0.1:8790/mcp`.
- If the GUI is not running there is **no** logplot tool in the agent's tool list: this
  skill is the entry point, and step 1 below is what brings the instance up.

## Step 1 — is an instance answering?

```bash
python <repo>/logplot.py status
```

- `"reachable": true` → use it (report `url` / `pid` to the user if useful).
- `"reachable": false` with `"discovery_stale": true` → the GUI was killed, not closed;
  its discovery file is left behind. Start a new one.
- Not reachable → start it, optionally with the log already loaded:

```bash
python <repo>/logplot.py start --load "C:\path\to\log-20260101.log_mpc_ctrl_105.csv.zst"
```

`start` is lazy and idempotent: it returns immediately if the endpoint already answers,
otherwise it launches `logplot.py`, waits up to 60 s for the handshake, and logs failures
to `%TEMP%\logplot-mcp-launch.log`. Ask the user before starting a second instance if a
window is already open on another port — two GUIs are two different states.

If the user is driving their own instance, do not start anything; use `call load_files`
to add files to the one they have open.

## Step 2 — get the contract from the server, never from memory

```bash
python <repo>/logplot.py tools            # names + short descriptions
python <repo>/logplot.py tools --full     # full inputSchema
python <repo>/logplot.py tools --name get_window --full
```

The schema on the server is the contract. Do **not** hardcode parameter lists, defaults,
or enum names from this document: they change with the version of `logplot.py` the user
is running.

## Step 3 — call tools

```bash
python <repo>/logplot.py call list_files
python <repo>/logplot.py call find_changes '{"t1": 1767224240, "t2": 1767224600}'
python <repo>/logplot.py call get_window @window.json
python <repo>/logplot.py --compact call get_window '{"column": "mpc_ctrl_mode", "t1": "...", "t2": "...", "time_format": "iso"}'
```

- stdout is the tool's JSON payload; a tool error exits 1 with the message on stderr.
- Times are Unix seconds (float) or ISO 8601; `{"time_format": "iso"}` makes the reply
  ISO with the instance's offset. `t ≥ 1e11` is read as nanoseconds.
- `file` may be omitted in the read and view tools: with one loaded file you get the
  single-file reply; with several, the reply carries a per-file list (`get_window` entries
  are bit-for-bit the single-file reply, and a file missing the column carries an error
  entry instead of failing the call). A unique basename also works.

## Step 4 — many calls: one process, `batch`

Each invocation pays an interpreter start (~0.15 s). For a set of queries, feed JSONL to
one process instead — one handshake, then one JSON line per call:

```bash
python <repo>/logplot.py batch <<'JSONL'
{"tool": "find_changes", "args": {"t1": 1767224200, "t2": 1767224500}}
{"tool": "get_window", "args": {"column": "mpc_ctrl_mode", "t1": 1767224200, "t2": 1767224500, "mode": "transitions"}}
JSONL
```

Each output line is `{"tool": .., "ok": true, "result": ..}` or
`{"tool": .., "ok": false, "error": ".."}`. "The same signal across N loaded files" does
not need N calls: omit `file` in `get_window` to get one per-file entry each, or add
`"fleet": true` (one `column`, modes `segments`/`transitions`) to cluster that signal's
runs across the files by value and start time — each `fleet` row is
`{value, t_start, t_end, n_files, files}`, so "which nodes halted at the same time" is one
row per cluster (`tolerance_s` tightens/loosens the start-time grouping, default 1 s).

## Rules the server holds you to

- **Structure, not semantics.** The server reports facts about the data (values, distinct
  counts, change positions, sampling, ranges) and never claims what a value *means*.
  `12`, `AI_W1`, `95.5` or an `alarm_data` string stay uninterpreted: the meaning comes
  from the user, from a loaded SignalList, or from correlating signals.
- **Do not decode payload grammars yourself** into the reply as if the server said it.
  If you infer something (for example what an alarm string denotes), label it as your
  inference, not the tool's output.
- **Two things may be assumed**: CSV format, and that the first column is the timestamp.
- **Backpressure**: the GUI thread serves one tool call at a time and rejects the 9th
  queued call with `server busy`. Send fewer calls at once (≤8, ideally fewer) and retry.
- **Destructive operations do not exist** (no delete, no save/load state, no script
  update). Do not promise them.

## Typical flow (state-machine forensics)

1. `list_files` — confirm files, row counts, time range, `sampling_median_s`,
   `constant_columns` (signals that never moved carry no information). Then
   `list_series`: with several files of one schema it lists the schema once with the rest
   named `same_as`, and names each enum's distinct values under `values`; a
   `kind_conflicts` block reports any name drawn differently in different files.
2. `find_changes` over the whole file, then again over a narrow window around the
   suspected moment — the second one lists exactly the signals that moved there.
3. `get_window` with `mode: "transitions"` (state machines) or `mode: "summary"` +
   `group_by` (counts and durations per state/transition), `min_duration` to drop jitter.
   Across several loaded files, omit `file` (per-file entries) or add `"fleet": true` to
   get one row per (value, start-time cluster) with `n_files` — "all N nodes halted at the
   same instant" comes back as a single row.
4. `get_window` with `columns: [...]` and `t1 == t2` (snapshot) to read N signals at the
   instant of interest in one call.
5. Report the finding with timestamps in the user's timezone (`time_format: "iso"`), and
   say which calls produced the numbers.

## Troubleshooting

| Symptom | Cause / action |
|---|---|
| `cannot reach http://127.0.0.1:8790/mcp` | GUI not running → `start`, or the user's instance uses another port → `status` / `--url` |
| `401 unauthorized` | Instance requires a token: set `LOGPLOT_MCP_TOKEN` (ask the user; never read the discovery file for it — the token is deliberately not in there) |
| `server busy: ... waiting for the GUI thread` | >8 concurrent calls → send fewer, retry |
| A call hangs > 60 s | A modal dialog is open in the GUI, or a big `.zst` is still parsing; the server reports its own 60 s timeout, so raise `--timeout` above it rather than expecting a fast retry |
| `discovery_stale: true` | The GUI was force-killed; the stale file is harmless, `start` will overwrite it |
