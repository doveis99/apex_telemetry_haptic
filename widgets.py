"""Dark theme and the small canvas widgets the tuning window is built from.

Plain tkinter only (no extra packages), so the PyInstaller build stays as it
is. Every widget draws itself on a Canvas: rounded segment buttons, an on/off
switch, a slider whose fill shows the route colour, a level meter and a
hover tooltip. The font follows the UI language (i18n.ui_font).

The window draws at the screen's own resolution (make_dpi_aware), so every
size here is in design pixels, as laid out at 100 % scaling, and px() turns
it into screen pixels; fonts are in points and scale by themselves.
"""

from __future__ import annotations

import ctypes
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk
from typing import Callable, Sequence

import i18n

BG = "#0e1014"          # window
CARD = "#161a21"        # panels
TILE = "#1c2029"        # telemetry tiles, log
FIELD = "#232833"       # tracks, idle segments
FIELD_HI = "#2e3441"    # hover
TEXT = "#e7e9ee"
MUTED = "#8a92a3"
FAINT = "#555c6b"
INK = "#0e1014"         # text on a coloured fill

GRIP_COLOR = "#5aa2ff"
TRIGGER_COLOR = "#ff9a4d"
BOTH_COLOR = "#b58cff"
ACCENT = "#5aa2ff"
GO = "#2ec27e"
STOP = "#f05252"
WARN = "#f5b942"

BUTTON_KINDS = {  # fill, hover fill, text
    "go": (GO, "#3fd590", INK),
    "danger": (STOP, "#ff6b6b", "#ffffff"),
    "primary": (ACCENT, "#7ab6ff", INK),
    "ghost": (FIELD, FIELD_HI, TEXT),
}
SCALE = 1.0  # screen pixels per design pixel; set by apply_theme from the screen DPI


def make_dpi_aware() -> None:
    """Draw at the screen's real resolution; call before the first window opens.

    Otherwise Windows draws the window at 96 DPI and stretches the bitmap
    to the display scaling, which blurs it at 125 % and up. System aware,
    not per monitor: Tk 8.6 does not redraw for a move to a monitor with
    another scaling, so there Windows stretches it as before.
    """
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # PROCESS_SYSTEM_DPI_AWARE
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()  # before Windows 8.1
        except (AttributeError, OSError):
            pass


def px(size: float) -> int:
    """A design-pixel size in screen pixels."""
    return round(size * SCALE)


def font(size: int = 10, weight: str = "normal") -> tuple[str, int, str]:
    return (i18n.ui_font(), size, weight)


def round_rect(canvas: tk.Canvas, x1: float, y1: float, x2: float, y2: float, r: float, **kw) -> int:
    r = min(r, (x2 - x1) / 2, (y2 - y1) / 2)
    points = (x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
              x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1)
    return canvas.create_polygon(points, smooth=True, **kw)


def apply_theme(root: tk.Tk) -> None:
    """Dark clam theme for the ttk widgets that remain (dialogs, spinbox); also sets SCALE."""
    global SCALE
    SCALE = max(1.0, root.winfo_fpixels("1i") / 96)
    root.configure(bg=BG)
    root.option_add("*Font", font())
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(".", background=BG, foreground=TEXT, fieldbackground=FIELD, bordercolor=FIELD,
                    lightcolor=FIELD, darkcolor=FIELD, troughcolor=FIELD, arrowcolor=TEXT,
                    selectbackground=ACCENT, selectforeground=INK, insertcolor=TEXT, font=font())
    style.configure("TButton", background=FIELD, padding=(px(12), px(5)), relief="flat")
    style.map("TButton", background=[("disabled", BG), ("active", FIELD_HI)],
              foreground=[("disabled", FAINT)])
    style.configure("TSpinbox", background=FIELD, arrowsize=px(12), padding=(px(6), px(2)))
    style.map("TSpinbox", background=[("active", FIELD_HI)])


def dark_title_bar(window: tk.Misc) -> None:
    """Ask Windows 10/11 for a dark caption; harmless where unsupported."""
    try:
        window.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        value = ctypes.c_int(1)
        for attribute in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE, pre-20H1 value
            if not ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(value), 4):
                break
        # repaint the caption now rather than on the next activation
        ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0027)  # NOSIZE|NOMOVE|NOZORDER|FRAMECHANGED
    except Exception:
        pass


def card(parent: tk.Misc, title: str, hint: str = "") -> tk.Frame:
    """A rounded-looking panel with a heading; returns the body frame to fill."""
    outer = tk.Frame(parent, bg=CARD, highlightthickness=1, highlightbackground="#20252f")
    head = tk.Frame(outer, bg=CARD)
    head.pack(fill="x", padx=px(14), pady=(px(10), px(4)))
    tk.Label(head, text=title, bg=CARD, fg=TEXT, font=font(11, "bold")).pack(side="left")
    if hint:
        tk.Label(head, text=hint, bg=CARD, fg=FAINT, font=font(9)).pack(side="left", padx=(px(8), 0), pady=(px(2), 0))
    body = tk.Frame(outer, bg=CARD)
    body.pack(fill="both", expand=True, padx=px(14), pady=(0, px(12)))
    body.outer = outer  # type: ignore[attr-defined]  # what the caller lays out
    body.head = head  # type: ignore[attr-defined]
    return body


class Button(tk.Canvas):
    """Rounded flat button. `set` changes text, colour kind or state in place."""

    def __init__(self, parent: tk.Misc, text: str, command: Callable[[], None], kind: str = "ghost",
                 size: int = 10, height: int = 32, min_width: int = 0, bg: str = BG) -> None:
        super().__init__(parent, height=px(height), bg=bg, highlightthickness=0, bd=0, cursor="hand2")
        self.command = command
        self.text, self.kind, self.state = text, kind, "normal"
        self.font = font(size, "bold")
        self.min_width = px(min_width)
        self.hover = False
        self.bind("<Enter>", lambda _: self._hover(True))
        self.bind("<Leave>", lambda _: self._hover(False))
        self.bind("<ButtonRelease-1>", self._click)
        self._draw()

    def set(self, text: str | None = None, kind: str | None = None, state: str | None = None) -> None:
        new = (text or self.text, kind or self.kind, state or self.state)
        if new != (self.text, self.kind, self.state):
            self.text, self.kind, self.state = new
            self._draw()

    def _hover(self, on: bool) -> None:
        self.hover = on
        self._draw()

    def _click(self, event: tk.Event) -> None:
        inside = 0 <= event.x < self.winfo_width() and 0 <= event.y < self.winfo_height()
        if inside and self.state == "normal":
            self.command()

    def _draw(self) -> None:
        width = max(self.min_width, tkfont.Font(font=self.font).measure(self.text) + px(32))
        height = int(self["height"])
        self.config(width=width, cursor="hand2" if self.state == "normal" else "arrow")
        fill, hover, ink = BUTTON_KINDS[self.kind]
        if self.state != "normal":
            fill, ink = FIELD, FAINT
        elif self.hover:
            fill = hover
        self.delete("all")
        round_rect(self, 1, 1, width - 1, height - 1, px(9), fill=fill, outline="")
        self.create_text(width / 2, height / 2, text=self.text, fill=ink, font=self.font)


class Segmented(tk.Canvas):
    """A row of mutually exclusive segments, each with its own colour when picked.

    `dim` greys the picked segment (the effect is off but remembers its
    route); `locked` ignores clicks.
    """

    def __init__(self, parent: tk.Misc, options: Sequence[tuple[str, str, str]],
                 command: Callable[[str], None], segment_width: int = 54, height: int = 28,
                 size: int = 9, bg: str = CARD) -> None:
        self.options = list(options)  # value, text, colour
        self.font = font(size, "bold")
        # wide enough for the longest text, which depends on the language
        measure = tkfont.Font(font=self.font).measure
        self.inset = px(3)
        self.segment_width = max(px(segment_width), *(measure(text) + px(16) for _, text, _ in self.options))
        width = self.segment_width * len(self.options) + 2 * self.inset
        super().__init__(parent, width=width, height=px(height), bg=bg, highlightthickness=0, bd=0,
                         cursor="hand2")
        self.command = command
        self.value: str | None = None
        self.dim = self.locked = False
        self.hovered: int | None = None
        self.bind("<Motion>", lambda e: self._hover(self._index(e.x)))
        self.bind("<Leave>", lambda _: self._hover(None))
        self.bind("<Button-1>", self._click)
        self._draw()

    def set(self, value: str | None, dim: bool = False, locked: bool = False) -> None:
        if (value, dim, locked) != (self.value, self.dim, self.locked):
            self.value, self.dim, self.locked = value, dim, locked
            self.config(cursor="arrow" if locked else "hand2")
            self._draw()

    def _index(self, x: int) -> int | None:
        index = (x - self.inset) // self.segment_width
        return index if 0 <= index < len(self.options) else None

    def _hover(self, index: int | None) -> None:
        if index != self.hovered:
            self.hovered = index
            self._draw()

    def _click(self, event: tk.Event) -> None:
        index = self._index(event.x)
        if index is not None and not self.locked:
            self.command(self.options[index][0])

    def _draw(self) -> None:
        self.delete("all")
        width, height, inset = int(self["width"]), int(self["height"]), self.inset
        round_rect(self, 0, 0, width, height, px(9), fill=FIELD, outline="")
        for i, (value, text, color) in enumerate(self.options):
            x1 = inset + i * self.segment_width
            x2 = x1 + self.segment_width
            picked = value == self.value
            if picked and not self.dim:
                round_rect(self, x1, inset, x2, height - inset, px(7), fill=FAINT if self.locked else color,
                           outline="")
                ink = INK
            elif picked:
                round_rect(self, x1 + 1, inset + 1, x2 - 1, height - inset - 1, px(7), fill=FIELD, outline=FAINT)
                ink = MUTED
            else:
                hover = i == self.hovered and not self.locked
                if hover:
                    round_rect(self, x1, inset, x2, height - inset, px(7), fill=FIELD_HI, outline="")
                ink = TEXT if hover else (FAINT if self.dim or self.locked else MUTED)
            self.create_text((x1 + x2) / 2, height / 2, text=text, fill=ink, font=self.font)


class Switch(tk.Canvas):
    """On/off pill bound to a BooleanVar; `command` runs after a click."""

    def __init__(self, parent: tk.Misc, variable: tk.BooleanVar, command: Callable[[], None],
                 color: str = GO, bg: str = CARD) -> None:
        super().__init__(parent, width=px(36), height=px(20), bg=bg, highlightthickness=0, bd=0, cursor="hand2")
        self.variable, self.command, self.color = variable, command, color
        self.bind("<Button-1>", self._click)
        variable.trace_add("write", lambda *_: self._draw())
        self._draw()

    def _click(self, _event: tk.Event) -> None:
        self.variable.set(not self.variable.get())
        self.command()

    def _draw(self) -> None:
        on = self.variable.get()
        width, height = int(self["width"]), int(self["height"])
        self.delete("all")
        round_rect(self, 0, 0, width, height, height / 2, fill=self.color if on else FIELD_HI, outline="")
        x, y, r = (width - height / 2 + 1 if on else height / 2 - 1), height / 2, px(7)
        self.create_oval(x - r, y - r, x + r, y + r, fill="#ffffff" if on else MUTED, outline="")


class Slider(tk.Canvas):
    """Horizontal slider on a DoubleVar; the filled part takes `color`.

    Drag or click to set, mouse wheel to step. `command` runs on user changes
    only; setting the variable from code just redraws.
    """

    def __init__(self, parent: tk.Misc, variable: tk.DoubleVar, low: float, high: float,
                 command: Callable[[], None], color: str = ACCENT, length: int = 200, bg: str = CARD) -> None:
        super().__init__(parent, width=px(length), height=px(24), bg=bg, highlightthickness=0, bd=0,
                         cursor="hand2")
        self.variable, self.low, self.high, self.command = variable, low, high, command
        self.margin = px(8)  # room for the knob at either end
        self.color = color
        self.step = 5 if high - low >= 100 else 1
        self.bind("<Button-1>", self._drag)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<MouseWheel>", self._wheel)
        variable.trace_add("write", lambda *_: self._draw())
        self._draw()

    def set_color(self, color: str) -> None:
        if color != self.color:
            self.color = color
            self._draw()

    def _put(self, value: float) -> None:
        value = round(min(self.high, max(self.low, value)))
        if value != round(self.variable.get()):
            self.variable.set(value)
            self.command()

    def _drag(self, event: tk.Event) -> None:
        span = int(self["width"]) - 2 * self.margin
        self._put(self.low + (event.x - self.margin) / span * (self.high - self.low))

    def _wheel(self, event: tk.Event) -> None:
        self._put(self.variable.get() + (self.step if event.delta > 0 else -self.step))

    def _draw(self) -> None:
        width, y, m, r = int(self["width"]), int(self["height"]) / 2, self.margin, px(7)
        fraction = (self.variable.get() - self.low) / (self.high - self.low)
        x = m + max(0.0, min(1.0, fraction)) * (width - 2 * m)
        self.delete("all")
        self.create_line(m, y, width - m, y, width=px(4), fill=FIELD_HI, capstyle="round")
        self.create_line(m, y, x, y, width=px(4), fill=self.color, capstyle="round")
        self.create_oval(x - r, y - r, x + r, y + r, fill="#ffffff", outline=self.color, width=px(2))


class Meter(tk.Canvas):
    """Thin level bar, 0..1."""

    def __init__(self, parent: tk.Misc, color: str, length: int = 200, bg: str = CARD) -> None:
        super().__init__(parent, width=px(length), height=px(10), bg=bg, highlightthickness=0, bd=0)
        self.color = color
        self.shown = -1
        self.set(0.0)

    def set(self, level: float) -> None:
        width, height = int(self["width"]), int(self["height"])
        x = round(max(0.0, min(1.0, level)) * width)
        if x == self.shown:
            return
        self.shown = x
        self.delete("all")
        round_rect(self, 0, 0, width, height, height / 2, fill=FIELD, outline="")
        if x >= px(4):
            round_rect(self, 0, 0, x, height, height / 2, fill=self.color, outline="")


class Tooltip:
    """Explanation shown in a small box once the pointer rests on a widget.

    Hidden again on leaving or clicking, and not shown while a button is
    held, so dragging a slider is not covered.
    """

    DELAY_MS = 500

    def __init__(self, widget: tk.Misc, text: str, wraplength: int = 340) -> None:
        self.widget, self.text, self.wraplength = widget, text, wraplength
        self.top: tk.Toplevel | None = None
        self.job: str | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")
        widget.bind("<Destroy>", self._hide, add="+")

    def _schedule(self, event: tk.Event) -> None:
        self._hide()
        if self.text and not int(event.state) & 0x700:  # Button1..3 held
            self.job = self.widget.after(self.DELAY_MS, self._show)

    def _show(self) -> None:
        self.job = None
        widget = self.widget
        self.top = top = tk.Toplevel(widget, bg=FIELD_HI)
        top.wm_overrideredirect(True)
        top.attributes("-topmost", True)
        tk.Label(top, text=self.text, bg=TILE, fg=TEXT, font=font(9), justify="left",
                 wraplength=px(self.wraplength), padx=px(9), pady=px(6)).pack(padx=1, pady=1)
        top.update_idletasks()
        x = widget.winfo_pointerx() + px(12)
        y = widget.winfo_rooty() + widget.winfo_height() + px(6)
        # keep it on the primary screen when the pointer is there
        if x < widget.winfo_screenwidth():
            x = min(x, widget.winfo_screenwidth() - top.winfo_reqwidth() - px(4))
        if y + top.winfo_reqheight() > widget.winfo_screenheight():
            y = widget.winfo_rooty() - top.winfo_reqheight() - px(6)
        top.geometry(f"+{x}+{y}")

    def _hide(self, _event: tk.Event | None = None) -> None:
        if self.job:
            self.widget.after_cancel(self.job)
            self.job = None
        if self.top:
            try:
                self.top.destroy()
            except tk.TclError:  # already gone with its widget
                pass
            self.top = None


def tip(widget: tk.Misc, text: str) -> tk.Misc:
    """Attach a tooltip; returns the widget so it can be laid out in one line."""
    Tooltip(widget, text)
    return widget


class Link(tk.Label):
    """Text that opens `command` on click, underlined under the pointer."""

    def __init__(self, parent: tk.Misc, text: str, command: Callable[[], None], size: int = 9,
                 bg: str = BG) -> None:
        super().__init__(parent, text=text, bg=bg, fg=MUTED, font=font(size), cursor="hand2")
        self.normal, self.hovered = font(size), font(size) + ("underline",)
        self.bind("<Enter>", lambda _: self.config(fg=ACCENT, font=self.hovered), add="+")
        self.bind("<Leave>", lambda _: self.config(fg=MUTED, font=self.normal), add="+")
        self.bind("<ButtonRelease-1>", lambda _: command())
