"""The window's presets: what a click or a rename leaves in the window and in config.json."""

from __future__ import annotations

import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest import mock

import gui
import settings
from effects import Gains


class PresetTest(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        for patch in (mock.patch.object(settings, "CONFIG_PATH", Path(folder.name) / "config.json"),
                      mock.patch.object(gui.updater, "newer_release", lambda: None)):  # no network
            patch.start()
            self.addCleanup(patch.stop)
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"no display: {exc}")
        self.addCleanup(self.root.destroy)
        gui.apply_theme(self.root)

    def open(self, active: str, **presets: Gains) -> gui.App:
        """The window as it opens with these presets stored and one of them in use."""
        for name, gains in presets.items():
            settings.save_preset(name, gains)
        settings.save_gains(presets[active])
        settings.save_app_options(settings.AppOptions(preset=active))
        app = gui.App(self.root, dry_run=True)
        self.addCleanup(app.watcher.close)
        return app

    def move_engine(self, app: gui.App, percent: int) -> None:
        app.slider_vars["engine"][0].set(percent)
        app._on_slider("engine")

    def test_saving_before_clicking_the_preset_in_use_keeps_the_change(self) -> None:
        app = self.open("Truck", Truck=Gains(engine=0.1))
        self.move_engine(app, 50)
        with mock.patch.object(gui.messagebox, "askyesnocancel", return_value=True):  # save first: yes
            app._switch_preset("Truck")
        self.assertEqual(app.gains.engine, 0.5)
        self.assertEqual(settings.load_gains().engine, 0.5)
        self.assertEqual(settings.load_presets()["Truck"].engine, 0.5)

    def test_not_saving_before_clicking_the_preset_in_use_reverts(self) -> None:
        app = self.open("Truck", Truck=Gains(engine=0.1))
        self.move_engine(app, 50)
        with mock.patch.object(gui.messagebox, "askyesnocancel", return_value=False):
            app._switch_preset("Truck")
        self.assertEqual(app.gains.engine, 0.1)
        self.assertEqual(settings.load_presets()["Truck"].engine, 0.1)

    def test_saving_before_switching_writes_into_the_preset_left(self) -> None:
        app = self.open("Truck", Truck=Gains(engine=0.1), Car=Gains(engine=0.3))
        self.move_engine(app, 50)
        with mock.patch.object(gui.messagebox, "askyesnocancel", return_value=True):
            app._switch_preset("Car")
        self.assertEqual(app.gains.engine, 0.3)
        self.assertEqual(settings.load_presets()["Truck"].engine, 0.5)
        self.assertEqual(settings.load_app_options().preset, "Car")

    def test_renaming_onto_the_preset_in_use_loads_what_it_now_holds(self) -> None:
        app = self.open("Car", Truck=Gains(engine=0.1), Car=Gains(engine=0.3))
        with mock.patch.object(gui.simpledialog, "askstring", return_value="Car"), \
                mock.patch.object(gui.messagebox, "askyesno", return_value=True):  # replace 'Car': yes
            app._rename_preset("Truck")
        self.assertEqual(sorted(settings.load_presets()), ["Car"])
        self.assertEqual(app._active_preset(), "Car")
        self.assertEqual(app.gains.engine, 0.1)
        self.assertEqual(settings.load_gains().engine, 0.1)
        self.assertEqual(app.save_state.get(), gui.t("✓ '{name}'(으)로 바꿈", name="Car"))


if __name__ == "__main__":
    unittest.main()
