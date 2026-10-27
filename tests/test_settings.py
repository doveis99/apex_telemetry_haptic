"""config.json: hand edits of the wrong type and broken files fall back to defaults."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from effects import DEFAULT_ROUTES, Gains
from settings import (AppOptions, delete_preset, load_app_options, load_eq, load_gains, load_presets,
                      save_app_options, save_eq, save_gains, save_preset)


class SettingsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "config.json"

    def tearDown(self) -> None:
        self.folder.cleanup()

    def test_wrong_types_keep_their_defaults(self) -> None:
        self.path.write_text(json.dumps({
            "engine": "loud", "road": 0.3, "cylinders": [8], "disabled": "road", "routes": [1, 2],
            "app": {"auto_start_on_game": True, "debug_log": {"on": 1}},
        }), encoding="utf-8")
        gains = load_gains(self.path)
        self.assertEqual(gains.engine, Gains().engine)
        self.assertEqual(gains.road, 0.3)
        self.assertEqual(gains.cylinders, Gains().cylinders)
        self.assertEqual(gains.disabled, [])
        self.assertEqual(gains.routes, DEFAULT_ROUTES)
        options = load_app_options(self.path)
        self.assertTrue(options.auto_start_on_game)
        self.assertTrue(options.debug_log)  # bool() of any non-empty value; not worth refusing

    def test_unreadable_file_gives_defaults(self) -> None:
        for content in (b"\xff\xfe{ not utf-8", b'{"engine": 0.9', b"[1, 2]"):
            self.path.write_bytes(content)
            self.assertEqual(load_gains(self.path), Gains())
            self.assertEqual(load_app_options(self.path), AppOptions())

    def test_saves_keep_each_others_part_and_leave_no_temp_file(self) -> None:
        save_gains(Gains(engine=0.9), self.path)
        save_app_options(AppOptions(language="de"), self.path)
        self.assertEqual(load_gains(self.path).engine, 0.9)
        self.assertEqual(load_app_options(self.path).language, "de")
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["config.json"])

    def test_calibration_survives_the_other_saves(self) -> None:
        self.assertEqual(load_eq(self.path), {})  # never measured: Equalizer.DEFAULT applies
        save_eq({"grip": {"30": 2.0}}, self.path)
        save_gains(Gains(engine=0.9), self.path)
        save_app_options(AppOptions(language="de"), self.path)
        self.assertEqual(load_eq(self.path), {"grip": {"30": 2.0}})
        self.assertEqual(load_gains(self.path).engine, 0.9)

    def test_presets_keep_their_own_gains_beside_the_others(self) -> None:
        self.assertEqual(load_presets(self.path), {})
        save_gains(Gains(engine=0.9), self.path)
        save_preset("Truck", Gains(cylinders=6), self.path)
        save_preset("Car", Gains(engine=0.3, cylinders=4, disabled=["retarder"]), self.path)
        save_preset("Car", Gains(engine=0.4, cylinders=4), self.path)  # same name replaces it
        save_app_options(AppOptions(preset="Car"), self.path)
        presets = load_presets(self.path)
        self.assertEqual(sorted(presets), ["Car", "Truck"])
        self.assertEqual((presets["Car"].engine, presets["Car"].cylinders, presets["Car"].disabled), (0.4, 4, []))
        self.assertEqual(load_gains(self.path).engine, 0.9)
        self.assertEqual(load_app_options(self.path).preset, "Car")
        delete_preset("Truck", self.path)
        delete_preset("Bus", self.path)  # never there
        self.assertEqual(list(load_presets(self.path)), ["Car"])

    def test_hand_edited_presets_fall_back_like_the_gains(self) -> None:
        self.path.write_text(json.dumps({"presets": {"Car": {"engine": "loud", "road": 0.2}, "Bus": [1]}}),
                             encoding="utf-8")
        presets = load_presets(self.path)
        self.assertEqual(list(presets), ["Car"])
        self.assertEqual((presets["Car"].engine, presets["Car"].road), (Gains().engine, 0.2))
        self.path.write_text(json.dumps({"presets": "Car"}), encoding="utf-8")
        self.assertEqual(load_presets(self.path), {})
        save_preset("Car", Gains(), self.path)
        self.assertEqual(list(load_presets(self.path)), ["Car"])


if __name__ == "__main__":
    unittest.main()
