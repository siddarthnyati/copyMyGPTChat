#!/usr/bin/env python3
"""Export ChatGPT conversations from the macOS desktop app.

Usage:
    python export_chatgpt.py                # full run
    python export_chatgpt.py --dry-run      # just enumerate sidebar
    python export_chatgpt.py --limit 5      # export first 5 only
    python export_chatgpt.py --retry-failed # retry only previously failed
    python export_chatgpt.py --debug        # dump AX tree to .ax_dump.txt

Requires macOS Accessibility permission for your terminal/Python.
Do not touch mouse/keyboard during the run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Iterable

import pyautogui
from tqdm import tqdm

from ApplicationServices import (  # type: ignore
    AXIsProcessTrusted,
    AXUIElementCopyAttributeNames,
    AXUIElementCopyAttributeValue,
    AXUIElementCreateApplication,
    AXUIElementPerformAction,
    kAXChildrenAttribute,
    kAXFocusedWindowAttribute,
    kAXPositionAttribute,
    kAXPressAction,
    kAXRoleAttribute,
    kAXSizeAttribute,
    kAXSubroleAttribute,
    kAXTitleAttribute,
    kAXValueAttribute,
    kAXWindowsAttribute,
)
from Cocoa import NSWorkspace  # type: ignore

from Quartz import (  # type: ignore
    CGEventCreateMouseEvent,
    CGEventCreateScrollWheelEvent,
    CGEventPost,
    kCGEventLeftMouseDown,
    kCGEventLeftMouseUp,
    kCGEventMouseMoved,
    kCGHIDEventTap,
    kCGMouseButtonLeft,
    kCGScrollEventUnitLine,
)

# -------------------- Tunables --------------------
OUTPUT_DIR = Path.home() / "chatgpt_exports"
CHECKPOINT_PATH = OUTPUT_DIR / ".checkpoint.json"
LOG_PATH = OUTPUT_DIR / ".log"
AX_DUMP_PATH = OUTPUT_DIR / ".ax_dump.txt"

INTER_CONV_DELAY = 1.5
SCROLL_DELAY = 0.4
LOAD_TIMEOUT = 10.0
COPY_RETRY = 1
MAX_PAGE_UPS = 50
SIDEBAR_SCROLL_CHUNKS = 200
MIN_EXPORT_CHARS = 20  # minimum total message chars to accept an export

APP_BUNDLE_IDS = ("com.openai.chat",)
APP_NAME_FALLBACKS = ("ChatGPT",)

pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.05


# -------------------- Logging --------------------
def setup_logging() -> logging.Logger:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("chatgpt_export")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fh = logging.FileHandler(LOG_PATH)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(fh)
    return logger


log = logging.getLogger("chatgpt_export")


# -------------------- macOS helpers --------------------
def find_chatgpt_pid() -> int | None:
    ws = NSWorkspace.sharedWorkspace()
    for app in ws.runningApplications():
        bid = app.bundleIdentifier()
        name = app.localizedName()
        if bid and bid in APP_BUNDLE_IDS:
            return int(app.processIdentifier())
        if name and name in APP_NAME_FALLBACKS:
            return int(app.processIdentifier())
    return None


def activate_chatgpt(sleep: float = 0.6) -> None:
    """Raise ChatGPT.app to the front. Cheap when already frontmost —
    Apple Events returns immediately if no activation is needed."""
    subprocess.run(
        ["osascript", "-e", 'tell application "ChatGPT" to activate'],
        check=False,
        capture_output=True,
    )
    time.sleep(sleep)


def check_accessibility() -> bool:
    return bool(AXIsProcessTrusted())


# -------------------- Low-level input (bypasses pyautogui failsafe) --------------------
def cg_move(x: float, y: float) -> None:
    ev = CGEventCreateMouseEvent(None, kCGEventMouseMoved, (x, y), kCGMouseButtonLeft)
    CGEventPost(kCGHIDEventTap, ev)


def cg_scroll(lines: int) -> None:
    """Positive lines = scroll up (content moves down), negative = scroll down."""
    ev = CGEventCreateScrollWheelEvent(None, kCGScrollEventUnitLine, 1, lines)
    CGEventPost(kCGHIDEventTap, ev)


def cg_click(x: float, y: float) -> None:
    cg_move(x, y)
    time.sleep(0.02)
    down = CGEventCreateMouseEvent(None, kCGEventLeftMouseDown, (x, y), kCGMouseButtonLeft)
    up = CGEventCreateMouseEvent(None, kCGEventLeftMouseUp, (x, y), kCGMouseButtonLeft)
    CGEventPost(kCGHIDEventTap, down)
    time.sleep(0.03)
    CGEventPost(kCGHIDEventTap, up)


# -------------------- System Events keystrokes (route via responder chain) --------------------
def osa_cmd_key(letter: str) -> None:
    """Send Cmd+<letter> to the ChatGPT process. Uses menu/responder routing,
    so Cmd+A acts like Edit > Select All on the document root — unlike raw
    pyautogui keystrokes, which only select within the currently-focused node."""
    script = (
        'tell application "System Events" to tell process "ChatGPT" '
        f'to keystroke "{letter}" using command down'
    )
    subprocess.run(["osascript", "-e", script], check=False, capture_output=True)


def osa_key_code(code: int) -> None:
    script = (
        'tell application "System Events" to tell process "ChatGPT" '
        f"to key code {code}"
    )
    subprocess.run(["osascript", "-e", script], check=False, capture_output=True)


# macOS virtual key codes
KEY_HOME = 115
KEY_END = 119
KEY_PAGE_UP = 116
KEY_PAGE_DOWN = 121
KEY_ESCAPE = 53


# -------------------- AX helpers --------------------
def ax_attr(element, name: str):
    err, value = AXUIElementCopyAttributeValue(element, name, None)
    if err != 0:
        return None
    return value


def ax_attr_names(element) -> list[str]:
    err, names = AXUIElementCopyAttributeNames(element, None)
    if err != 0 or names is None:
        return []
    return list(names)


def ax_children(element) -> list:
    kids = ax_attr(element, kAXChildrenAttribute)
    return list(kids) if kids else []


def ax_role(element) -> str:
    return ax_attr(element, kAXRoleAttribute) or ""


def ax_subrole(element) -> str:
    return ax_attr(element, kAXSubroleAttribute) or ""


def ax_title(element) -> str:
    return ax_attr(element, kAXTitleAttribute) or ""


def ax_value(element):
    return ax_attr(element, kAXValueAttribute)


def ax_frame(element) -> tuple[float, float, float, float] | None:
    pos = ax_attr(element, kAXPositionAttribute)
    size = ax_attr(element, kAXSizeAttribute)
    if pos is None or size is None:
        return None
    try:
        # pyobjc returns AXValueRef; extract via repr parsing is brittle. Use
        # helper that converts through NSValue-like interface if available.
        # Fallback: parse descriptions.
        from ApplicationServices import (  # type: ignore
            AXValueGetValue,
            kAXValueCGPointType,
            kAXValueCGSizeType,
        )
        import objc  # type: ignore
        from CoreFoundation import CGPoint, CGSize  # type: ignore

        point = CGPoint()
        ok1 = AXValueGetValue(pos, kAXValueCGPointType, point)
        size_v = CGSize()
        ok2 = AXValueGetValue(size, kAXValueCGSizeType, size_v)
        if not (ok1 and ok2):
            return None
        return (float(point.x), float(point.y), float(size_v.width), float(size_v.height))
    except Exception:
        # Fallback by parsing the string form.
        try:
            import re as _re
            p = str(pos)
            s = str(size)
            px = float(_re.search(r"x:([-\d\.]+)", p).group(1))
            py = float(_re.search(r"y:([-\d\.]+)", p).group(1))
            sw = float(_re.search(r"w:([-\d\.]+)", s).group(1))
            sh = float(_re.search(r"h:([-\d\.]+)", s).group(1))
            return (px, py, sw, sh)
        except Exception:
            return None


def walk_ax(element, depth: int = 0, max_depth: int = 25) -> Iterable[tuple[int, object]]:
    yield depth, element
    if depth >= max_depth:
        return
    for child in ax_children(element):
        yield from walk_ax(child, depth + 1, max_depth)


_EXTRA_ATTRS = (
    "AXDescription",
    "AXHelp",
    "AXRoleDescription",
    "AXIdentifier",
    "AXLabel",
    "AXPlaceholderValue",
)


def _stringify(v) -> str:
    if v is None:
        return ""
    s = str(v)
    return s[:100].replace("\n", " ")


def dump_ax_tree(root, path: Path) -> None:
    lines = []
    for depth, el in walk_ax(root, max_depth=30):
        role = ax_role(el)
        sub = ax_subrole(el)
        title = ax_title(el)
        val = ax_value(el)
        val_s = _stringify(val) if isinstance(val, str) else ""
        frame = ax_frame(el)
        frame_s = f" @({frame[0]:.0f},{frame[1]:.0f} {frame[2]:.0f}x{frame[3]:.0f})" if frame else ""
        extras = []
        for attr in _EXTRA_ATTRS:
            v = ax_attr(el, attr)
            if v:
                s = _stringify(v)
                if s:
                    extras.append(f"{attr[2:].lower()}={s!r}")
        extras_s = (" " + " ".join(extras)) if extras else ""
        lines.append(
            f"{'  ' * depth}{role}{'/' + sub if sub else ''}{frame_s} "
            f"title={title!r} value={val_s!r}{extras_s}"
        )
    path.write_text("\n".join(lines))


# -------------------- Sidebar enumeration --------------------
# Buttons whose AXDescription matches one of these are UI chrome, not chats.
_SIDEBAR_SKIP_DESCS = {
    "ChatGPT",       # the app-title / new-chat button at the top
    "GPTs",
    "New project",
    "See less",
    "See more",
    "Library",
    "Sora",
}


class Conversation:
    """A sidebar row. Identity is the AXDescription of its button, which
    ChatGPT.app sets to the conversation title verbatim. If two rows share
    the same description, we fall back to ``rel_y`` to distinguish them.
    """

    __slots__ = ("rel_y", "ax_ref", "index", "title")

    def __init__(self, rel_y: int, ax_ref, index: int, title: str):
        self.rel_y = rel_y
        self.ax_ref = ax_ref
        self.index = index
        self.title = title

    @property
    def cid(self) -> str:
        raw = f"{self.title}@{self.rel_y}".encode()
        return hashlib.sha1(raw).hexdigest()[:16]


def find_sidebar_container(window) -> tuple[object, object] | tuple[None, None]:
    """Locate the sidebar's AXScrollArea and its inner AXList (collection).

    The sidebar is the leftmost narrow scroll area (width < ~300).
    """
    best_scroll = None
    best_x = 1e9
    for _, el in walk_ax(window, max_depth=25):
        if ax_role(el) != "AXScrollArea":
            continue
        f = ax_frame(el)
        if not f:
            continue
        x, y, w, h = f
        if w > 320:
            continue
        if x < best_x:
            best_x = x
            best_scroll = el
    if best_scroll is None:
        return None, None
    for _, el in walk_ax(best_scroll, max_depth=6):
        if ax_role(el) == "AXList":
            return best_scroll, el
    return best_scroll, None


def list_sidebar_buttons(
    collection_list,
    viewport: tuple[float, float, float, float] | None = None,
) -> list[tuple[int, object, tuple[float, float, float, float], str]]:
    """Return sidebar row buttons that represent *chats* as
    (rel_y, ax_ref, frame, title).

    rel_y = button.y - collection_list.y — stable under scrolling.
    title = AXDescription, which ChatGPT.app populates with the real
    conversation name.

    Filters applied, in order:
      1. Must be a shape-matching row button (215<=w<=245, 26<=h<=42).
      2. Must have a non-empty AXDescription.
      3. Description not in _SIDEBAR_SKIP_DESCS (chrome buttons).
      4. Row must sit below the "See less"/"See more" divider, if one is
         present in this scroll window — rows above it are GPTs/projects,
         not conversations. If no divider is present (already scrolled past
         it), all shape-matching rows are assumed to be chats.
      5. If ``viewport`` is given, the button's full rect must lie inside
         it (plus a 2px tolerance). SwiftUI virtualized lists sometimes
         expose off-screen rows via AX with stale frames; clicking those
         coordinates lands on empty space or on an unrelated UI element,
         which is exactly how the "same chat stays open" bug manifests.
         Requiring strict viewport containment filters those out.
    """
    if collection_list is None:
        return []
    list_frame = ax_frame(collection_list)
    list_y = list_frame[1] if list_frame else 0.0

    divider_rel_y: int | None = None  # rel_y of "See less"/"See more"
    candidates: list[tuple[int, object, tuple[float, float, float, float], str]] = []
    for _, el in walk_ax(collection_list, max_depth=10):
        if ax_role(el) != "AXButton":
            continue
        f = ax_frame(el)
        if not f:
            continue
        x, y, w, h = f
        desc = (ax_attr(el, "AXDescription") or "").strip()
        rel_y = int(round(y - list_y))
        if desc in ("See less", "See more"):
            divider_rel_y = rel_y
            continue
        if not (215 <= w <= 245 and 26 <= h <= 42):
            continue
        if not desc:
            continue
        if desc in _SIDEBAR_SKIP_DESCS:
            continue
        if viewport is not None:
            vx, vy, vw, vh = viewport
            if not (y >= vy - 2 and (y + h) <= (vy + vh) + 2
                    and x >= vx - 2 and (x + w) <= (vx + vw) + 2):
                continue
        candidates.append((rel_y, el, (x, y, w, h), desc))

    if divider_rel_y is not None:
        candidates = [c for c in candidates if c[0] > divider_rel_y]

    candidates.sort(key=lambda r: r[0])
    return candidates


def _scroll_sidebar(scroll_area_frame, amount: int) -> None:
    sx, sy, sw, sh = scroll_area_frame
    cg_move(sx + sw / 2, sy + sh / 2)
    time.sleep(0.03)
    cg_scroll(amount)
    time.sleep(0.15)


def enumerate_conversations(pid: int) -> list[Conversation]:
    app = AXUIElementCreateApplication(pid)
    window = ax_attr(app, kAXFocusedWindowAttribute)
    if window is None:
        wins = ax_attr(app, kAXWindowsAttribute)
        if wins:
            window = wins[0]
    if window is None:
        raise RuntimeError("No ChatGPT window found via Accessibility API.")

    scroll_area, collection = find_sidebar_container(window)
    if scroll_area is None:
        raise RuntimeError(
            "Could not locate the sidebar scroll area. "
            "Make sure ChatGPT.app is frontmost and the sidebar is expanded."
        )
    if collection is None:
        raise RuntimeError("Sidebar scroll area found, but no AXList child inside it.")

    sa_frame = ax_frame(scroll_area)
    if sa_frame is None:
        raise RuntimeError("Sidebar scroll area has no frame.")

    # Scroll sidebar to the very top first so rel_y values align with the
    # start of the collection.
    for _ in range(40):
        _scroll_sidebar(sa_frame, 20)

    collected: dict[int, Conversation] = {}
    order: list[int] = []
    stable_rounds = 0

    for _ in range(SIDEBAR_SCROLL_CHUNKS):
        buttons = list_sidebar_buttons(collection, viewport=sa_frame)
        new_in_round = 0
        for rel_y, el, _frame, title in buttons:
            if rel_y in collected:
                continue
            conv = Conversation(rel_y=rel_y, ax_ref=el, index=len(order), title=title)
            collected[rel_y] = conv
            order.append(rel_y)
            new_in_round += 1
        if new_in_round == 0:
            stable_rounds += 1
            if stable_rounds >= 3:
                break
        else:
            stable_rounds = 0
        _scroll_sidebar(sa_frame, -10)

    # Return back to the top so the first row we process is visible / pressable.
    for _ in range(40):
        _scroll_sidebar(sa_frame, 20)

    out = [collected[k] for k in sorted(order)]
    for i, c in enumerate(out):
        c.index = i
    return out


# -------------------- Message pane location --------------------
def _get_window(pid: int):
    """Return ChatGPT's *focused* window. This can be a modal dialog
    ('Edit project', 'Rename', 'Share', etc.) if one is open. Prefer
    ``_find_main_window`` when you want the sidebar/chat window."""
    app = AXUIElementCreateApplication(pid)
    window = ax_attr(app, kAXFocusedWindowAttribute)
    if window is None:
        wins = ax_attr(app, kAXWindowsAttribute)
        if wins:
            window = wins[0]
    return window


def _find_main_window(pid: int):
    """Return the main ChatGPT chat window — the one that contains the
    sidebar scroll area. Bypasses modal dialogs (e.g. 'Edit project')
    that would otherwise be picked up by ``kAXFocusedWindowAttribute``.

    When a modal is open and we sample the focused window's title, every
    signature reads as "win:Edit project" regardless of which chat is
    actually underneath. That makes the verifier think no click ever
    switches — because, from a focused-window perspective, the modal
    never goes away. Using the main window (whose title *does* update
    per chat) unblocks that.
    """
    app = AXUIElementCreateApplication(pid)
    wins = ax_attr(app, kAXWindowsAttribute) or []
    for w in wins:
        sa, _coll = find_sidebar_container(w)
        if sa is not None:
            return w
    # Fallback: whatever's focused, or the first window.
    focused = ax_attr(app, kAXFocusedWindowAttribute)
    if focused is not None:
        return focused
    return wins[0] if wins else None


# Window titles that almost always belong to modal dialogs layered
# over the main ChatGPT window. When the top window has one of these
# titles, the sidebar is unclickable until the modal is dismissed.
_MODAL_WINDOW_TITLES = {
    "Edit project",
    "New project",
    "Rename",
    "Share",
    "Share link",
    "Settings",
    "Delete",
    "Archive",
}


def _dismiss_modal_if_present(pid: int) -> bool:
    """If the focused window is a known modal dialog, press Escape to
    dismiss it. Returns True if a modal was seen and an Escape was
    attempted. Safe to call at startup and between rows.

    ChatGPT's sidebar buttons accept ``AXPress`` even while a modal is
    open (so the press reports err=0) but the press is a no-op — the
    main chat view never updates. Auto-dismissing the modal is the
    difference between 10/10 rows failing and 10/10 rows working.
    """
    window = _get_window(pid)
    if window is None:
        return False
    title = ax_attr(window, kAXTitleAttribute)
    if not isinstance(title, str):
        return False
    t = title.strip()
    if t in _MODAL_WINDOW_TITLES:
        log.warning(
            "Modal dialog '%s' is frontmost in ChatGPT.app; sending Escape "
            "to dismiss it so sidebar clicks can switch chats.", t,
        )
        osa_key_code(KEY_ESCAPE)
        time.sleep(0.4)
        # Second Escape in case the modal has nested focus (e.g. a
        # text field caught the first keypress rather than the dialog).
        osa_key_code(KEY_ESCAPE)
        time.sleep(0.4)
        return True
    return False


def find_message_pane(pid: int) -> tuple[
    tuple[float, float, float, float] | None, object | None, object | None
]:
    """Return (pane_frame, pane_scroll_area, message_collection_list).

    The message pane is the outer AXScrollArea to the right of the sidebar
    whose first descendant AXList/AXCollectionList is the message stream.
    The collection list contains AXGroup children, each with AXStaticText
    descendants carrying the rendered message text in AXDescription.
    """
    # Use the main chat window, not whichever is focused — avoids
    # returning None just because a modal dialog is currently topmost.
    window = _find_main_window(pid)
    if window is None:
        return None, None, None

    best: tuple[tuple[float, float, float, float], object, object] | None = None
    for _, el in walk_ax(window, max_depth=25):
        if ax_role(el) != "AXScrollArea":
            continue
        frame = ax_frame(el)
        if not frame:
            continue
        x, y, w, h = frame
        if x < 240 or y < 0 or w < 600 or h < 200 or h > 2000:
            continue
        # Require an inner AXList/AXCollectionList — that's the message stream.
        inner_list = None
        for _, child in walk_ax(el, max_depth=4):
            if ax_role(child) == "AXList":
                inner_list = child
                break
        if inner_list is None:
            continue
        if best is None or y < best[0][1]:
            best = ((x, y, w, h), el, inner_list)

    if best is None:
        return None, None, None
    return best


def find_message_pane_frame(pid: int) -> tuple[float, float, float, float] | None:
    """Legacy wrapper — returns only the frame."""
    frame, _, _ = find_message_pane(pid)
    return frame


# -------------------- AX-tree message extraction --------------------
# How many scroll wheel ticks per iteration when walking the virtualized list.
# Small steps ensure no message node is scrolled past without at least one
# capture pass while it's in the viewport.
_SCROLL_STEP = 3
# Max scroll iterations (safety cap).
_MAX_EXTRACT_SCROLLS = 240
# Stop after this many consecutive scrolls with no new text captured.
_EXTRACT_NO_NEW_LIMIT = 8
# Minimum AXDescription length to treat as a real message (skip tiny labels).
_MIN_TEXT_LEN = 4


def _collect_text_nodes(
    msg_list,
) -> list[tuple[float, float, float, float, str]]:
    """Return [(x, y, w, h, description), ...] for message content in the
    list. Captures:
      * Every AXStaticText with non-empty AXDescription (or AXValue fallback).
      * Groups of image AXButtons — emitted as a single placeholder
        "[N image attachment(s)]" entry positioned at the group's top-left,
        so attachments don't silently disappear between messages.
    """
    out: list[tuple[float, float, float, float, str]] = []
    # Count image-like buttons per parent group so we can emit one
    # placeholder per group (not one per button).
    image_groups: dict[int, list[tuple[float, float]]] = {}

    for _, el in walk_ax(msg_list, max_depth=12):
        role = ax_role(el)
        if role == "AXStaticText":
            desc = ax_attr(el, "AXDescription")
            if not desc:
                val = ax_attr(el, kAXValueAttribute)
                if isinstance(val, str) and val:
                    desc = val
                else:
                    continue
            desc = str(desc).strip()
            if len(desc) < _MIN_TEXT_LEN:
                continue
            f = ax_frame(el)
            if not f:
                continue
            out.append((f[0], f[1], f[2], f[3], desc))
        elif role == "AXButton":
            # Chat attachments: square thumbnails (~128x128), no description.
            f = ax_frame(el)
            if not f:
                continue
            _x, _y, w, h = f
            if 100 <= w <= 180 and 100 <= h <= 180:
                desc = ax_attr(el, "AXDescription") or ""
                if desc in ("Scroll to bottom",):
                    continue
                # Group by an identity key — we don't have the parent
                # element here, so approximate by banding the y-coord
                # (two rows of images in the same group share a y-band).
                band = int(_y // 160)
                image_groups.setdefault(band, []).append((_x, _y))

    for band, positions in image_groups.items():
        n = len(positions)
        if n == 0:
            continue
        xs = [p[0] for p in positions]
        ys = [p[1] for p in positions]
        # Position the placeholder at the top-left of the group.
        out.append((min(xs), min(ys), 0.0, 0.0, f"[{n} image attachment(s)]"))

    return out


def _classify(frame_x: float, pane_x: float, pane_w: float) -> str:
    """USER if the text's left edge sits noticeably right of the pane's
    left edge (right-aligned user bubble); ASSISTANT otherwise.

    From empirical AX dumps, assistant messages span the pane starting at
    pane_x (offset ~0), while user message bubbles are indented by well
    over 100px. 120px is a comfortable threshold.
    """
    return "USER" if (frame_x - pane_x) > 120 else "ASSISTANT"


def _coalesce_blocks(blocks: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Merge consecutive same-label blocks (a single message often fragments
    into multiple AXStaticText nodes — list items, code blocks, tables)."""
    out: list[tuple[str, str]] = []
    for label, body in blocks:
        if out and out[-1][0] == label:
            out[-1] = (label, out[-1][1] + "\n\n" + body)
        else:
            out.append((label, body))
    return out


class _Capture:
    """One captured text node. We track the earliest y-coordinate we saw
    it at (for ordering), the x-coordinate (for USER/ASSISTANT classification),
    and keep replacing ``desc`` whenever a longer variant is observed.
    """

    __slots__ = ("first_order", "first_y", "x", "desc")

    def __init__(self, order: int, y: float, x: float, desc: str) -> None:
        self.first_order = order
        self.first_y = y
        self.x = x
        self.desc = desc


def _merge_capture(store: list[_Capture], order: int, fx: float, fy: float, desc: str) -> bool:
    """Merge ``desc`` into ``store`` as a new capture or as an update to an
    existing one. Returns True if anything changed (new capture added or
    existing one extended).

    Rules:
      * If an existing capture's ``desc`` is a prefix of the new ``desc``
        (or vice versa), treat them as the same message and keep the longer.
      * Otherwise, if no overlap found, add as a new capture.
    """
    # Short descriptions match by equality only — long enough prefixes are
    # distinctive so a prefix match means it's the same message.
    PREFIX_MATCH_MIN = 40

    for c in store:
        # Exact duplicate — nothing to do.
        if c.desc == desc:
            return False
        short, long_ = (c.desc, desc) if len(c.desc) <= len(desc) else (desc, c.desc)
        if len(short) >= PREFIX_MATCH_MIN and long_.startswith(short):
            # Same message, one is a prefix of the other. Keep the longer.
            if len(desc) > len(c.desc):
                c.desc = desc
                # Keep original first_y / first_order so we don't re-order.
                return True
            return False
    store.append(_Capture(order, fy, fx, desc))
    return True


def _first_text_y(msg_list) -> float | None:
    """Return the screen-y of the topmost AXStaticText currently in the
    message list, or None if nothing's there. Used as a scroll-progress
    signal — if the y is unchanged across two key events, we've hit the
    top / bottom and further scrolling is a no-op."""
    ys: list[float] = []
    for _, el in walk_ax(msg_list, max_depth=12):
        if ax_role(el) != "AXStaticText":
            continue
        f = ax_frame(el)
        if f is None:
            continue
        ys.append(f[1])
    return min(ys) if ys else None


def extract_messages_from_ax(
    pane_frame: tuple[float, float, float, float],
    msg_list,
) -> list[tuple[str, str]]:
    """Walk the message pane top-to-bottom with keyboard navigation, capturing
    every AXStaticText's AXDescription (plus image-attachment placeholders)
    at each viewport position. Classify by x-offset, coalesce, return
    [(USER|ASSISTANT, body), ...].

    Design: do *not* early-exit on "no new text this scroll". Image
    attachments between messages create a transition window where the
    assistant-msg-above has unmounted, the user-msg-below has mounted
    buttons but not text yet, and our AXStaticText walker sees nothing
    new. Previously we bailed there. Now we just PageDown a fixed number
    of times regardless, and double-capture each step (immediate + after
    300ms) so the next group's text node has time to mount.

    Keyboard (PageDown / Home / End) over mouse wheel because:
      * wheel events are delivered to whatever window is under the cursor,
        which is fragile on multi-monitor setups;
      * PageDown routed via System Events goes to the ChatGPT responder
        chain once the pane is focused — reliable per-keystroke distance
        of ~one viewport.
    """
    px, py, pw, ph = pane_frame
    center_x = px + pw / 2
    center_y = py + ph / 2
    gutter_x = px + pw - 20  # near right edge, off any message bubble

    # Focus the pane (click right-gutter, off bubbles) and blur the
    # composer so PageDown targets the message list, not the input box.
    cg_click(gutter_x, center_y)
    time.sleep(0.20)
    osa_key_code(KEY_ESCAPE)
    time.sleep(0.12)

    # Jump to the very top.
    osa_key_code(KEY_HOME)
    time.sleep(0.9)

    store: list[_Capture] = []
    order_ref = [0]  # mutable, shared with _capture

    def _capture(tag: str) -> int:
        nodes = _collect_text_nodes(msg_list)
        added = 0
        for fx, fy, fw, fh, desc in nodes:
            if _merge_capture(store, order_ref[0], fx, fy, desc):
                order_ref[0] += 1
                added += 1
        total_chars = sum(len(c.desc) for c in store)
        top = _first_text_y(msg_list)
        log.info(
            "Extract %-18s ax_nodes=%3d captures=%3d chars=%6d (+%d) top_y=%s",
            tag, len(nodes), len(store), total_chars, added,
            f"{top:.0f}" if top is not None else "?",
        )
        return added

    # Initial top-of-conversation capture.
    _capture("home")
    time.sleep(0.3)
    _capture("home(settled)")

    # Fixed-count scroll loop. 50 PageDowns at ~1 viewport each covers
    # >40,000px — more than 4x the largest observed conversation height.
    # We do not early-exit on quiet transition windows (image groups).
    MAX_PAGE_DOWNS = 50
    # Only early-exit when scroll position *and* char count have been
    # unchanged for this many consecutive steps — a strict bottom signal.
    STRICT_EXIT_QUIET = 6

    prev_top_y: float | None = None
    stagnant = 0
    prev_total_chars = 0

    for step in range(MAX_PAGE_DOWNS):
        osa_key_code(KEY_PAGE_DOWN)
        time.sleep(0.45)
        _capture(f"pgdn#{step + 1:02d}")
        # Second capture after a short settle — lets virtualization mount
        # the group that just scrolled into view.
        time.sleep(0.35)
        _capture(f"pgdn#{step + 1:02d}(s)")

        top_y = _first_text_y(msg_list)
        total_chars = sum(len(c.desc) for c in store)
        same_pos = (
            prev_top_y is not None and top_y is not None
            and abs(top_y - prev_top_y) < 2
        )
        same_content = total_chars == prev_total_chars
        if same_pos and same_content:
            stagnant += 1
        else:
            stagnant = 0
        prev_top_y = top_y
        prev_total_chars = total_chars

        # Require both position stuck AND content stuck, AND a minimum
        # number of scrolls done, before declaring bottom reached.
        if stagnant >= STRICT_EXIT_QUIET and step >= 8:
            log.info("Extract: bottom reached after %d pagedowns (stagnant=%d)",
                     step + 1, stagnant)
            break

    # End-key finale to guarantee the bottom is in-view at least once.
    osa_key_code(KEY_END)
    time.sleep(0.8)
    _capture("end")
    time.sleep(0.4)
    _capture("end(settled)")

    total_chars = sum(len(c.desc) for c in store)
    log.info("Extract done: captures=%d total_chars=%d", len(store), total_chars)

    # Sort by first-seen y so messages appear in conversation order.
    store.sort(key=lambda c: c.first_y)
    blocks: list[tuple[str, str]] = []
    for c in store:
        label = _classify(c.x, px, pw)
        blocks.append((label, c.desc))

    return _coalesce_blocks(blocks)


# Legacy hooks kept for call-site compatibility (unused now).
def load_all_messages(pane):  # pragma: no cover
    return


def open_conversation(conv: Conversation) -> bool:
    """Open the sidebar row (legacy — prefer ``click_live_row``).

    We prefer a real mouse click on the row center over AXPress because
    ChatGPT.app's SwiftUI button handlers do not always fire on AXPress —
    the cursor visibly stays put and the pane never switches. A genuine
    CGEvent click is indistinguishable from user input and reliably
    triggers the navigation.
    """
    frame = ax_frame(conv.ax_ref)
    if frame:
        x, y, w, h = frame
        cg_click(x + w / 2, y + h / 2)
        return True
    # Fallback if the frame disappeared (row scrolled out): try AXPress.
    err = AXUIElementPerformAction(conv.ax_ref, kAXPressAction)
    return err == 0


def click_live_row(frame: tuple[float, float, float, float]) -> None:
    """Click the center of a *freshly-queried* sidebar row frame.

    Takes the frame tuple directly (not an AXUIElementRef) so the caller is
    forced to re-query rows from the live AX tree right before clicking.
    This avoids the stale-ref trap: ChatGPT.app's sidebar is virtualized,
    so an AXUIElementRef captured at enumeration time goes dead when its
    row scrolls out of the viewport, and the frame it reports becomes
    stale — resulting in clicks landing on empty space (or worse, on
    whichever unrelated row currently occupies those screen coords).
    """
    x, y, w, h = frame
    cg_click(x + w / 2, y + h / 2)


LOAD_WAIT = 1.4  # fixed wait after clicking a sidebar row.


def wait_for_load(_prev_signature: str, _pid: int) -> str:
    time.sleep(LOAD_WAIT)
    return ""


def slugify(title: str, max_words: int = 6, max_len: int = 60) -> str:
    words = re.findall(r"[A-Za-z0-9]+", title)[:max_words]
    slug = "-".join(w.lower() for w in words) if words else "untitled"
    return slug[:max_len] or "untitled"


def unique_path(base: Path) -> Path:
    if not base.exists():
        return base
    stem, suffix = base.stem, base.suffix
    parent = base.parent
    for i in range(2, 1000):
        candidate = parent / f"{stem}-{i}{suffix}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Could not allocate unique filename for {base}")


def write_outputs(conv: Conversation, blocks: list[tuple[str, str]]) -> tuple[Path, Path]:
    today = datetime.now().strftime("%Y-%m-%d")
    title = conv.title or "untitled"
    slug = slugify(title)
    base_md = OUTPUT_DIR / f"{today}_{slug}.md"
    base_md = unique_path(base_md)
    base_txt = base_md.with_suffix(".txt")

    header_lines = [
        f"Title: {title}",
        f"Sidebar index: {conv.index + 1}",
        f"Exported: {datetime.now().isoformat(timespec='seconds')}",
    ]

    # Markdown
    md_parts = ["---"]
    md_parts += header_lines
    md_parts.append("---\n")
    for label, body in blocks:
        md_parts.append(f"**{label}:**\n\n{body}\n")
    base_md.write_text("\n".join(md_parts))

    # Plain text
    txt_parts = header_lines + [""]
    for label, body in blocks:
        txt_parts.append(f"{label}:")
        txt_parts.append(body)
        txt_parts.append("")
    base_txt.write_text("\n".join(txt_parts))

    return base_md, base_txt


# -------------------- Checkpoint --------------------
def load_checkpoint() -> dict:
    if CHECKPOINT_PATH.exists():
        try:
            return json.loads(CHECKPOINT_PATH.read_text())
        except Exception:
            log.warning("Checkpoint file corrupted; starting fresh.")
    return {"done": [], "failed": [], "started_at": datetime.now().isoformat()}


def save_checkpoint(state: dict) -> None:
    state["last_update"] = datetime.now().isoformat()
    CHECKPOINT_PATH.write_text(json.dumps(state, indent=2))


# -------------------- Sidebar-walking processor --------------------
# How many "scroll sidebar down, find nothing new" rounds to tolerate
# before declaring the bottom of the list reached. Each round scrolls
# by SIDEBAR_STEP_LINES and waits for virtualization to catch up.
_SIDEBAR_STUCK_ROUNDS = 10
# Lines per sidebar scroll-down step. Small enough that a row doesn't
# jump from "visible and unseen" to "scrolled past and unmounted" in a
# single tick.
SIDEBAR_STEP_LINES = 4

# How many characters from the top of the message pane to hash when
# building a fingerprint. Long enough to differ across chats, short
# enough that partially-loaded renders still match their full version.
_PANE_SIG_LEN = 400

# Max seconds to wait for the pane's signature to change after a click.
_CLICK_VERIFY_TIMEOUT = 3.5
# Sleep between poll attempts while verifying the click landed.
_CLICK_VERIFY_POLL = 0.25

# How many first-800-char hashes of already-written content to remember.
_DEDUPE_HASH_LEN = 800


def _sidebar_scroll_to_top(sa_frame) -> None:
    for _ in range(50):
        _scroll_sidebar(sa_frame, 20)
    time.sleep(0.4)


_GENERIC_WINDOW_TITLES = {"", "ChatGPT", "New chat"}


def _pane_signature(pid: int) -> str:
    """Identify the currently-open chat with a fingerprint that is
    invariant under message-pane scroll position.

    Why scroll-invariance matters: ``extract_messages_from_ax`` PageDowns
    through the chat to the bottom. If our signature includes the
    y-coordinates or the prefix-text of currently-mounted nodes, the
    "before next click" snapshot will differ from the "after click but
    click missed" snapshot — because both samples observe the same
    chat at different scroll positions. Verifier then falsely reports
    a switch and the wrong content gets written under the new title.

    Strategy:
      1. Prefer the *window* AXTitle. ChatGPT.app sets this to the
         conversation title and updates it when a different chat is
         opened — independent of scroll position.
      2. Fall back to a sorted-set hash of all currently-mounted
         AXStaticText descriptions (sorted, so order-of-mounting drift
         doesn't matter). For two different chats these sets are
         disjoint; for the same chat at different scroll positions they
         overlap heavily but won't necessarily match — hence (1) is
         strongly preferred.
      3. Fall back to the empty string only if everything else fails.
    """
    # Prefer the main chat window over whatever is focused — a modal
    # dialog (e.g. "Edit project") would otherwise poison every sig
    # with its own static title.
    window = _find_main_window(pid)
    if window is not None:
        title = ax_attr(window, kAXTitleAttribute)
        if isinstance(title, str):
            t = title.strip()
            if t and t not in _GENERIC_WINDOW_TITLES:
                return f"win:{t}"
            log.debug("pane_sig: main-window title is generic (%r); using content fallback", t)

    _pane_frame, _pane_sa, msg_list = find_message_pane(pid)
    if msg_list is None:
        return "(no-pane)"
    descs: list[str] = []
    for _, el in walk_ax(msg_list, max_depth=12):
        if ax_role(el) != "AXStaticText":
            continue
        d = ax_attr(el, "AXDescription")
        if not d:
            v = ax_attr(el, kAXValueAttribute)
            d = v if isinstance(v, str) else None
        if not d:
            continue
        s = str(d).strip()
        if len(s) >= 4:
            descs.append(s[:80])
        if len(descs) >= 8:
            break
    descs.sort()
    if not descs:
        return "(empty)"
    return "fb:" + hashlib.sha1("||".join(descs).encode()).hexdigest()[:16]


def _verify_click_switched(
    pid: int, prev_sig: str, deadline: float
) -> tuple[bool, str]:
    """Poll the pane signature until it differs from ``prev_sig`` or we
    run past the deadline. Returns (switched, new_sig).

    Considers a switch successful only if the new sig is non-empty,
    differs from prev_sig, AND is not a transient empty/loading state.
    A bare empty/no-pane sig isn't enough — we need positive evidence
    that a real new chat is on screen.
    """
    new_sig = prev_sig
    while time.time() < deadline:
        new_sig = _pane_signature(pid)
        if (
            new_sig
            and new_sig != prev_sig
            and new_sig not in ("(empty)", "(no-pane)", "(no-list)")
        ):
            return True, new_sig
        time.sleep(_CLICK_VERIFY_POLL)
    return False, new_sig


def _blocks_fingerprint(blocks: list[tuple[str, str]]) -> str:
    """SHA of the first _DEDUPE_HASH_LEN chars of the combined body —
    used to detect the "same chat got extracted again" failure mode."""
    buf: list[str] = []
    total = 0
    for _label, body in blocks:
        buf.append(body)
        total += len(body)
        if total >= _DEDUPE_HASH_LEN:
            break
    joined = "\n".join(buf)[:_DEDUPE_HASH_LEN]
    return hashlib.sha1(joined.encode()).hexdigest()[:16]


def _click_row_verified(
    pid: int,
    frame: tuple[float, float, float, float],
    ax_ref,
    title: str,
    prev_sig: str,
) -> tuple[bool, str]:
    """Press the row and confirm the message pane actually changed.

    Strategy, in order (fastest and most reliable first):
      1. ``AXUIElementPerformAction(ax_ref, kAXPressAction)``. This fires
         the button's press handler directly inside the ChatGPT process
         — no cursor, no focus, no CGEventTap. It works even if another
         app is frontmost or if the cursor was just nudged onto a
         secondary display.
      2. Raise ChatGPT to the front, then a plain ``cg_click`` on the
         frame center. Activation guarantees the click is delivered to
         the sidebar rather than to whatever window happens to be under
         the cursor.
      3. Cursor-jitter + ``cg_click``. Some SwiftUI button hit-tests
         ignore a click when the cursor was already inside the button's
         rect at click-down; nudging forces a fresh enter/exit cycle.

    After each attempt we poll ``_pane_signature`` for up to
    _CLICK_VERIFY_TIMEOUT seconds and consider the row opened only if
    the sig changes to a non-empty value.
    """
    x, y, w, h = frame
    cx, cy = x + w / 2, y + h / 2

    # Attempt 1: AXPress. Focus-independent, cursor-independent.
    err = AXUIElementPerformAction(ax_ref, kAXPressAction)
    log.info("  AXPress '%s' err=%s", title, err)
    if err == 0:
        switched, sig = _verify_click_switched(
            pid, prev_sig, time.time() + _CLICK_VERIFY_TIMEOUT
        )
        if switched:
            log.info("  switched via AXPress -> sig=%s", sig)
            return True, sig

    # Attempt 2: re-activate ChatGPT, then plain click.
    log.warning("  AXPress did not switch '%s'; activating ChatGPT + cg_click", title)
    activate_chatgpt()
    time.sleep(0.15)
    cg_click(cx, cy)
    time.sleep(0.25)
    switched, sig = _verify_click_switched(
        pid, prev_sig, time.time() + _CLICK_VERIFY_TIMEOUT
    )
    if switched:
        log.info("  switched via cg_click -> sig=%s", sig)
        return True, sig

    # Attempt 3: cursor jitter + click.
    log.warning("  cg_click did not switch '%s'; jitter + retry", title)
    cg_move(cx - 8, cy - 3)
    time.sleep(0.08)
    cg_move(cx, cy)
    time.sleep(0.05)
    cg_click(cx, cy)
    switched, sig = _verify_click_switched(
        pid, prev_sig, time.time() + _CLICK_VERIFY_TIMEOUT
    )
    if switched:
        log.info("  switched via jitter+click -> sig=%s", sig)
        return True, sig

    log.error(
        "  ALL three attempts failed for '%s'. prev_sig=%s final_sig=%s "
        "ChatGPT likely not frontmost or the sidebar row AXRef is dead.",
        title, prev_sig, sig,
    )
    return False, sig


def _process_one_row(
    pid: int,
    rel_y: int,
    frame: tuple[float, float, float, float],
    ax_ref,
    title: str,
    index: int,
    state: dict,
    delay: float,
    prev_pane_sig: str,
    content_hashes: set[str],
) -> tuple[bool, str, str]:
    """Click + verify + extract + dedupe-check + write.

    Returns (ok, message, new_pane_sig). ``content_hashes`` is mutated
    with the fingerprint of the written content on success; on a dedupe
    hit we mark the row failed so the bug is immediately visible in the
    checkpoint file.
    """
    conv = Conversation(rel_y=rel_y, ax_ref=None, index=index, title=title)
    cid = conv.cid

    try:
        switched, new_sig = _click_row_verified(pid, frame, ax_ref, title, prev_pane_sig)
        if not switched:
            raise RuntimeError(
                f"Click did not switch chats (pane signature unchanged after 3 attempts). "
                f"Row frame=({int(frame[0])},{int(frame[1])} {int(frame[2])}x{int(frame[3])})"
            )

        pane_frame, _pane_sa, msg_list = find_message_pane(pid)
        if pane_frame is None or msg_list is None:
            raise RuntimeError("Could not locate message pane / collection list")

        log.info(
            "#%d '%s' (rel_y=%d) pane=(x=%d, y=%d, w=%d, h=%d) sig=%s",
            index + 1, title, rel_y,
            int(pane_frame[0]), int(pane_frame[1]),
            int(pane_frame[2]), int(pane_frame[3]),
            new_sig,
        )

        blocks = extract_messages_from_ax(pane_frame, msg_list)
        total_chars = sum(len(b) for _, b in blocks)
        if not blocks or total_chars < MIN_EXPORT_CHARS:
            raise RuntimeError(
                f"No messages extracted (blocks={len(blocks)}, chars={total_chars})"
            )

        # Dedupe guard: the extracted content's first-800-char hash must
        # not match any previously-written file from this run. If it
        # does, the click almost certainly re-read the previous chat.
        fp = _blocks_fingerprint(blocks)
        if fp in content_hashes:
            raise RuntimeError(
                f"Content fingerprint {fp} duplicates an already-written file — "
                f"click probably didn't switch. Refusing to write '{title}'."
            )

        md_path, _txt_path = write_outputs(conv, blocks)
        content_hashes.add(fp)
        log.info(
            "Saved #%d '%s' (%d blocks, %d chars, fp=%s) -> %s",
            index + 1, title, len(blocks), total_chars, fp, md_path.name,
        )
        state.setdefault("done", []).append(cid)
        save_checkpoint(state)
        time.sleep(delay)
        return True, md_path.name, new_sig
    except Exception as e:
        log.exception("Failed #%d '%s' (cid=%s)", index + 1, title, cid)
        state.setdefault("failed", []).append(
            {"id": cid, "index": index + 1, "title": title, "error": str(e)}
        )
        save_checkpoint(state)
        time.sleep(delay)
        # Return the most-recent signature we have so the caller's
        # prev_sig stays accurate for the next verification.
        return False, str(e), prev_pane_sig


def walk_and_export(pid: int, state: dict, args: argparse.Namespace) -> int:
    """Walk the sidebar top-to-bottom, clicking each unseen row with
    freshly-queried coordinates. Processes rows in visible-first order.

    Invariants that make this robust:
      1. Every click target is read from a *fresh* ``list_sidebar_buttons``
         call filtered to the visible sidebar viewport — so the frame is
         always live AND the row is actually on-screen (no stale
         virtualized entries).
      2. Identity is ``(title, rel_y)`` — stable across scroll position
         because ``rel_y`` is measured relative to the collection list's
         own origin, not the viewport.
      3. Every click is verified: we compare the pane signature before
         and after, retry up to 3 times, and mark the row failed if the
         pane never changed.
      4. Every written file's first-800-char hash is remembered and
         used to reject any later extraction that duplicates it.
    """
    app = AXUIElementCreateApplication(pid)
    window = ax_attr(app, kAXFocusedWindowAttribute)
    if window is None:
        wins = ax_attr(app, kAXWindowsAttribute)
        window = wins[0] if wins else None
    if window is None:
        raise RuntimeError("No ChatGPT window found.")

    scroll_area, collection = find_sidebar_container(window)
    if scroll_area is None or collection is None:
        raise RuntimeError("Could not locate sidebar scroll area / collection list.")
    sa_frame = ax_frame(scroll_area)
    if sa_frame is None:
        raise RuntimeError("Sidebar scroll area has no frame.")

    done_ids: set[str] = set(state.get("done", []))
    only_failed = bool(args.retry_failed)
    failed_ids: set[str] = getattr(args, "_retry_cids", set()) if only_failed else set()

    # Dismiss any modal dialog ('Edit project' etc.) before enumerating.
    # If one is frontmost, sidebar AXPress returns err=0 but the press
    # is silently ignored — every row would read as failed.
    _dismiss_modal_if_present(pid)
    activate_chatgpt(sleep=0.3)

    _sidebar_scroll_to_top(sa_frame)

    visited: set[tuple[str, int]] = set()
    content_hashes: set[str] = set()
    processed_count = 0
    limit = args.limit if args.limit else 10**9
    # Tracks how many rows in a row have failed verification. If this
    # crosses a threshold, it's almost always a modal that re-appeared
    # mid-run (e.g. a confirmation dialog) — dismiss and continue.
    consecutive_failures = 0

    # Seed the pane signature from whatever chat is open right now.
    current_sig = _pane_signature(pid)
    log.info("Initial pane signature: %s", current_sig)

    stuck = 0
    progress = tqdm(unit="conv")

    while stuck < _SIDEBAR_STUCK_ROUNDS and processed_count < limit:
        # Re-read the sidebar scroll-area frame each iteration — in case
        # the window was resized mid-run, viewport filtering stays honest.
        live_sa_frame = ax_frame(scroll_area) or sa_frame
        buttons = list_sidebar_buttons(collection, viewport=live_sa_frame)
        unseen = [b for b in buttons if (b[3], b[0]) not in visited]

        if not unseen:
            _scroll_sidebar(sa_frame, -SIDEBAR_STEP_LINES)
            time.sleep(0.3)
            stuck += 1
            continue

        rel_y, ax_ref, frame, title = unseen[0]
        key = (title, rel_y)
        visited.add(key)

        cid = hashlib.sha1(f"{title}@{rel_y}".encode()).hexdigest()[:16]
        if only_failed:
            if cid not in failed_ids:
                stuck = 0
                continue
        else:
            if cid in done_ids:
                stuck = 0
                continue

        # Cheap re-activation — guarantees ChatGPT is frontmost so the
        # click goes to it rather than to whatever window happens to be
        # under the cursor. Apple Events no-ops when already frontmost.
        activate_chatgpt(sleep=0.1)

        # Resample sig RIGHT NOW so the verifier compares against the
        # actual current state of the pane (not the post-click sig from
        # the previous iteration, which is stale because extract scrolled
        # to the bottom of that conversation in between).
        live_sig = _pane_signature(pid)
        log.info(
            "--> Click row #%d '%s' rel_y=%d frame=(%d,%d %dx%d) live_sig=%s",
            processed_count + 1, title, rel_y,
            int(frame[0]), int(frame[1]), int(frame[2]), int(frame[3]),
            live_sig,
        )

        ok, msg, current_sig = _process_one_row(
            pid, rel_y, frame, ax_ref, title,
            processed_count, state, args.delay,
            live_sig, content_hashes,
        )
        processed_count += 1
        progress.update(1)
        progress.set_description_str(f"{'ok ' if ok else 'ERR'} {title[:40]}")

        if ok:
            consecutive_failures = 0
        else:
            consecutive_failures += 1
            # Two failures in a row is a strong signal a modal popped
            # up mid-run. Escape it and continue; costs nothing when
            # no modal is present.
            if consecutive_failures >= 2:
                log.warning(
                    "%d consecutive failures — checking for a modal and "
                    "re-activating ChatGPT.", consecutive_failures,
                )
                _dismiss_modal_if_present(pid)
                activate_chatgpt(sleep=0.2)

        # Spot-check every 5 processed rows. The bug we're guarding
        # against: every file ends up with the SAME content because
        # clicks aren't switching chats. Symptom: dedupe guard rejects
        # them (so failed grows), or — worse — sigs falsely report
        # switches and writes go through with stale content.
        #
        # Hard sanity gate: if after the first 5 successful writes we
        # have fewer than 4 unique content hashes, the run is broken.
        # Abort instead of polluting the export folder with duplicates.
        if processed_count % 5 == 0 and processed_count > 0:
            n_done = len(state.get("done", []))
            n_failed = len(state.get("failed", []))
            n_unique = len(content_hashes)
            log.info(
                "Spot check @ %d: done=%d failed=%d unique_content=%d",
                processed_count, n_done, n_failed, n_unique,
            )
            print(
                f"[spot check] {processed_count} processed, "
                f"{n_unique} unique content, "
                f"{n_failed} failed",
                flush=True,
            )
            # n_done counts successfully-written files (dedupe-passed).
            # If we have >= 5 done but unique hashes <= 1, every file
            # is identical — bail out.
            if n_done >= 5 and n_unique <= max(1, n_done - 4):
                msg = (
                    f"ABORTING: {n_done} files written but only {n_unique} "
                    f"unique content hash(es). Click verification is being "
                    f"fooled. Inspect ~/chatgpt_exports/.log and report."
                )
                log.error(msg)
                print("\n" + msg, flush=True)
                progress.close()
                return 1
        stuck = 0

    progress.close()

    n_done = len(state.get("done", []))
    n_failed = len(state.get("failed", []))
    print(f"Walk complete: visited {processed_count} new row(s); "
          f"{n_done} total done, {n_failed} failed, "
          f"{len(content_hashes)} unique content hashes.")
    log.info(
        "Walk complete: processed=%d done_total=%d failed_total=%d unique_hashes=%d",
        processed_count, n_done, n_failed, len(content_hashes),
    )
    return 0


# -------------------- Main --------------------
def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Export ChatGPT conversations from macOS app.")
    ap.add_argument("--dry-run", action="store_true", help="Enumerate only; don't open or save.")
    ap.add_argument("--limit", type=int, default=0, help="Export at most N conversations.")
    ap.add_argument("--retry-failed", action="store_true", help="Only retry previously failed ones.")
    ap.add_argument("--debug", action="store_true", help="Dump AX tree to .ax_dump.txt.")
    ap.add_argument("--delay", type=float, default=INTER_CONV_DELAY,
                    help="Seconds between conversations.")
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    global log
    log = setup_logging()

    if not check_accessibility():
        print(
            "ERROR: macOS Accessibility permission is not granted.\n"
            "Grant it under System Settings -> Privacy & Security -> Accessibility.\n"
            "Add the terminal you are running this script from (Terminal.app, iTerm, etc.)\n"
            "and/or Python itself, then re-run.",
            file=sys.stderr,
        )
        return 2

    activate_chatgpt()
    pid = find_chatgpt_pid()
    if pid is None:
        print("ERROR: ChatGPT app is not running. Open ChatGPT.app and log in first.",
              file=sys.stderr)
        return 2

    if args.debug:
        app = AXUIElementCreateApplication(pid)
        window = ax_attr(app, kAXFocusedWindowAttribute)
        if window is None:
            wins = ax_attr(app, kAXWindowsAttribute)
            window = wins[0] if wins else None
        if window is not None:
            dump_ax_tree(window, AX_DUMP_PATH)
            print(f"AX tree dumped to {AX_DUMP_PATH}")

    # --dry-run keeps the old enumerate-only path: it scrolls the sidebar
    # top-to-bottom once and prints every row it finds. The live export
    # path uses walk_and_export instead — which re-queries rows with
    # fresh frames between clicks to sidestep stale AXUIElementRefs.
    if args.dry_run:
        print("Enumerating sidebar conversations (dry run)...")
        convs = enumerate_conversations(pid)
        print(f"Found {len(convs)} conversations.")
        log.info("Enumerated %d conversations (dry run)", len(convs))
        for c in convs:
            print(f"  #{c.index + 1:>4}  rel_y={c.rel_y:>5}  cid={c.cid}  {c.title}")
        return 0

    state = load_checkpoint()

    if args.retry_failed:
        # Snapshot the failed-cid set onto args so the walker can filter
        # by it, then clear state['failed'] — any row that fails again
        # this run will be re-appended; any row that succeeds drops off.
        args._retry_cids = {f["id"] for f in state.get("failed", [])}  # type: ignore[attr-defined]
        print(f"Retrying {len(args._retry_cids)} previously failed conversation(s).")  # type: ignore[attr-defined]
        state["failed"] = []
    else:
        print(f"Exporting all unfinished conversations to {OUTPUT_DIR}")

    print("Do NOT touch mouse or keyboard. Move cursor to a screen corner to abort (failsafe).")
    time.sleep(2.0)

    try:
        walk_and_export(pid, state, args)
    except KeyboardInterrupt:
        save_checkpoint(state)
        print("\nInterrupted. Re-run the script to resume from checkpoint.")
        return 130
    except pyautogui.FailSafeException:
        save_checkpoint(state)
        print("\nFailsafe triggered (mouse in corner). Checkpoint saved.")
        return 130

    save_checkpoint(state)
    n_done = len(state.get("done", []))
    n_failed = len(state.get("failed", []))
    print(f"Done. {n_done} exported, {n_failed} failed. See {LOG_PATH} for details.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
