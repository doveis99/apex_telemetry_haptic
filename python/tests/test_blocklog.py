"""The debug log: one row per block with every column filled, the settings beside it."""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from blocklog import COLUMNS, BlockLog
from effects import DT, Equalizer, Gains, Synth, effective_gains
from scenarios import CITY, Drive


class BlockLogTest(unittest.TestCase):
    def test_rows_carry_the_game_the_synth_and_the_output(self) -> None:
        drive = Drive(CITY)
        synth = Synth(effective_gains(Gains()), Equalizer())
        with tempfile.TemporaryDirectory() as folder:
            log = BlockLog(Path(folder), info={"version": "test", "gains": {"road": 0.5}})
            for n in range(round(12.6 / DT)):  # pulling out with the blinker on, through the first shifts
                frame = drive.frame(n * DT)
                frame.render_time = n * 8000 // 16667 * 16667  # a new game frame about every other block
                log.add(synth.render(frame), frame, synth.levels.events, synth)
            log.add(synth.render(None), None, ["Paused"], synth, preview="gear")
            log.close()
            with log.path.open(encoding="utf-8") as file:
                rows = list(csv.DictReader(file))
            info = json.loads(log.path.with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(info["gains"], {"road": 0.5})
        self.assertTrue(all(len(row) == len(COLUMNS) for row in rows))
        driving = rows[round(11.2 / DT)]  # second gear, past the shift's knock
        self.assertEqual(driving["gear"], "2")
        self.assertEqual(driving["surface_l"], "road")
        self.assertEqual(len(driving["fresh"].split("/")), 3)
        self.assertIn("L", {row["blinkers"] for row in rows[round(3.0 / DT):round(4.0 / DT)]})
        self.assertIn("S", rows[round(12.4 / DT)]["shifter"])  # the splitter flipped before third
        self.assertTrue(any("gear" in row["sounding"].split() for row in rows))
        new = [row for row in rows[:-1] if row["new_frame"] == "1"]
        self.assertTrue(len(rows) / 3 < len(new) < len(rows) * 2 / 3)
        self.assertTrue(all(len(row["deflection_mm"].split("/")) == 10 for row in new))  # truck 6, trailer 4
        self.assertEqual(rows[-1]["speed_kmh"], "")
        self.assertEqual(rows[-1]["events"], "Paused")
        self.assertTrue(rows[-1]["sounding"].startswith("▶gear"))

    def test_old_sessions_go_with_their_settings(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            for k in range(3):
                (Path(folder) / f"blocks-2026010{k}-000000.csv").write_text("")
                (Path(folder) / f"blocks-2026010{k}-000000.json").write_text("{}")
            with mock.patch("blocklog.KEEP", 2):
                BlockLog(Path(folder), info={}).close()
            left = sorted(p.name for p in Path(folder).iterdir())
        self.assertEqual(len(left), 4)  # the newest old session and the new one, each with its .json
        self.assertNotIn("blocks-20260100-000000.json", left)


if __name__ == "__main__":
    unittest.main()
