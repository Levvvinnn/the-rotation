import asyncio
import os
import random
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image  # noqa: E402

from rotation import fairness, money, wheel  # noqa: E402
from rotation.store import Store  # noqa: E402
from rotation.understand import rules, understand  # noqa: E402

DAY = 86400


class FairnessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "t.db")
        self.now = time.time()

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def seed(self, uid, name, days_ago):
        for d in days_ago:
            self.store.add(1, "drive", uid, name, ts=self.now - d * DAY)

    def test_demo_seed_spins_then_clears(self):
        self.seed(1, "Ana", [2, 6, 13])
        self.seed(2, "Ben", [9, 20])
        self.seed(3, "Cal", [12])
        people = {1: "Ana", 2: "Ben", 3: "Cal"}
        ranked = fairness.standings(people, self.store.events(1, "drive"), self.now)
        self.assertEqual([s.name for s in ranked], ["Cal", "Ben", "Ana"])
        self.assertFalse(fairness.is_clear(ranked))  # first ask: wheel
        self.store.add(1, "drive", 3, "Cal", ts=self.now)  # Cal drives
        ranked = fairness.standings(people, self.store.events(1, "drive"), self.now)
        self.assertTrue(fairness.is_clear(ranked))  # second ask: clear answer
        self.assertEqual(ranked[0].name, "Ben")

    def test_never_done_is_most_overdue(self):
        self.seed(1, "Ana", [1])
        ranked = fairness.standings({1: "Ana", 2: "Ben"}, self.store.events(1, "drive"), self.now)
        self.assertEqual(ranked[0].name, "Ben")
        self.assertIn("never", fairness.describe(ranked[0]))

    def test_pick_follows_weights(self):
        ranked = [fairness.Standing(1, "A", 0, None, 9.0), fairness.Standing(2, "B", 0, None, 1.0)]
        rng = random.Random(1)
        wins = sum(fairness.pick(ranked, rng) == 0 for _ in range(2000))
        self.assertTrue(1700 < wins < 1900)

    def test_chances_add_up(self):
        ranked = [fairness.Standing(i, str(i), 0, None, w) for i, w in enumerate([3.0, 2.0, 1.0])]
        self.assertAlmostEqual(sum(fairness.chances(ranked)), 100, delta=1)


class WheelTest(unittest.TestCase):
    def test_winner_lands_under_pointer(self):
        names, weights = ["Ana", "Ben", "Cal", "Dev"], [0.75, 3.3, 6.5, 2.0]
        for winner in range(4):
            with tempfile.TemporaryDirectory() as tmp:
                path = wheel.render(names, weights, winner, Path(tmp) / "w.gif", size=300, frames=12, seed=winner)
                with Image.open(path) as gif:
                    gif.seek(gif.n_frames - 1)
                    frame = gif.convert("RGB")
                # straight up from the center, between the hub and the name labels
                px = frame.getpixel((150, 95))
                want = Image.new("RGB", (1, 1), wheel.winner_color(winner)).getpixel((0, 0))
                self.assertLess(sum(abs(a - b) for a, b in zip(px, want)), 60, (winner, px, want))

    def test_gif_is_small_enough_for_discord(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = wheel.render(["Ana", "Ben", "Cal"], [1, 2, 3], 1, Path(tmp) / "w.gif")
            self.assertLess(path.stat().st_size, 8 * 1024 * 1024)


class UnderstandTest(unittest.TestCase):
    def test_asks(self):
        for text, task in [("whose turn to drive tonight", "drive"), ("who's driving to the game?", "drive"),
                           ("whose turn is it to pay", "pay"), ("who's getting the uber", "uber"),
                           ("ok whose turn to host", "host")]:
            got = rules(text)
            self.assertIsNotNone(got, text)
            self.assertEqual((got.kind, got.task), ("ask", task), text)

    def test_did(self):
        for text, task in [("I drove last night", "drive"), ("i paid for dinner", "pay"),
                           ("I got the uber home", "uber"), ("I hosted friday", "host"),
                           ("i just bought the groceries", "groceries")]:
            got = rules(text)
            self.assertIsNotNone(got, text)
            self.assertEqual((got.kind, got.task), ("did", task), text)

    def test_ignores_chatter(self):
        for text in ["lmao", "that movie was fire", "i love driving games", "see you at 8"]:
            self.assertIsNone(asyncio.run(understand(text)), text)

    def test_known_task_names_win(self):
        got = rules("whose turn for costco", known=["costco"])
        self.assertEqual((got.kind, got.task), ("ask", "costco"))

    def test_works_without_api_key(self):
        os.environ.pop("ANTHROPIC_API_KEY", None)
        got = asyncio.run(understand("I drove us to Tempe Marketplace"))
        self.assertEqual((got.kind, got.task), ("did", "drive"))


class MoneyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "m.db")
        self.trio = [(1, "Ana"), (2, "Ben"), (3, "Cal")]

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_split_always_adds_up(self):
        for cents in (1, 100, 4801, 9999):
            self.assertEqual(sum(c for _, _, c in money.split(cents, self.trio)), cents)

    def test_demo_balances_and_fewest_payments(self):
        self.store.add_expense(1, 1, "Ana", 6000, "dinner", money.split(6000, self.trio))
        self.store.add_expense(1, 2, "Ben", 2400, "uber", money.split(2400, self.trio))
        bal = money.balances(self.store.expenses(1))
        self.assertEqual({u: v for u, (_, v) in bal.items()}, {1: 3200, 2: -400, 3: -2800})
        pays = money.payments(bal)
        self.assertEqual([(f, t, c) for f, _, t, _, c in pays], [(3, 1, 2800), (2, 1, 400)])

    def test_settling_clears_the_debt(self):
        self.store.add_expense(1, 1, "Ana", 6000, "dinner", money.split(6000, self.trio))
        pays = money.payments(money.balances(self.store.expenses(1)))
        owed = money.owed_between(pays, 3, 1)
        self.assertEqual(owed, 2000)
        self.store.add_expense(1, 3, "Cal", owed, "settle up", [(1, "Ana", owed)], kind="settle")
        pays = money.payments(money.balances(self.store.expenses(1)))
        self.assertEqual(money.owed_between(pays, 3, 1), 0)

    def test_owing_money_makes_you_up_next_to_pay(self):
        now = time.time()
        for uid, name, d in [(1, "Ana", 3), (2, "Ben", 5), (3, "Cal", 7)]:
            self.store.add(1, "pay", uid, name, ts=now - d * DAY)
        self.store.add_expense(1, 1, "Ana", 6000, "dinner", money.split(6000, self.trio))
        self.store.add_expense(1, 2, "Ben", 2400, "uber", money.split(2400, self.trio))
        owes = money.owes(money.balances(self.store.expenses(1)))
        ranked = fairness.standings(dict((u, n) for u, n in self.trio), self.store.events(1, "pay"), now, owes)
        self.assertEqual(ranked[0].name, "Cal")
        self.assertTrue(fairness.is_clear(ranked))
        self.assertIn("owes $28.00", fairness.describe(ranked[0]))

    def test_money_phrases(self):
        got = rules("I paid $48 for dinner")
        self.assertEqual((got.kind, got.task, got.cents, got.note), ("did", "pay", 4800, "dinner"))
        got = rules("i got the uber home, 22 bucks")
        self.assertEqual((got.kind, got.task, got.cents), ("did", "uber", 2200))
        for text in ["paid you back <@123>", "just venmo'd you $20", "sent it back to you", "we're even now"]:
            self.assertEqual(rules(text).kind, "settle", text)
        self.assertEqual(rules("just venmo'd you $20").cents, 2000)
        self.assertIsNone(rules("i drove").cents)


if __name__ == "__main__":
    unittest.main()
