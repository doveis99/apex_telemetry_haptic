"""A drive written by the debug log comes back as the frames the game sent, old logs too."""

from __future__ import annotations

import csv
import json
import math
import tempfile
import unittest
from pathlib import Path

from blocklog import BlockLog
from effects import DT, Equalizer, Gains, Synth, effective_gains
from replay import LogTelemetry, LogReader, replay, settings_of
from scenarios import CITY, Drive


def write_log(folder: Path, seconds: float, events: tuple[tuple[float, str], ...] = ()) -> Path:
    """The city drive's first seconds through the debug log, a game frame about every other block."""
    drive = Drive(CITY)
    synth = Synth(effective_gains(Gains()), Equalizer())
    log = BlockLog(folder, info={"version": "test", "gains": {"road": 0.25}, "eq_curves": {"grip": {"65": 1.0}}})
    for n in range(round(seconds / DT)):
        frame = drive.frame(n * DT)
        frame.render_time = n * 8000 // 16667 * 16667
        frame.accel = (0.5, 9.8, -1.25)
        frame.game_events = frozenset(name for at, name in events if at <= n * DT < at + 0.1)
        if n in (100, 101):  # 101 has a new game frame
            frame.wheels[3].on_ground = False
        log.add(synth.render(frame), frame, synth.levels.events, synth)
        if n == 200:
            log.add(synth.render(None), None, ["Paused"], synth)
    log.close()
    return log.path


class ReplayTest(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = write_log(Path(self.folder.name), 14.0, events=((10.0, "fined"),))

    def test_frames_come_back_as_sent(self) -> None:
        drive = Drive(CITY)
        rows = list(LogReader(self.path).rows())
        self.assertEqual(len(rows), round(14.0 / DT) + 1)
        for n in (60, 100, 400, 1300):
            n += n * 8000 // 16667 * 16667 == (n - 1) * 8000 // 16667 * 16667  # a block with a new game frame
            k = n + (n > 200)  # the paused row
            sent, (_, _, got) = drive.frame(n * DT), rows[k]
            self.assertEqual(got.render_time, n * 8000 // 16667 * 16667)
            self.assertAlmostEqual(got.speed, sent.speed, delta=0.05 / 3.6)
            self.assertEqual((got.gear, got.engine_on, got.park_brake, got.trailer_attached),
                             (sent.gear, sent.engine_on, sent.park_brake, sent.trailer_attached))
            self.assertEqual(len(got.wheels), len(sent.wheels))
            self.assertEqual(len(got.trailer_wheels), len(sent.trailer_wheels))
            for g, s in zip(got.wheels + got.trailer_wheels, sent.wheels + sent.trailer_wheels):
                self.assertAlmostEqual(g.deflection, s.deflection, delta=0.00005)
                self.assertEqual(g.substance, s.substance)
                self.assertEqual(g.pos_x < 0, s.pos_x < 0)
            for g, s in zip(got.wheels, sent.wheels):
                self.assertAlmostEqual(g.velocity * g.radius, s.velocity * s.radius, delta=0.005 / (2 * math.pi))
            self.assertEqual(got.accel, (0.5, 9.8, -1.25))
        self.assertFalse(rows[101][2].wheels[3].on_ground)
        self.assertTrue(rows[201][2].paused)
        fined = [n for n, (_, _, f) in enumerate(rows) if f.game_events]
        self.assertEqual(rows[fined[0]][2].game_events, {"fined"})
        self.assertTrue(round(10.0 / DT) + 1 <= fined[0] <= round(10.0 / DT) + 3)  # on the next game frame
        self.assertIs(rows[62][2], rows[61][2])  # no new game frame: the one before again

    def test_a_stretch_of_the_log(self) -> None:
        rows = list(LogReader(self.path).rows())
        start, end = rows[300][0], rows[500][0]
        stretch = list(LogReader(self.path, start, end).rows())
        self.assertEqual(stretch[0][0], start)
        self.assertEqual(stretch[-1][0], end)

    def test_an_old_log_without_the_frame_columns(self) -> None:
        with self.path.open(encoding="utf-8", newline="") as file:
            rows = list(csv.DictReader(file))
        old = self.path.with_name("blocks-old.csv")
        columns = [c for c in rows[0] if c not in ("render_us", "game_throttle", "game_brake", "rpm_max",
                                                   "retarder_steps", "air_pressure", "accel", "wheel_speed",
                                                   "airborne", "substances", "game_events")]
        for n, row in enumerate(rows):  # written as fast as it goes; a drive takes 8 ms a block
            row["time"] = f"12:00:{n * 0.008:06.3f}" if n < 7500 else row["time"]
        with old.open("w", encoding="utf-8", newline="") as file:
            writer = csv.DictWriter(file, columns, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        frames = [frame for _, _, frame in LogReader(old).rows()]
        drive = Drive(CITY)
        got, sent = frames[401], drive.frame(400 * DT)  # past the paused row
        self.assertEqual(len(got.wheels), 6)  # the old logs' tractor
        self.assertEqual(len(got.trailer_wheels), 4)
        self.assertAlmostEqual(got.wheels[2].deflection, sent.wheels[2].deflection, delta=0.00005)
        self.assertEqual(got.accel, (0.0, 0.0, 0.0))
        self.assertGreater(frames[403].render_time, frames[401].render_time)  # from the wall clock

    def test_the_settings_it_was_recorded_with(self) -> None:
        gains, eq, version = settings_of(self.path)
        self.assertEqual((gains.road, gains.gear, version), (0.25, Gains().gear, "test"))
        self.assertEqual(eq.curves, {"grip": {"65": 1.0}})
        info = self.path.with_suffix(".json")
        # before 0.16.0 the log kept the curves' points: (ln Hz, ln gain + ln LEVEL)
        info.write_text(json.dumps({"eq": {"grip": [[math.log(65.0), math.log(2.0 * Equalizer.LEVEL["grip"])]]}}))
        _, eq, _ = settings_of(self.path)
        self.assertAlmostEqual(eq.gain("grip", 65.0), Equalizer({"grip": {"65": 2.0}}).gain("grip", 65.0))

    def test_the_replay_hears_what_the_drive_did(self) -> None:
        logged, replayed, blocks = replay(self.path, Gains(), Equalizer())
        self.assertEqual(blocks, round(14.0 / DT) + 1)
        self.assertEqual(len(replayed.peaks[0]), blocks)
        self.assertEqual(replayed.fade_resets, logged.fade_resets)

    def test_it_plays_in_a_loop_with_a_pause(self) -> None:
        source = LogTelemetry(self.path)
        frames = [source.read() for _ in range(round(14.0 / DT) + 1 + LogTelemetry.PAUSE_BLOCKS + 2)]
        pause = frames[round(14.0 / DT) + 1:-2]
        self.assertTrue(all(frame.paused for frame in pause))
        self.assertEqual(len({frame.render_time for frame in pause}), len(pause))  # live again after it
        self.assertTrue(frames[-1].live)


if __name__ == "__main__":
    unittest.main()
