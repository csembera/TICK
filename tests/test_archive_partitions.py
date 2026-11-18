"""
Tests that the archive finds every closed posting's last record across older
curated partitions, while reading from each day only the rows it keeps.

    python3 -m unittest discover tests

load_lake used to hold every older day's curated board whole until it
returned, which grew the publisher by one full board per crawl until it ran
out of memory. It now keeps only each day's closed rows and skips the rest
before parsing them. These check that the archive it builds is unchanged by
that: the right record from the right day, departed rows included, and rows
the quick ID match can't read still found. Hand-written lake; the company is
the fictional Dunder Mifflin.
"""

import json
import os
import shutil
import sys
import tempfile
import types
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.modules.setdefault("boto3", types.SimpleNamespace(client=lambda *a, **k: None))
sys.path.insert(0, os.path.join(ROOT, "infra", "src", "collector"))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import tick_insights_report as report  # noqa: E402

SLUG = "dunder-mifflin"
DAYS = ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"]
QUOTED = 'Q"1'          # an id that needs escaping in JSON


def row(pid, title):
    return {"posting_id": pid, "company": "Dunder Mifflin", "company_slug": SLUG,
            "title": title, "location": "Scranton, PA"}


class ArchivePartitions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lake = tempfile.mkdtemp(prefix="tick-test-lake-")

        def write(key, text):
            path = os.path.join(cls.lake, key)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(text)

        boards = {
            DAYS[0]: [row("OPEN", "Regional Manager"), row("OLD", "Paper Salesman, first day"),
                      row("MID", "Accountant, first draft"), row(QUOTED, "Quoted Clerk")],
            DAYS[1]: [row("OPEN", "Regional Manager"), row("MID", "Accountant")],
            DAYS[2]: [row("OPEN", "Regional Manager")],
            DAYS[3]: [row("OPEN", "Regional Manager")],
        }
        for dt, rows in boards.items():
            lines = [json.dumps(r) for r in rows]
            if dt == DAYS[0]:
                # posting_id not first: the quick match misses, so it must parse
                lines.append(json.dumps({"title": "Late Key Temp", "posting_id": "LATE"}))
            write(f"curated/company={SLUG}/dt={dt}/postings.jsonl", "\n".join(lines) + "\n")
            write(f"derived/tech_stack/company={SLUG}/dt={dt}/tech_stack.json", json.dumps({
                "company": "Dunder Mifflin", "company_slug": SLUG, "captured_dt": dt,
                "postings_total": len(rows), "tech_role_ids": [], "stack": {}, "postings": {}}))
        # opened and closed between two runs on DAYS[1]: only in departed.jsonl
        write(f"curated/company={SLUG}/dt={DAYS[1]}/departed.jsonl",
              json.dumps(row("GONE", "Temp")) + "\n")

        def closed(last_seen, title):
            return {"first_seen": DAYS[0], "last_seen": last_seen,
                    "closed_on": last_seen, "title": title, "location": "Scranton, PA"}
        state = {
            "OPEN": {"first_seen": DAYS[0], "last_seen": DAYS[3], "title": "Regional Manager"},
            "OLD": closed(DAYS[0], "Paper Salesman"),
            "MID": closed(DAYS[1], "Accountant"),
            "GONE": closed(DAYS[1], "Temp"),
            QUOTED: closed(DAYS[0], "Quoted Clerk"),
            "LATE": closed(DAYS[0], "Late Key Temp"),
        }
        write(f"derived/state/company={SLUG}/state.json", json.dumps(state))
        io = report.open_lake(cls.lake)
        cls.archive = report.load_lake(cls.lake, SLUG, io)["archive"]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.lake, ignore_errors=True)

    def test_only_closed_postings_are_archived(self):
        self.assertEqual(set(self.archive), {"OLD", "MID", "GONE", QUOTED, "LATE"})

    def test_a_posting_closed_days_ago_keeps_its_last_record(self):
        self.assertEqual(self.archive["OLD"]["title"], "Paper Salesman, first day")

    def test_the_record_comes_from_its_last_seen_day(self):
        self.assertEqual(self.archive["MID"]["title"], "Accountant")

    def test_a_same_day_departure_is_found_in_departed(self):
        self.assertEqual(self.archive["GONE"]["title"], "Temp")

    def test_an_escaped_id_is_matched(self):
        self.assertEqual(self.archive[QUOTED]["title"], "Quoted Clerk")

    def test_a_row_without_posting_id_first_is_still_read(self):
        self.assertEqual(self.archive["LATE"]["title"], "Late Key Temp")


if __name__ == "__main__":
    unittest.main()
