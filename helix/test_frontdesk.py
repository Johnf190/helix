import os, json, tempfile, unittest
import frontdesk as fd


class PadTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = os.path.join(self.d, "notepad.json")

    def test_missing_file_reads_as_empty(self):
        pad = fd.load_pad(self.p)
        self.assertEqual(pad["notes"], [])
        self.assertIsNone(pad["last_job"])

    def test_corrupt_file_reads_as_empty(self):
        open(self.p, "w").write("{not json at all")
        pad = fd.load_pad(self.p)
        self.assertEqual(pad["notes"], [])

    def test_add_note_persists_and_keeps_order(self):
        fd.add_note(self.p, "check the S-58 folder")
        fd.add_note(self.p, "and the 1972 photos")
        notes = fd.load_pad(self.p)["notes"]
        self.assertEqual([n["text"] for n in notes],
                         ["check the S-58 folder", "and the 1972 photos"])
        self.assertEqual(len({n["id"] for n in notes}), 2)
        self.assertTrue(notes[0]["at"])

    def test_add_note_rejects_blank(self):
        fd.add_note(self.p, "   ")
        self.assertEqual(fd.load_pad(self.p)["notes"], [])

    def test_clear_notes_leaves_last_job(self):
        fd.add_note(self.p, "one")
        fd.set_last_job(self.p, "abc123", "what is X", "X is 4")
        fd.clear_notes(self.p)
        pad = fd.load_pad(self.p)
        self.assertEqual(pad["notes"], [])
        self.assertEqual(pad["last_job"]["answer"], "X is 4")

    def test_set_last_job_overwrites(self):
        fd.set_last_job(self.p, "a", "q1", "a1")
        fd.set_last_job(self.p, "b", "q2", "a2")
        self.assertEqual(fd.load_pad(self.p)["last_job"]["job_id"], "b")

    def test_save_is_atomic_no_tmp_left_behind(self):
        fd.add_note(self.p, "one")
        self.assertEqual([f for f in os.listdir(self.d) if f != "notepad.json"], [])


class ControlTests(unittest.TestCase):
    def test_plain_talk_has_no_control(self):
        spoken, ctl = fd.parse_control("Good evening, sir.")
        self.assertEqual(spoken, "Good evening, sir.")
        self.assertEqual(ctl["kind"], "talk")

    def test_job_marker_is_extracted_and_stripped(self):
        spoken, ctl = fd.parse_control(
            "Of course, sir. I'll put Odin on it.\n[[JOB]] count the S-61 photos in the a friend archive")
        self.assertEqual(spoken, "Of course, sir. I'll put Odin on it.")
        self.assertEqual(ctl["kind"], "job")
        self.assertEqual(ctl["text"], "count the S-61 photos in the a friend archive")

    def test_job_marker_may_span_several_lines(self):
        spoken, ctl = fd.parse_control("Right away, sir.\n[[JOB]] do this\nand also that")
        self.assertEqual(spoken, "Right away, sir.")
        self.assertEqual(ctl["text"], "do this\nand also that")

    def test_empty_job_marker_degrades_to_talk(self):
        spoken, ctl = fd.parse_control("Certainly, sir.\n[[JOB]]   ")
        self.assertEqual(ctl["kind"], "talk")
        self.assertEqual(spoken, "Certainly, sir.")

    def test_send_marker(self):
        spoken, ctl = fd.parse_control("Sending them over now, sir.\n[[SEND]]")
        self.assertEqual(ctl["kind"], "send")
        self.assertEqual(spoken, "Sending them over now, sir.")

    def test_full_marker(self):
        spoken, ctl = fd.parse_control("Here it is in full, sir.\n[[FULL]]")
        self.assertEqual(ctl["kind"], "full")

    def test_marker_inline_mid_sentence_is_still_caught(self):
        spoken, ctl = fd.parse_control("I'll ask Odin. [[JOB]] find the registry file")
        self.assertEqual(ctl["kind"], "job")
        self.assertEqual(ctl["text"], "find the registry file")
        self.assertEqual(spoken, "I'll ask Odin.")

    def test_a_reply_that_is_only_a_marker_still_yields_some_speech(self):
        spoken, ctl = fd.parse_control("[[JOB]] go and look")
        self.assertEqual(ctl["kind"], "job")
        self.assertTrue(spoken.strip())      # never silence — Helix always says something


class BriefTests(unittest.TestCase):
    def test_brief_forbids_stating_facts(self):
        b = fd.BRIEF.lower()
        self.assertIn("never", b)
        self.assertIn("fact", b)

    def test_brief_documents_every_marker(self):
        for m in (fd.M_JOB, fd.M_SEND, fd.M_FULL):
            self.assertIn(m, fd.BRIEF)

    def test_brief_sets_the_butler_register(self):
        self.assertIn("sir", fd.BRIEF.lower())

    def test_brief_names_odin_as_the_worker(self):
        self.assertIn("Odin", fd.BRIEF)


class StatusLineTests(unittest.TestCase):
    def test_idle_says_free(self):
        s = fd.status_line(False, [], False)
        self.assertIn("free", s)
        self.assertNotIn("note", s)

    def test_busy_says_busy_and_tells_it_to_take_notes(self):
        s = fd.status_line(True, [], False)
        self.assertIn("busy", s)
        self.assertIn(fd.M_JOB, s)     # it is told the marker still means "note it"

    def test_notes_are_listed_verbatim(self):
        notes = [{"id": "a", "text": "check the S-58 folder", "at": "x"}]
        s = fd.status_line(False, notes, True)
        self.assertIn("check the S-58 folder", s)
        self.assertIn(fd.M_SEND, s)

    def test_last_job_present_offers_full(self):
        s = fd.status_line(False, [], True)
        self.assertIn(fd.M_FULL, s)

    def test_no_last_job_does_not_offer_full(self):
        s = fd.status_line(False, [], False)
        self.assertNotIn(fd.M_FULL, s)

    def test_status_line_is_short(self):
        # It is prepended to EVERY message. Keep it cheap.
        self.assertLess(len(fd.status_line(True, [{"id": "a", "text": "x", "at": "y"}], True)), 900)


if __name__ == "__main__":
    unittest.main()
