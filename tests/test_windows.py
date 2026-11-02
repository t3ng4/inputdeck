import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from inputdeck import core, windows  # noqa: E402
from inputdeck.windows import Window  # noqa: E402


class FakeXprop:
    """Answers `xprop` the way a window manager's EWMH properties would look."""

    def __init__(self):
        self.root = {"clients": [], "active": "0x0", "desktop": 0}
        self.win = {}            # id -> dict(pid, cls, title, state, desktop, wm_state)
        self.calls = []
        self.fail = False

    def add(self, wid, pid, cls, title="", state=(), desktop=0, wm_state="Normal"):
        self.win[wid] = dict(pid=pid, cls=cls, title=title, state=state, desktop=desktop, wm_state=wm_state)
        self.root["clients"].append(wid)

    def __call__(self, cmd, timeout=None):
        self.calls.append(cmd)
        if self.fail:
            return 1, "xprop: unable to open display"
        args = cmd[1:]
        if args[0] == "-root":
            ids = ", ".join(self.root["clients"])
            lines = []
            if self.root["clients"] is not None:
                lines.append(f"_NET_CLIENT_LIST(WINDOW): window id # {ids}")
            lines.append(f"_NET_ACTIVE_WINDOW(WINDOW): window id # {self.root['active']}")
            lines.append(f"_NET_CURRENT_DESKTOP(CARDINAL) = {self.root['desktop']}")
            return 0, "\n".join(lines) + "\n"
        w = self.win[args[1]]
        if "_NET_WM_PID" in args:
            classes = ", ".join(f'"{c}"' for c in w["cls"])
            return 0, (f"_NET_WM_PID(CARDINAL) = {w['pid']}\nWM_CLASS(STRING) = {classes}\n"
                       f'_NET_WM_NAME(UTF8_STRING) = "{w["title"]}"\n')
        state = ", ".join(w["state"])
        return 0, (f"_NET_WM_STATE(ATOM) = {state}\n_NET_WM_DESKTOP(CARDINAL) = {w['desktop']}\n"
                   f"WM_STATE(WM_STATE):\n\t\twindow state: {w['wm_state']}\n\t\ticon window: 0x0\n")


def provider(fake):
    return windows.XpropProvider(runner=fake, which=lambda _n: "/usr/bin/xprop", environ={"DISPLAY": ":0"})


class XpropProviderTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeXprop()
        self.fake.add("0x400001", 4242, ("blackdesert64.exe", "BlackDesert64.exe"), "BLACK DESERT")
        self.fake.add("0x400002", 900, ("konsole", "konsole"), "Terminal")
        self.fake.root["active"] = "0x400001"

    def test_reads_windows_and_focus(self):
        snap = provider(self.fake).snapshot()
        by_id = {w.id: w for w in snap}
        self.assertEqual(by_id["0x400001"].pid, 4242)
        self.assertEqual(by_id["0x400001"].classes, ("blackdesert64.exe", "BlackDesert64.exe"))
        self.assertEqual(by_id["0x400001"].title, "BLACK DESERT")
        self.assertTrue(by_id["0x400001"].active)
        self.assertFalse(by_id["0x400002"].active)
        self.assertFalse(by_id["0x400001"].minimized)

    def test_minimized_window(self):
        self.fake.win["0x400001"]["state"] = ("_NET_WM_STATE_HIDDEN", "_NET_WM_STATE_SKIP_TASKBAR")
        snap = {w.id: w for w in provider(self.fake).snapshot()}
        self.assertTrue(snap["0x400001"].minimized)
        self.assertFalse(snap["0x400002"].minimized)

    def test_iconic_wm_state_counts_as_minimized(self):
        self.fake.win["0x400001"]["wm_state"] = "Iconic"
        snap = {w.id: w for w in provider(self.fake).snapshot()}
        self.assertTrue(snap["0x400001"].minimized)

    def test_withdrawn_window_is_not_listed(self):
        self.fake.win["0x400001"]["wm_state"] = "Withdrawn"
        ids = [w.id for w in provider(self.fake).snapshot()]
        self.assertEqual(ids, ["0x400002"])

    def test_other_desktop(self):
        self.fake.win["0x400002"]["desktop"] = 1
        snap = {w.id: w for w in provider(self.fake).snapshot()}
        self.assertFalse(snap["0x400002"].on_current_desktop)
        self.assertTrue(snap["0x400001"].on_current_desktop)

    def test_sticky_window_is_on_every_desktop(self):
        self.fake.win["0x400002"]["desktop"] = 0xFFFFFFFF
        self.fake.root["desktop"] = 3
        snap = {w.id: w for w in provider(self.fake).snapshot()}
        self.assertTrue(snap["0x400002"].on_current_desktop)

    def test_only_interesting_windows_get_their_state_read(self):
        provider(self.fake).snapshot(lambda pid, classes, title: "blackdesert" in " ".join(classes).lower())
        state_queries = [c for c in self.fake.calls if "_NET_WM_STATE" in c]
        self.assertEqual(len(state_queries), 1)
        self.assertEqual(state_queries[0][2], "0x400001")

    def test_static_info_is_cached(self):
        p = provider(self.fake)
        p.snapshot()
        before = len([c for c in self.fake.calls if "_NET_WM_PID" in c])
        p.snapshot()
        self.assertEqual(len([c for c in self.fake.calls if "_NET_WM_PID" in c]), before)

    def test_unavailable_without_xprop_or_display(self):
        self.assertIsNone(windows.XpropProvider(runner=self.fake, which=lambda _n: None,
                                                environ={"DISPLAY": ":0"}).snapshot())
        self.assertIsNone(windows.XpropProvider(runner=self.fake, which=lambda _n: "/x",
                                                environ={}).snapshot())

    def test_xprop_failure_gives_none(self):
        self.fake.fail = True
        self.assertIsNone(provider(self.fake).snapshot())


class RuleFormatTests(unittest.TestCase):
    def test_old_three_field_rules_still_work(self):
        r = core.parse_rules("mygame|Pad|Game")[0]
        self.assertEqual((r.process, r.device, r.preset, r.when), ("mygame", "Pad", "Game", "running"))

    def test_fourth_field(self):
        rules = core.parse_rules("a|Pad|G|visible\nb|Pad|H|FOCUSED\nc|Pad|I|nonsense\nd|Pad|J|")
        self.assertEqual([r.when for r in rules], ["visible", "focused", "running", "running"])

    def test_roundtrip_keeps_saito_format_for_running_rules(self):
        rules = [core.Rule("a", "Pad", "G"), core.Rule("b", "Pad", "H", "visible"),
                 core.Rule("DEFAULT", "Pad", "D")]
        text = core.dump_rules(rules)
        body = [ln for ln in text.splitlines() if not ln.startswith("#")]
        self.assertEqual(body, ["a|Pad|G", "b|Pad|H|visible", "DEFAULT|Pad|D"])
        self.assertEqual(core.parse_rules(text), rules)


def win(**kw):
    base = dict(id="0x1", pid=0, classes=("blackdesert64.exe",), title="", minimized=False,
                on_current_desktop=True, active=False)
    base.update(kw)
    return Window(**base)


class DecideTests(unittest.TestCase):
    RULES = "blackdesert|Pad|Game|visible\nDEFAULT|Pad|Desktop"
    RUNNING = ["Z:\\games\\BlackDesert64.exe"]

    def decide(self, windows_, text=None, cmd=None):
        return core.decide(core.parse_rules(text or self.RULES), self.RUNNING, windows_,
                           cmdline_for=lambda pid: cmd or "")

    def test_running_rule_ignores_windows(self):
        self.assertEqual(self.decide([], "blackdesert|Pad|Game\nDEFAULT|Pad|Desktop"), {"Pad": "Game"})

    def test_visible_window_selects_the_game_preset(self):
        self.assertEqual(self.decide([win()]), {"Pad": "Game"})

    def test_tray_hidden_game_falls_back_to_desktop(self):
        # process runs but has no window at all
        self.assertEqual(self.decide([win(classes=("konsole",))]), {"Pad": "Desktop"})
        self.assertEqual(self.decide([]), {"Pad": "Desktop"})

    def test_minimized_game_falls_back_to_desktop(self):
        self.assertEqual(self.decide([win(minimized=True)]), {"Pad": "Desktop"})

    def test_game_on_another_desktop_falls_back(self):
        self.assertEqual(self.decide([win(on_current_desktop=False)]), {"Pad": "Desktop"})

    def test_focused_needs_the_active_window(self):
        text = "blackdesert|Pad|Game|focused\nDEFAULT|Pad|Desktop"
        self.assertEqual(self.decide([win()], text), {"Pad": "Desktop"})
        self.assertEqual(self.decide([win(active=True)], text), {"Pad": "Game"})
        self.assertEqual(self.decide([win(active=True, minimized=True)], text), {"Pad": "Desktop"})

    def test_matches_on_title_and_on_process_command_line(self):
        self.assertEqual(self.decide([win(classes=("x",), title="BlackDesert Online")]), {"Pad": "Game"})
        self.assertEqual(self.decide([win(classes=("x",), pid=77)], cmd="wine BlackDesert64.exe"), {"Pad": "Game"})

    def test_detection_unavailable_acts_like_running(self):
        self.assertEqual(self.decide(None), {"Pad": "Game"})
        self.assertEqual(core.decide(core.parse_rules(self.RULES), ["bash"], None), {"Pad": "Desktop"})

    def test_mixed_rules_first_match_wins(self):
        text = "blackdesert|Pad|Game|visible\nfirefox|Pad|Browser\nDEFAULT|Pad|Desktop"
        self.assertEqual(core.decide(core.parse_rules(text), ["firefox"], [win(minimized=True)]),
                         {"Pad": "Browser"})


class SwitcherWindowTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        paths = core.Paths(tmp / "ir", tmp / "cfg", tmp / "data")
        self.store = core.PresetStore(paths)
        for p in ("Desktop", "Game"):
            self.store.create("Pad", p)
        self.logs, self.calls, self.snapshot, self.asked = [], [], [], []

        class Ctl:
            last_warnings = []

            def start(_s, d, p):
                self.calls.append((d, p))
                return 0, ""

        self.rules = core.parse_rules("blackdesert|Pad|Game|visible\nDEFAULT|Pad|Desktop")

        def windows_fn(interesting):
            self.asked.append(interesting)
            return self.snapshot

        self.sw = core.Switcher(Ctl(), self.store, lambda: self.rules, lambda: ["BlackDesert64.exe"],
                                self.logs.append, windows=windows_fn)

    def test_follows_the_window_state(self):
        self.snapshot = [win()]
        self.sw.tick()
        self.snapshot = [win(minimized=True)]
        self.sw.tick()
        self.snapshot = []                        # minimized to tray: window is gone
        self.sw.tick()
        self.snapshot = [win()]
        self.sw.tick()
        self.assertEqual(self.calls, [("Pad", "Game"), ("Pad", "Desktop"), ("Pad", "Game")])

    def test_windows_are_not_scanned_when_no_rule_needs_them(self):
        self.rules = core.parse_rules("blackdesert|Pad|Game\nDEFAULT|Pad|Desktop")
        self.sw.tick()
        self.assertEqual(self.asked, [])

    def test_setting_off_acts_like_running(self):
        self.sw.use_windows = False
        self.snapshot = []
        self.sw.tick()
        self.assertEqual(self.calls, [("Pad", "Game")])
        self.assertEqual(self.asked, [])

    def test_unavailable_detection_is_reported_once(self):
        self.sw.windows = lambda interesting: None
        self.sw.tick()
        self.sw.tick()
        self.assertEqual(self.calls, [("Pad", "Game")])
        self.assertEqual(len([m for m in self.logs if "window detection is not available" in m]), 1)

    def test_interesting_filter_only_lets_rule_windows_through(self):
        self.sw.tick()
        interesting = self.asked[0]
        self.assertTrue(interesting(0, ("BlackDesert64.exe",), ""))
        self.assertTrue(interesting(0, ("x",), "BlackDesert launcher"))
        self.assertFalse(interesting(0, ("konsole",), "Terminal"))


if __name__ == "__main__":
    unittest.main()
