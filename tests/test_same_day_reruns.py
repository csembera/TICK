"""
Tests for running the collector more than once on one day: a manual
re-crawl, an SQS retry after a failure partway through, a duplicate
delivery. Every run's changes must survive in the day's record.

    python3 -m unittest discover tests

Drives the real collect_company() against a temporary local lake
(LOCAL_OUTPUT_DIR mode), with fetch_all replaced by a fake board that
changes between runs. The company is the fictional Dunder Mifflin.
"""

import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from datetime import date

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.modules.setdefault("boto3", types.SimpleNamespace(client=lambda *a, **k: None))
sys.path.insert(0, os.path.join(ROOT, "infra", "src", "collector"))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import app  # noqa: E402
import audit_diffs  # noqa: E402
import tick_insights_report as report  # noqa: E402

SLUG = "dunder-mifflin"
CONFIG = {"company_name": "Dunder Mifflin", "company_slug": SLUG, "date_trust": "high",
          "schema_map": {"posting_id": "id", "title": "title", "location": "loc", "url": "url"}}
TODAY = date.today().isoformat()
TITLES = {"A": "Sales Rep", "B": "Accountant", "C": "Receptionist", "D": "Warehouse Lead"}


def posting(pid):
    return {"id": pid, "title": TITLES[pid], "loc": "Scranton, PA", "url": f"https://dm.example/{pid}"}


class SameDayReruns(unittest.TestCase):
    def setUp(self):
        self.lake = tempfile.mkdtemp(prefix="tick-test-lake-")
        self._saved = (app.LOCAL_DIR, app.fetch_all, app._write)
        app.LOCAL_DIR = self.lake
        # yesterday's state: B and C open
        state = {pid: {"first_seen": "2000-01-01", "last_seen": "2000-01-01", "closed_on": None,
                       "title": TITLES[pid], "location": "Scranton, PA"} for pid in "BC"}
        self.write(f"derived/state/company={SLUG}/state.json", state)

    def tearDown(self):
        app.LOCAL_DIR, app.fetch_all, app._write = self._saved
        shutil.rmtree(self.lake, ignore_errors=True)

    def write(self, key, obj):
        path = os.path.join(self.lake, key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(obj, f)

    def read(self, key):
        with open(os.path.join(self.lake, key)) as f:
            return f.read()

    def crawl(self, board):
        app.fetch_all = lambda config: ([posting(p) for p in board], len(board))
        return app.collect_company(CONFIG)

    def day(self):
        return json.loads(self.read(f"derived/snapshots/company={SLUG}/dt={TODAY}/diff.json"))

    def state(self):
        return json.loads(self.read(f"derived/state/company={SLUG}/state.json"))

    def test_two_runs_keep_both_runs_changes(self):
        self.crawl("AB")    # A opens, C closes
        self.crawl("BD")    # D opens, A closes
        d = self.day()
        self.assertEqual(d["new"], ["A", "D"])
        self.assertEqual(d["closed"], ["C", "A"])
        self.assertEqual((d["counts"]["new"], d["counts"]["closed"]), (2, 2))
        self.assertEqual(len(d["runs"]), 2)
        # curated stays the board as of the latest run: the site lists it as open roles
        curated = [json.loads(l)["posting_id"] for l in self.read(
            f"curated/company={SLUG}/dt={TODAY}/postings.jsonl").splitlines()]
        self.assertEqual(sorted(curated), ["B", "D"])
        # A opened and closed between the runs: its record is kept beside the board
        departed = [json.loads(l)["posting_id"] for l in self.read(
            f"curated/company={SLUG}/dt={TODAY}/departed.jsonl").splitlines()]
        self.assertEqual(departed, ["A"])
        self.assertEqual(self.state()["A"]["closed_on"], TODAY)

    def test_a_rerun_that_finds_nothing_changes_nothing(self):
        self.crawl("AB")
        self.crawl("BD")
        before = self.day()
        summary = self.crawl("BD")
        after = self.day()
        self.assertEqual((after["new"], after["closed"], after["reopened"]),
                         (before["new"], before["closed"], before["reopened"]))
        self.assertEqual(len(after["runs"]), 3)
        self.assertEqual((summary["new"], summary["closed"], summary["runs_today"]), (0, 0, 3))

    def test_failure_before_state_is_written_retries_to_the_same_changes(self):
        real_write = app._write
        failed = []

        def flaky(key, body):
            if key.endswith("state.json") and not failed:
                failed.append(key)
                raise RuntimeError("S3 hiccup")
            real_write(key, body)
        app._write = flaky
        with self.assertRaises(RuntimeError):
            self.crawl("AB")
        self.assertEqual(self.state()["C"]["closed_on"], None)   # state untouched by the failed run
        self.crawl("AB")                                          # the SQS retry
        d = self.day()
        self.assertEqual((d["new"], d["closed"]), (["A"], ["C"]))  # not emptied by the retry

    def test_close_and_reopen_within_the_day(self):
        self.crawl("B")     # C closes
        self.crawl("BC")    # C is back
        d = self.day()
        self.assertEqual((d["closed"], d["reopened"]), (["C"], ["C"]))
        self.assertIsNone(self.state()["C"]["closed_on"])
        departed = os.path.join(self.lake, f"curated/company={SLUG}/dt={TODAY}/departed.jsonl")
        # C came back, so nothing departed for good
        self.assertTrue(not os.path.exists(departed) or not open(departed).read().strip())

    def test_audit_and_archive_read_the_merged_day(self):
        self.crawl("AB")
        self.crawl("BD")
        io = report.open_lake(self.lake)
        self.assertEqual(audit_diffs.audit(io, SLUG)["gaps"], [])
        self.assertEqual(audit_diffs.audit(io, SLUG)["multi_run_days"], [TODAY])
        lake = report.load_lake(self.lake, SLUG, io)
        self.assertEqual(lake["archive"]["A"]["title"], "Sales Rep")   # from departed.jsonl


class AuditCatchesLoss(unittest.TestCase):
    def test_an_overwritten_day_is_reported(self):
        lake = tempfile.mkdtemp(prefix="tick-test-lake-")
        try:
            def put(key, obj):
                path = os.path.join(lake, key)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w") as f:
                    json.dump(obj, f)
            put(f"derived/state/company={SLUG}/state.json", {
                "A": {"first_seen": "2026-01-02", "closed_on": None, "title": "Sales Rep"},
                "C": {"first_seen": "2026-01-01", "closed_on": "2026-01-02", "title": "Receptionist"}})
            put(f"derived/snapshots/company={SLUG}/dt=2026-01-01/diff.json", {"new": ["C"], "closed": [], "reopened": []})
            # the 2026-01-02 record as a later run would have overwritten it: A and C are gone
            put(f"derived/snapshots/company={SLUG}/dt=2026-01-02/diff.json", {"new": [], "closed": [], "reopened": []})
            gaps = audit_diffs.audit(report.open_lake(lake), SLUG)["gaps"]
            self.assertEqual([(d, p, w) for d, p, w, _ in gaps],
                             [("2026-01-02", "A", "opened"), ("2026-01-02", "C", "closed")])
        finally:
            shutil.rmtree(lake, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
