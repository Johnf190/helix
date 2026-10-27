"""ORDER 59 — Claude in Chrome for Odin: `--chrome` on the CHAT and WORK lanes only.

In-process checks of _lane_options. No server, no SDK connection, no model calls, no browser.

Run:  .venv/bin/python -m pytest test_chrome_flag.py -q
"""
import os, sys, tempfile, unittest

# Same guards as test_front_lane.py: import app without booting a warmup turn, a persistent
# SDK pre-connect, the scheduler ticker or the studio worker.
os.environ["HELIX_NO_WARMUP"] = "1"
os.environ["HELIX_NO_PRECONNECT"] = "1"
os.environ["HELIX_NO_SCHEDULER"] = "1"
os.environ["HELIX_NO_STUDIO_WORKER"] = "1"
sys.path.insert(0, "__HOME__/helix")
import app
import factgate_hook

# _lane_options(CHAT/WORK) calls _write_wall(); never let it rewrite the live wall_exec.sh.
_ORIG_WALL_EXEC = app.WALL_EXEC
_WALL_TMP = tempfile.mkdtemp(prefix="helix_test_chrome_")
app.WALL_EXEC = os.path.join(_WALL_TMP, "wall_exec.sh")


def tearDownModule():
    import shutil
    try:
        shutil.rmtree(_WALL_TMP, ignore_errors=True)
    finally:
        app.WALL_EXEC = _ORIG_WALL_EXEC


class ChromeFlag(unittest.TestCase):
    def test_constant_is_the_bare_chrome_flag(self):
        self.assertEqual(app.CHROME_LANES_FLAG, {"chrome": None})

    def test_chat_and_work_get_chrome(self):
        for lane in (app.CHAT, app.WORK):
            with self.subTest(lane=lane.name if hasattr(lane, "name") else lane):
                self.assertEqual(app._lane_options(lane).extra_args, {"chrome": None})

    def test_sched_has_no_chrome(self):
        self.assertNotIn("chrome", app._lane_options(app.SCHED).extra_args)

    def test_front_desk_has_no_chrome_and_still_no_tools(self):
        o = app._lane_options(app.FRONT)
        self.assertNotIn("chrome", o.extra_args)
        self.assertEqual(o.allowed_tools, [])
        self.assertEqual(o.setting_sources, [])
        self.assertNotEqual(o.permission_mode, "bypassPermissions")

    def test_lanes_do_not_share_the_constant(self):
        # A caller mutating one lane's extra_args must not hand Chrome to another lane.
        a = app._lane_options(app.CHAT).extra_args
        self.assertIsNot(app._lane_options(app.SCHED).extra_args, a)


class Regression(unittest.TestCase):
    """Everything but extra_args is what it was before order 59 (values pinned here, not read
    from a live path)."""
    TOOL_LANES = ("CHAT", "WORK", "SCHED")

    def test_hooks_unchanged(self):
        for name in self.TOOL_LANES:
            with self.subTest(lane=name):
                h = app._lane_options(getattr(app, name)).hooks
                self.assertEqual(sorted(h), ["PostToolUse", "PreToolUse"])
                for event, fn in (("PreToolUse", factgate_hook.pre_tool_use),
                                  ("PostToolUse", factgate_hook.post_tool_use)):
                    self.assertEqual(len(h[event]), 1)
                    m = h[event][0]
                    self.assertIsNone(m.matcher)
                    self.assertEqual(m.hooks, [fn])
                    self.assertEqual(m.timeout, 30)

    def test_disallowed_tools_and_budget_unchanged(self):
        for name in self.TOOL_LANES:
            with self.subTest(lane=name):
                o = app._lane_options(getattr(app, name))
                self.assertEqual(o.disallowed_tools, app._fanout_tools_now())
                self.assertEqual(o.max_budget_usd, app.TURN_BUDGET_USD)
                self.assertEqual(o.permission_mode, "bypassPermissions")

    def test_env_unchanged(self):
        for name in ("CHAT", "WORK"):
            with self.subTest(lane=name):
                env = app._lane_options(getattr(app, name)).env
                self.assertEqual(list(env), ["CLAUDE_CODE_SHELL_PREFIX"])
        self.assertEqual(app._lane_options(app.SCHED).env, {})

    def test_front_desk_unchanged(self):
        o = app._lane_options(app.FRONT)
        self.assertIsNone(o.hooks)
        self.assertEqual(o.max_turns, 1)
        self.assertEqual(o.disallowed_tools, [])
        self.assertIsNone(o.max_budget_usd)


if __name__ == "__main__":
    unittest.main()
