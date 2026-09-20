"""The mailbox sorter: it must never lose a person's mail to a tray.

Two ways to fail. File a human's message under promotional and it is never read. Cry
wolf on every newsletter and the attention flag means nothing. Both are tested here, and
so is the reason this module exists at all: urgency decided on probability mass, because
a flat distribution has no rounded level that means anything.
"""
import json
import os
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from jevkit import client, mailbox  # noqa: E402


def fake(answer_for):
    calls = []

    def transport(body, headers, timeout):
        request = json.loads(body)
        calls.append(request)
        answers = {name: answer_for(name, q, request["state"]) for name, q in request["questions"].items()}
        return json.dumps({"model": "jev-test", "answers": answers,
                           "usage": {"input_tokens": 1}, "latency_ms": 12}).encode()

    transport.calls = calls
    return transport


def jev(lane="needs_reply", urgency=0.5, spread=None, personal=0.9, confidence=0.95):
    """Answer with a lane, an urgency distribution, and P(written by a human)."""
    spread = spread or {int(urgency): 1.0}

    def answer(name, question, state):
        if name == "lane":
            return {"type": "choice", "choice": lane, "confidence": confidence,
                    "probabilities": {k: (0.9 if k == lane else 0.0) for k in question["criteria"]}}
        if name == "urgency":
            avg = sum(level * p for level, p in spread.items())
            return {"type": "score", "score": avg, "confidence": 0.9,
                    "probabilities": {str(k): v for k, v in spread.items()}}
        return {"type": "noul", "noul": personal}
    return fake(answer)


class MailboxTests(unittest.TestCase):
    def test_a_question_from_a_person_reaches_the_attention_flag(self):
        out = mailbox.classify(
            {"subject": "Re: invoice 2041 - can you check the totals?",
             "content": "Can you confirm line 3 before I send it? Need it today.",
             "sender": "dana@northside-partners.com"},
            transport=jev(lane="needs_reply", spread={3: 0.6, 4: 0.2, 2: 0.2}))
        self.assertEqual(out["lane"], "needs_reply")
        self.assertTrue(out["needs_attention"])
        self.assertAlmostEqual(out["urgent_mass"], 0.8, places=3)

    def test_a_newsletter_is_filed_without_an_interrupt(self):
        out = mailbox.classify(
            {"subject": "5 growth loops that still work",
             "content": "This week: pay social, three teardowns. Unsubscribe any time.",
             "sender": "hello@growthweekly.co"},
            transport=jev(lane="promotional", spread={0: 1.0}, personal=0.05))
        self.assertEqual(out["lane"], "promotional")
        self.assertFalse(out["needs_attention"])
        self.assertFalse(out["low_confidence"])

    def test_a_reply_we_started_is_never_cold_outreach(self):
        """The header signal earns its place here: same words, different answer."""
        transport = jev(lane="sales", spread={1: 0.9}, personal=0.4)
        mailbox.classify({"subject": "Re: schedule for October", "content": "Can you confirm the room?",
                          "sender": "kelsey@freshsheet.co", "labels": ["INBOX", "SENT"]},
                         transport=transport)
        state = transport.calls[0]["state"]
        self.assertTrue(state["user_replied_in_thread"])
        self.assertFalse(state["has_unsubscribe_header"])
        self.assertEqual(state["from_domain"], "freshsheet.co")
        self.assertEqual(state["sender_class"], "person")

    def test_a_human_writing_in_is_lifted_out_of_junk_whatever_the_lane_said(self):
        out = mailbox.classify(
            {"subject": "quick question", "content": "are you free thursday?", "sender": "priya.r@gmail.com"},
            transport=jev(lane="promotional", spread={2: 0.7, 3: 0.3}, personal=0.93))
        self.assertEqual(out["lane"], "needs_reply")
        self.assertTrue(out["low_confidence"])
        self.assertTrue(out["needs_attention"])

    def test_urgency_is_read_from_the_mass_not_the_rounded_score(self):
        """The spread and the point estimate disagree, and the spread is the one that matters.

        On a live bank alert Jev answered with a *flat* urgency distribution, confidence
        0.0, point estimate 2.7. jevmail stores `round(score) + 1`, so that landed at 4 of
        5 — a level nobody chose — from an answer that said nothing at all. Two examples
        here differ by 0.2 in the mass at the top but 1.3 in the point estimate; the mass is
        what a caller acts on, and the estimate is reported as what it is.
        """
        calm = mailbox.classify(
            {"subject": "Weekly activity summary", "content": "Here is a summary of your account activity.",
             "sender": "no-reply@notify.bank.com"},
            transport=jev(lane="updates", spread={0: 0.4, 1: 0.2, 2: 0.2, 3: 0.2}, personal=0.03))
        self.assertEqual(calm["lane"], "updates")
        self.assertAlmostEqual(calm["urgent_mass"], 0.2, places=3)
        self.assertFalse(calm["needs_attention"])

        pressing = mailbox.classify(
            {"subject": "Card declined: action needed", "content": "Your card was declined at the pump.",
             "sender": "no-reply@notify.bank.com"},
            transport=jev(lane="updates", spread={1: 0.1, 2: 0.5, 3: 0.2, 4: 0.2}, personal=0.03))
        self.assertAlmostEqual(pressing["urgent_mass"], 0.4, places=3)
        self.assertGreater(pressing["urgency"], calm["urgency"])
        self.assertAlmostEqual(calm["urgency"] - pressing["urgency"], -1.3, places=2)

    def test_an_unsure_lane_is_flagged_rather_than_filed(self):
        def answer(name, question, state):
            if name == "lane":
                return {"type": "choice", "choice": "updates", "confidence": 0.5,
                        "probabilities": {"updates": 0.48, "promotional": 0.45, "sales": 0.07}}
            if name == "urgency":
                return {"type": "score", "score": 0.5, "confidence": 0.8, "probabilities": {"1": 1.0}}
            return {"type": "noul", "noul": 0.2}
        out = mailbox.classify({"subject": "fyi", "content": "something happened", "sender": "x@y.com"},
                               transport=fake(answer))
        self.assertTrue(out["low_confidence"])
        self.assertTrue(out["needs_attention"])
        self.assertIn("unsure", out["reason"])

    def test_a_message_carrying_a_secret_is_not_sent_anywhere(self):
        transport = jev()
        out = mailbox.classify({"subject": "credentials", "content": "the password is hunter2, please reset it",
                                "sender": "s@customer.com"}, transport=transport)
        self.assertFalse(out["sent_to_jev"])
        self.assertTrue(out["needs_attention"])
        self.assertEqual(transport.calls, [])

    def test_jev_down_means_a_person_looks_not_that_the_mail_vanishes(self):
        def down(body, headers, timeout):
            raise client.JevError("network")
        out = mailbox.classify({"subject": "hello", "content": "anything", "sender": "a@b.com"}, transport=down)
        self.assertIsNone(out["lane"])
        self.assertTrue(out["needs_attention"])
        self.assertIn("unavailable", out["reason"])

    def test_a_lane_outside_the_closed_set_cannot_be_filed(self):
        """The client refuses an answer that was not offered, so this never reaches a tray."""
        out = mailbox.classify({"subject": "hi", "content": "there", "sender": "a@b.com"},
                               transport=jev(lane="urgent_but_not_a_lane"))
        self.assertIsNone(out["lane"])
        self.assertTrue(out["needs_attention"])
        self.assertIn("a person should look", out["reason"])

    def test_an_empty_message_costs_nothing(self):
        transport = jev()
        out = mailbox.classify({"subject": "", "content": "", "sender": "a@b.com"}, transport=transport)
        self.assertIsNone(out["lane"])
        self.assertFalse(out["needs_attention"])
        self.assertEqual(transport.calls, [])

    def test_the_state_block_is_what_the_sorter_promised(self):
        state = mailbox.build_state({"subject": "Your parcel arrives Tuesday",
                                     "content": "Track your parcel for the latest.",
                                     "sender": "tracking@parcelmail.com",
                                     "received": "2026-09-19T14:00:00Z"},
                                    has_unsubscribe=True, replied_before=False)
        self.assertEqual(state["from_domain"], "parcelmail.com")
        self.assertNotIn("@", json.dumps(state))
        self.assertTrue(state["has_unsubscribe_header"])
        self.assertFalse(state["user_replied_in_thread"])
        self.assertEqual(state["sender_class"], "list")  # an unsubscribe header makes it list mail
        self.assertEqual(state["received"], "2026-09-19T14:00:00Z")

    def test_the_summary_counts_lanes_and_names_what_was_unsure(self):
        rows = [
            mailbox.classify({"subject": "one", "content": "body one", "sender": "a@b.com"},
                             transport=jev(lane="needs_reply", spread={3: 0.9})),
            mailbox.classify({"subject": "two", "content": "body two", "sender": "c@d.com"},
                             transport=jev(lane="promotional", spread={0: 1.0}, personal=0.05)),
            mailbox.classify({"subject": "", "content": "", "sender": "e@f.com"}, transport=jev()),
        ]
        for i, row in enumerate(rows):
            row["subject"] = row.get("subject") or "one"
            row["id"] = i
        summary = mailbox.summarize(rows)
        self.assertEqual(summary["messages"], 3)
        self.assertEqual(summary["lanes"]["needs_reply"], 1)
        self.assertEqual(summary["lanes"]["promotional"], 1)
        self.assertEqual(summary["lanes"]["unsorted"], 1)
        self.assertEqual(summary["not_sent_to_jev"], 1)
        self.assertGreaterEqual(summary["cost_estimate_usd"], 0.0)

    def test_bulk_detection_needs_no_model(self):
        self.assertTrue(mailbox._looks_bulk("hello@x.co", "News", "Unsubscribe at any time"))
        self.assertTrue(mailbox._looks_bulk("alerts@bank.com", "Alert", "your transaction", "List-Unsubscribe: <x>"))
        self.assertFalse(mailbox._looks_bulk("dana@customer.com", "Invoice totals", "please confirm line 3"))


if __name__ == "__main__":
    unittest.main()