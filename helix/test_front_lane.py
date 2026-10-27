"""In-process checks of the front-lane wiring. No server, no SDK connection, no model calls.

Run:  .venv/bin/python -m pytest test_front_lane.py -q
"""
import os, sys, unittest

# Same guards as test_work_lane.py: import app without booting a warmup turn, a persistent
# SDK pre-connect, the scheduler ticker or the studio worker.
os.environ["HELIX_NO_WARMUP"] = "1"
os.environ["HELIX_NO_PRECONNECT"] = "1"
os.environ["HELIX_NO_SCHEDULER"] = "1"
os.environ["HELIX_NO_STUDIO_WORKER"] = "1"
sys.path.insert(0, "__HOME__/helix")
import app

# Redirect persisted UI state into a scratch file BEFORE any route runs, or the route tests
# below write the real memory/ui_state.json (found by order 12's step 0).
import tempfile as _tf
_ORIG_UI_STATE = app.UI_STATE
app.UI_STATE = os.path.join(_tf.mkdtemp(), "ui_state.json")

# Order 32: _lane_options(CHAT/WORK) calls _write_wall(), which rewrote the live ~/helix/wall_exec.sh.
# Point it at a temp path, as test_write_wall.py's autouse fixture does; tearDownModule restores it.
_ORIG_WALL_EXEC = app.WALL_EXEC
_WALL_TMP = _tf.mkdtemp(prefix="helix_test_wall_")
app.WALL_EXEC = os.path.join(_WALL_TMP, "wall_exec.sh")


def tearDownModule():
    import shutil
    try:
        shutil.rmtree(_WALL_TMP, ignore_errors=True)
    finally:
        app.WALL_EXEC = _ORIG_WALL_EXEC


class LaneModelTests(unittest.TestCase):
    def test_unpinned_lane_follows_odin_model(self):
        L = app.Lane("probe")
        self.assertEqual(L.want_model, app.ODIN_MODEL)

    def test_pinned_lane_keeps_its_own_model(self):
        L = app.Lane("probe", model="haiku")
        self.assertEqual(L.want_model, "haiku")

    def test_front_lane_exists_and_is_pinned_to_haiku(self):
        self.assertEqual(app.FRONT.name, "front")
        self.assertEqual(app.FRONT.want_model, os.environ.get("HELIX_FRONT_MODEL", "haiku"))

    def test_front_lane_is_not_chat_or_work(self):
        self.assertIsNot(app.FRONT, app.CHAT)
        self.assertIsNot(app.FRONT, app.WORK)
        self.assertIsNot(app.FRONT.lock, app.WORK.lock)


class LaneOptionsTests(unittest.TestCase):
    def test_work_lane_gets_tools_and_bypass(self):
        o = app._lane_options(app.WORK)
        self.assertEqual(o.permission_mode, "bypassPermissions")
        self.assertEqual(o.model, app.ODIN_MODEL)

    def test_front_lane_gets_no_tools(self):
        o = app._lane_options(app.FRONT)
        self.assertEqual(o.allowed_tools, [])
        self.assertNotEqual(o.permission_mode, "bypassPermissions")
        self.assertEqual(o.max_turns, 1)

    def test_front_lane_carries_the_brief_as_system_prompt(self):
        import frontdesk
        o = app._lane_options(app.FRONT)
        self.assertEqual(o.system_prompt, frontdesk.BRIEF)

    def test_front_lane_loads_no_project_settings(self):
        # No CLAUDE.md, no skills, no plugins — they cost latency and could smuggle in tools.
        o = app._lane_options(app.FRONT)
        self.assertEqual(o.setting_sources, [])

    def test_work_lane_has_no_system_prompt_override(self):
        o = app._lane_options(app.WORK)
        self.assertIsNone(o.system_prompt)

    def test_order32_the_wrapper_goes_to_temp_not_the_live_file(self):
        o = app._lane_options(app.WORK)
        self.assertNotEqual(os.path.realpath(app.WALL_EXEC), os.path.realpath(_ORIG_WALL_EXEC))
        self.assertEqual(o.env["CLAUDE_CODE_SHELL_PREFIX"], app.WALL_EXEC)
        self.assertTrue(os.path.isfile(app.WALL_EXEC))


class FrontPromptTests(unittest.TestCase):
    def test_prompt_carries_the_user_text(self):
        p = app._front_prompt("good evening")
        self.assertIn("good evening", p)

    def test_prompt_states_odin_is_free_when_work_is_idle(self):
        self.assertFalse(app.WORK.lock.locked())
        self.assertIn("Odin is free", app._front_prompt("hello"))

    def test_prompt_states_odin_is_busy_while_work_is_locked(self):
        app.WORK.lock.acquire()
        try:
            self.assertIn("Odin is busy", app._front_prompt("hello"))
        finally:
            app.WORK.lock.release()

    def test_prompt_includes_waiting_notes(self):
        import frontdesk, tempfile, os as _os
        d = tempfile.mkdtemp(); p = _os.path.join(d, "notepad.json")
        old, app.NOTEPAD = app.NOTEPAD, p
        try:
            frontdesk.add_note(p, "check the S-58 folder")
            self.assertIn("check the S-58 folder", app._front_prompt("anything else?"))
        finally:
            app.NOTEPAD = old


class FrontDisabledTests(unittest.TestCase):
    def test_front_disabled_returns_none(self):
        old, app.FRONT_ON = app.FRONT_ON, False
        try:
            self.assertIsNone(app.front_raw("hello"))
        finally:
            app.FRONT_ON = old


class TurnPromptTests(unittest.TestCase):
    """The front desk is a DIFFERENT PERSON from Odin and must not be primed as him.

    _run_turn prepends _preamble() — Odin's persona plus the fact digest, ~87k characters —
    to the first turn of a lane. On FRONT that both drowns the 2.3k-char butler brief (which
    is why it stopped emitting [[JOB]] markers) and costs the exact latency the front lane
    exists to remove. Caught live 2026-09-19.
    """
    def test_work_lane_is_still_primed_with_odins_preamble(self):
        app.WORK.primed = False
        p = app._turn_prompt(app.WORK, "hello")
        self.assertIn("You are ODIN", p)
        self.assertIn("=== USER ===", p)

    def test_front_lane_never_receives_the_preamble(self):
        app.FRONT.primed = False
        p = app._turn_prompt(app.FRONT, "hello")
        self.assertNotIn("You are ODIN", p)
        self.assertNotIn("=== USER ===", p)

    def test_front_lane_prompt_is_just_the_text(self):
        app.FRONT.primed = False
        self.assertEqual(app._turn_prompt(app.FRONT, "good evening"), "good evening")

    def test_front_prompt_stays_small_while_odins_is_huge(self):
        app.FRONT.primed = False
        app.WORK.primed = False
        self.assertLess(len(app._turn_prompt(app.FRONT, "hi")), 2000)
        self.assertGreater(len(app._turn_prompt(app.WORK, "hi")), 5000)


class DirectPathTests(unittest.TestCase):
    def test_plain_message_is_not_direct(self):
        self.assertEqual(app._is_direct_to_odin("how are you"), (False, "how are you"))

    def test_named_odin_is_direct_and_the_name_is_stripped(self):
        self.assertEqual(app._is_direct_to_odin("Odin, read the registry"),
                         (True, "read the registry"))

    def test_named_odin_no_comma(self):
        self.assertEqual(app._is_direct_to_odin("odin read the registry"),
                         (True, "read the registry"))

    def test_odin_mid_sentence_is_NOT_direct(self):
        # "ask odin about X" is the user talking TO Helix about Odin, not to Odin.
        self.assertEqual(app._is_direct_to_odin("ask odin about the registry")[0], False)

    def test_bare_name_is_direct_with_empty_remainder(self):
        self.assertEqual(app._is_direct_to_odin("Odin?"), (True, ""))


class DefaultModeTests(unittest.TestCase):
    """the user's ruling 2026-09-19: Helix is the default front, Odin is the override."""
    def test_ask_stream_defaults_to_helix(self):
        import inspect, re
        src = inspect.getsource(app.ask_stream)
        self.assertRegex(src, r'get\("mode",\s*"helix"\)')

    def test_the_canonical_ui_sends_helix(self):
        html = open("__HOME__/helix/static/index_v6.html").read()
        self.assertIn("mode:'helix'", html)


class RoutingTests(unittest.TestCase):
    """Drive _produce with front_raw and start_job stubbed — no model, no threads."""
    def setUp(self):
        import tempfile, os as _os
        self.d = tempfile.mkdtemp()
        self.pad = _os.path.join(self.d, "notepad.json")
        self._old = (app.NOTEPAD, app.front_raw, app.start_job, app.greet, app._log_turn)
        app.NOTEPAD = self.pad
        app.greet = lambda t: None          # the dead ollama front desk stays out of these tests
        # _produce writes the turn to the REAL session transcript. Left unstubbed these tests
        # append junk ("anything") to the user's actual conversation log — they did, once.
        app._log_turn = lambda *a, **k: None
        self.jobs = []
        app.start_job = lambda t, mode="odin": self.jobs.append(t) or app.TurnBuf("j", t, mode)

    def tearDown(self):
        (app.NOTEPAD, app.front_raw, app.start_job, app.greet, app._log_turn) = self._old

    def _run(self, reply, text="anything"):
        app.front_raw = lambda t: reply
        buf = app.TurnBuf("t1", text, "helix")
        app._produce(buf)
        return buf.snapshot()

    def test_talk_is_spoken_and_no_job_is_started(self):
        snap = self._run("Good evening, sir.")
        self.assertIn("Good evening, sir.", snap["text"])
        self.assertEqual(self.jobs, [])

    def test_job_while_odin_is_free_starts_a_job(self):
        snap = self._run("I'll have Odin check, sir.\n[[JOB]] count the photos")
        self.assertEqual(self.jobs, ["count the photos"])
        self.assertIn("Odin check", snap["text"])
        self.assertNotIn("[[JOB]]", snap["text"])

    def test_job_while_odin_is_busy_goes_on_the_pad_instead(self):
        import frontdesk
        app.WORK.lock.acquire()
        try:
            self._run("Noted, sir.\n[[JOB]] check the S-58 folder")
        finally:
            app.WORK.lock.release()
        self.assertEqual(self.jobs, [])
        self.assertEqual([n["text"] for n in frontdesk.load_pad(self.pad)["notes"]],
                         ["check the S-58 folder"])

    def test_send_dispatches_every_note_as_one_job_and_clears_the_pad(self):
        import frontdesk
        frontdesk.add_note(self.pad, "one")
        frontdesk.add_note(self.pad, "two")
        self._run("Sending them now, sir.\n[[SEND]]")
        self.assertEqual(len(self.jobs), 1)
        self.assertIn("one", self.jobs[0]); self.assertIn("two", self.jobs[0])
        self.assertEqual(frontdesk.load_pad(self.pad)["notes"], [])

    def test_send_with_an_empty_pad_dispatches_nothing(self):
        self._run("Nothing waiting, sir.\n[[SEND]]")
        self.assertEqual(self.jobs, [])

    def test_full_replays_odins_own_words_verbatim(self):
        import frontdesk
        frontdesk.set_last_job(self.pad, "j1", "q", "The count is 169 photographs.")
        snap = self._run("Here it is in full, sir.\n[[FULL]]")
        self.assertIn("The count is 169 photographs.", snap["text"])

    def test_full_with_no_stored_job_says_so_and_does_not_crash(self):
        snap = self._run("Here it is, sir.\n[[FULL]]")
        self.assertTrue(snap["text"].strip())

    def test_direct_to_odin_bypasses_the_front_entirely(self):
        seen = []
        app.front_raw = lambda t: seen.append(t) or "should never be used"
        old = app._run_escalated
        app._run_escalated = lambda buf, run_text, log_text=None, lane=None: (
            seen.append(("odin", run_text)), buf.emit("final", {"text": "odin answered"}))
        try:
            buf = app.TurnBuf("t3", "Odin, read the registry", "helix")
            app._produce(buf)
        finally:
            app._run_escalated = old
        self.assertEqual(seen, [("odin", "read the registry")])

    def test_front_returning_none_falls_through_to_odin(self):
        app.front_raw = lambda t: None
        calls = []
        old = app._run_escalated
        app._run_escalated = lambda buf, run_text, log_text=None, lane=None: (
            calls.append(lane), buf.emit("final", {"text": "odin answered"}))
        try:
            buf = app.TurnBuf("t2", "hello", "helix")
            app._produce(buf)
        finally:
            app._run_escalated = old
        self.assertEqual(calls, [app.CHAT])


class QuietLoggingTests(unittest.TestCase):
    """Front-desk chatter stays out of the transcript — but ONLY the front desk's.

    The first cut inferred quiet from `log_text is None`, which is also the default for every
    ordinary caller (_produce passes no log_text), so it silently stopped logging Odin's
    conversation. test_buffer_recovery caught it. The flag is explicit for that reason.
    """
    def test_run_escalated_takes_an_explicit_quiet_flag(self):
        import inspect
        sig = inspect.signature(app._run_escalated)
        self.assertIn("quiet", sig.parameters)
        self.assertIs(sig.parameters["quiet"].default, False)

    def test_front_raw_asks_for_quiet(self):
        import inspect
        self.assertIn("quiet=True", inspect.getsource(app.front_raw))

    def test_ordinary_turns_are_still_logged(self):
        import inspect
        src = inspect.getsource(app._run_escalated)
        self.assertNotIn("quiet = log_text is None", src)


class ReportTests(unittest.TestCase):
    def setUp(self):
        import tempfile, os as _os
        self.pad = _os.path.join(tempfile.mkdtemp(), "notepad.json")
        self._old = (app.NOTEPAD, app.front_raw)
        app.NOTEPAD = self.pad

    def tearDown(self):
        app.NOTEPAD, app.front_raw = self._old

    def _finished(self, answer):
        buf = app.TurnBuf("j9", "count the photos", "odin")
        buf.emit("final", {"text": answer}); buf.finish("done")
        return buf

    def test_odins_full_answer_is_stored_verbatim(self):
        import frontdesk
        app.front_raw = lambda t: "Odin counted them, sir."
        long = "I counted 169 photographs across 73 airframes." + " detail" * 200
        app._report_job(self._finished(long))
        self.assertEqual(frontdesk.load_pad(self.pad)["last_job"]["answer"], long)

    def test_the_spoken_bubble_is_helixs_own_words_not_odins(self):
        app.front_raw = lambda t: "Odin counted them, sir. A hundred and sixty-nine."
        buf = self._finished("I counted 169 photographs across 73 airframes. " + "x" * 3000)
        app._report_job(buf)
        final = buf.snapshot()["text"]
        self.assertIn("sir", final)
        self.assertLess(len(final), 400)

    def test_a_failed_headline_falls_back_to_odins_text(self):
        app.front_raw = lambda t: None
        buf = self._finished("I counted 169 photographs.")
        app._report_job(buf)
        self.assertIn("169", buf.snapshot()["text"])

    def test_headline_request_is_not_routed_back_as_a_job(self):
        # A [[JOB]] marker in a headline would start an infinite job loop. It must be dropped.
        app.front_raw = lambda t: "Done, sir.\n[[JOB]] do it again"
        started = []
        old, app.start_job = app.start_job, lambda t, mode="odin": started.append(t)
        try:
            app._report_job(self._finished("done"))
        finally:
            app.start_job = old
        self.assertEqual(started, [])


class VoiceTests(unittest.TestCase):
    def setUp(self):
        self.c = app.app.test_client()

    def test_default_voice_is_odins_blend(self):
        r = self.c.get("/front/voice")
        self.assertEqual(r.status_code, 200)
        self.assertIn(r.get_json()["voice"], ("odin", "alice"))
        self.assertEqual(sorted(r.get_json()["choices"]), ["alice", "odin"])

    def test_switching_to_alice_persists(self):
        self.c.post("/front/voice", json={"voice": "alice"})
        self.assertEqual(self.c.get("/front/voice").get_json()["voice"], "alice")
        self.c.post("/front/voice", json={"voice": "odin"})
        self.assertEqual(self.c.get("/front/voice").get_json()["voice"], "odin")

    def test_unknown_voice_is_rejected(self):
        r = self.c.post("/front/voice", json={"voice": "bm_george"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.c.get("/front/voice").get_json()["voice"], "odin")

    def test_helix_voice_returns_an_array_not_a_name(self):
        import numpy as np
        self.assertIsInstance(app._helix_voice(), np.ndarray)


class BargeInTests(unittest.TestCase):
    """Talking over Helix stops it — on the mic, on send, and on STOP."""
    def setUp(self):
        self.html = open("__HOME__/helix/static/index_v6.html").read()

    def test_stop_speaking_exists(self):
        self.assertIn("function stopSpeaking(", self.html)

    def test_it_is_wired_to_three_triggers(self):
        # mic open, send, and the STOP button all mean "I am taking the floor".
        self.assertGreaterEqual(self.html.count("stopSpeaking()"), 3)

    def test_the_voice_selector_is_present(self):
        self.assertIn('id="helixVoice"', self.html)
        self.assertIn("/front/voice", self.html)


class AudioWedgeTests(unittest.TestCase):
    """A refused autoplay must not wedge the speech queue.

    Brave/Chrome reject play() on a page that has had no click or keypress. The original
    `a.play().catch(()=>{})` swallowed that with _aplay still true, so enqueueAudio queued
    every later clip and nothing was ever spoken again — text kept arriving, sound never did.
    Hit live 2026-09-19 the moment a fresh window was opened and talked to by voice.
    """
    def setUp(self):
        self.html = open("__HOME__/helix/static/index_v6.html").read()

    def test_the_silent_catch_is_gone(self):
        self.assertNotIn("a.play().catch(()=>{})", self.html)

    def test_a_refused_play_stands_the_queue_back_down(self):
        self.assertIn("_aplay=false;_acur=null;_aq.unshift(c);armAudioGesture();", self.html)

    def test_a_gesture_listener_resumes_playback(self):
        self.assertIn("function armAudioGesture(", self.html)
        self.assertIn("addEventListener('click',go)", self.html)
        self.assertIn("addEventListener('keydown',go)", self.html)


class SpeakerNameTests(unittest.TestCase):
    """who() indexes DOM ids and matrix rows (odin/alice only); speaker() is the label."""
    def setUp(self):
        self.html = open("__HOME__/helix/static/index_v6.html").read()

    def test_who_never_returns_helix(self):
        self.assertIn("function who(){return S.mode==='alice'?'ALICE':'ODIN';}", self.html)

    def test_speaker_does_return_helix(self):
        self.assertIn("function speaker(){", self.html)
        self.assertIn("'HELIX'", self.html)

    def test_the_wake_word_accepts_helix_too(self):
        self.assertIn("h[ea]l[iey]+x", self.html)


class ProactiveSpeechTests(unittest.TestCase):
    """A finished background job must be SPOKEN, not just printed.

    Instant replies get their audio from the _TTSPipeline inside /ask_stream. A proactive
    report never goes through that route — it is fetched from /turn_state by noteCheck() —
    so nothing ever synthesized it and the bubble arrived in silence. the user, 2026-09-20:
    "it dosent speak whn it hits back after a while when odin finishes its job but when
    instand reply it speks fine."
    """
    def setUp(self):
        self.html = open("__HOME__/helix/static/index_v6.html").read()

    def test_a_report_is_handed_to_speakText(self):
        self.assertIn("speakText(t.text);", self.html)

    def test_speakText_asks_the_server_for_audio(self):
        self.assertIn("async function speakText(", self.html)
        self.assertIn("fetch('/speak'", self.html)

    def test_it_reuses_the_one_audio_queue(self):
        # Not a second player: MUTE, barge-in and the stuck-queue guard must all still apply.
        i = self.html.index("async function speakText(")
        body = self.html[i:i + 1400]
        self.assertIn("enqueueAudio(", body)
        self.assertNotIn("new Audio(", body)

    def test_a_muted_report_says_so_instead_of_synthesizing(self):
        i = self.html.index("async function speakText(")
        body = self.html[i:i + 1400]
        self.assertIn("if(_muted)", body)


class SpeakEndpointTests(unittest.TestCase):
    def test_speak_uses_helix_voice_for_anyone_but_alice(self):
        import inspect
        src = inspect.getsource(app.speak_on_demand)
        self.assertIn("_helix_voice()", src)

    def test_speak_rejects_empty_text(self):
        r = app.app.test_client().post("/speak", json={"text": "   ", "who": "odin"})
        self.assertEqual(r.status_code, 400)


class MobilePageTests(unittest.TestCase):
    """The phone page the user opens over Tailscale (/m -> static/mobile.html).

    the user, 2026-09-20: one entity, a chatbox keeping every feature it already had, and a
    button to bypass Helix when he wants Odin directly. Updated IN PLACE — never a new page
    (see the standing rule that features are built into the Helix app, not beside it).
    """
    def setUp(self):
        self.html = open("__HOME__/helix/static/mobile.html").read()

    # ── one entity ────────────────────────────────────────────────────────────
    def test_the_two_entity_rows_are_gone(self):
        for dead in ("eOdin", "eAlice", "sAlice"):
            self.assertNotIn(dead, self.html)

    def test_there_is_one_orb(self):
        self.assertIn('id="ent"', self.html)
        self.assertIn('id="orb"', self.html)

    def test_the_orb_shows_the_speech_envelope(self):
        # /speak and /ask_stream already send `env`; the orb is driven by it.
        self.assertIn('id="eBars"', self.html)
        self.assertIn("function drawEnv(", self.html)

    # ── the bypass ────────────────────────────────────────────────────────────
    def test_the_arm_button_exists_and_starts_unarmed(self):
        self.assertIn('id="arm"', self.html)
        self.assertIn('data-t="helix"', self.html)

    def test_it_cycles_helix_odin_alice(self):
        self.assertIn('setTarget(target === "helix" ? "odin" : (target === "odin" ? "alice" : "helix"))',
                      self.html)

    def test_the_bypass_is_spent_by_one_message(self):
        # Without this a forgotten mode means Opus answers small talk all evening.
        self.assertIn('setTarget("helix");            // the bypass is spent by exactly one message',
                      self.html)

    def test_helix_is_the_default_target(self):
        self.assertIn('let target = "helix";', self.html)

    # ── speech ────────────────────────────────────────────────────────────────
    def test_the_phone_now_asks_for_audio(self):
        # It used to hard-code voice:false and only speak on a tap.
        self.assertIn("voice: !muted", self.html)
        self.assertNotIn("voice: false", self.html)

    def test_a_landing_report_is_spoken(self):
        self.assertIn("speakNow(t.text);", self.html)
        self.assertIn("async function speakNow(", self.html)

    def test_one_audio_element_and_one_queue(self):
        # Mobile only unlocks playback on a real tap, so a second element would be silent.
        self.assertEqual(self.html.count("new Audio()"), 2)   # live queue + tap-to-speak
        self.assertIn("liveQ.unshift(c); livePlaying = false;", self.html)   # no wedge

    def test_mute_is_remembered(self):
        self.assertIn('localStorage.getItem("helix_m_mute")', self.html)

    # ── nothing was lost ──────────────────────────────────────────────────────
    def test_every_existing_feature_survived(self):
        for keep in ("/history", "/turn_state", "/listen/phone", "/upload", "/speak",
                     "cfab", "csheet", "aOptCam", "aOptFile", "recoverTurn", "hx_turn"):
            self.assertIn(keep, self.html, f"{keep} was dropped from the mobile page")


class MobileStatTests(unittest.TestCase):
    def test_stat_tells_the_phone_about_odin(self):
        r = app.app.test_client().get("/stat")
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertIn("work_busy", j)
        self.assertIn("notes", j)
        self.assertIsInstance(j["work_busy"], bool)

    def test_the_phone_hides_the_line_on_an_older_server(self):
        html = open("__HOME__/helix/static/mobile.html").read()
        self.assertIn('if (typeof s.work_busy !== "boolean"){ el.hidden = true; return; }', html)


if __name__ == "__main__":
    unittest.main()
