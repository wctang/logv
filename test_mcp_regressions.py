"""Small regression checks for fleet semantics and the HTTP request boundary."""

import http.client
import json
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pandas as pd

from logplot import McpBridge, McpHttpServer, _fleet_rows, MCP_MAX_REQUEST_BYTES


class _WindowReader:
    _to_ns = staticmethod(McpBridge._to_ns)
    _window_slice = McpBridge._window_slice


class _EmptyBridge:
    tools = {}

    def list_tools(self):
        return []


class _FleetWindowReader:
    """Just enough of McpBridge to run get_window's argument checks without a viewer.

    Two files are loaded, and their runs start 0.4 s apart: one cluster at the default
    1 s tolerance, two clusters at 0.2 s.
    """
    _column_names = McpBridge._column_names
    _window_args = McpBridge._window_args
    _window_time = McpBridge._window_time
    _time_formatter = McpBridge._time_formatter
    _window_options = McpBridge._window_options
    _resolve_file = McpBridge._resolve_file
    _positive_int = staticmethod(McpBridge._positive_int)

    def __init__(self):
        self._a = pd.DataFrame({"v": [1]})
        self._b = pd.DataFrame({"v": [1]})
        self._runs = {id(self._a): [(1.0, 2.0, 1)], id(self._b): [(1.4, 2.4, 1)]}
        self.viewer = types.SimpleNamespace(
            dataframes={"a.csv": self._a, "b.csv": self._b},
            x_axis=types.SimpleNamespace(utcOffset=0))

    def _window_one(self, path, df, name, t1, t2, fmt, opts):
        return {"segments": []}

    def _fleet_runs(self, df, column, t1, t2, min_duration=0):
        return self._runs[id(df)]


class McpRegressions(unittest.TestCase):
    def test_fleet_debounce_matches_per_file_runs(self):
        times = pd.Index([0, 1, 2, 3, 4], dtype="int64") * 1_000_000_000
        df = pd.DataFrame({"state": ["ready", "playing", "halt", "playing", "halt"]},
                          index=times)
        reader = _WindowReader()
        raw = McpBridge._fleet_runs(reader, df, "state", 0, 4)
        debounced = McpBridge._fleet_runs(reader, df, "state", 0, 4, 1.5)
        self.assertEqual(len(raw), 5)
        self.assertEqual([(start, value) for start, _end, value in debounced],
                         [(0.0, "ready")])

    def test_fleet_keeps_numeric_and_text_values_separate(self):
        rows = _fleet_rows({"numeric": [(0, 2, 1)], "text": [(0, 2, "1")]}, 1)
        self.assertEqual(len(rows), 2)
        self.assertEqual(sorted(len(files) for _value, _start, _end, files in rows), [1, 1])

    def test_get_window_accepts_fleet_tolerance_and_applies_it(self):
        reader = _FleetWindowReader()
        args = {"column": "v", "t1": 0, "t2": 10, "mode": "segments", "fleet": True}
        out = McpBridge._tool_get_window(reader, dict(args, tolerance_s=0.2))
        self.assertEqual(len(out["fleet"]), 2)     # 0.4 s apart exceeds 0.2 s
        out = McpBridge._tool_get_window(reader, args)
        self.assertEqual(len(out["fleet"]), 1)     # within the default 1 s
        self.assertEqual(out["fleet"][0]["n_files"], 2)

    def test_get_window_rejects_bad_or_out_of_scope_tolerance(self):
        reader = _FleetWindowReader()
        plain = {"file": "a.csv", "column": "v", "t1": 0, "t2": 10, "mode": "segments"}
        fleet = {"column": "v", "t1": 0, "t2": 10, "mode": "segments", "fleet": True}
        self.assertEqual(McpBridge._tool_get_window(reader, plain)["file"], "a.csv")
        for bad in (-1, True, "1", float("nan")):
            with self.assertRaises(ValueError) as ctx:
                McpBridge._tool_get_window(reader, dict(fleet, tolerance_s=bad))
            self.assertIn("must be a non-negative number", str(ctx.exception))
        with self.assertRaises(ValueError) as ctx:
            McpBridge._tool_get_window(reader, dict(plain, tolerance_s=5))
        self.assertIn("only applies with fleet=true", str(ctx.exception))

    def test_http_rejects_oversized_and_bad_lengths_then_stays_usable(self):
        server = McpHttpServer(_EmptyBridge(), 0)
        server.start()
        try:
            def post(body, headers=None):
                connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=3)
                connection.request("POST", "/mcp", body=body, headers=headers or {})
                response = connection.getresponse()
                status, payload = response.status, response.read()
                connection.close()
                return status, payload

            status, _ = post(b"x", {"Content-Length": str(MCP_MAX_REQUEST_BYTES + 1)})
            self.assertEqual(status, 413)
            status, _ = post(b"x", {"Content-Length": "invalid"})
            self.assertEqual(status, 400)
            status, payload = post(json.dumps({"jsonrpc": "2.0", "id": 1,
                                               "method": "ping"}).encode())
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(payload)["result"], {})
        finally:
            server.stop()


if __name__ == "__main__":
    unittest.main()
