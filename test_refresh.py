from datetime import timedelta
from unittest import TestCase

from refresh import baseline, category, grade_market, now, receipt, tempo


class TotalsTests(TestCase):
    def test_independent_baseline(self):
        values = [
            dict(tempo=74, offense=115, defense=98),
            dict(tempo=64, offense=100, defense=105),
        ]
        forecast = baseline(values)
        self.assertEqual(forecast["possessions"], 69)
        self.assertAlmostEqual(forecast["scores"][0], 75.9)
        self.assertAlmostEqual(forecast["scores"][1], 68.31)
        self.assertAlmostEqual(forecast["total"], 144.21)

    def test_threshold_boundaries(self):
        self.assertEqual([tempo(x) for x in (63.99, 64, 67, 71, 74)],
                         ["Very Slow", "Slow", "Average", "Fast", "Very Fast"])
        self.assertEqual([category(x) for x in (1.99, -2, -4, 6, 8)],
                         ["Minimal", "Small", "Moderate", "Large", "Very Large"])

    def test_exact_total_rules(self):
        line = dict(direction="UNDER", line=150)
        self.assertEqual(grade_market(dict(line, settlement="strict-greater"), 150), "Win")
        self.assertEqual(grade_market(dict(line, settlement="push-on-equal"), 150), "Push")
        self.assertEqual(grade_market(dict(line, settlement="push-on-equal"), 151), "Loss")

    def test_receipt_requires_future_scheduled_tip_and_fresh_rating(self):
        stamp = now()
        game = dict(id="123", state="pre", statusCode="STATUS_SCHEDULED",
                    start=(stamp + timedelta(minutes=1)).isoformat(), markets=[],
                    model={"ratingsAt": stamp.isoformat()}, teams=[{"displayName": "A"}, {"displayName": "B"}])
        self.assertIsNotNone(receipt(game, stamp.isoformat()))
        self.assertIsNone(receipt(game, game["start"]))
        game["model"]["ratingsAt"] = (stamp - timedelta(days=2)).isoformat()
        self.assertIsNone(receipt(game, stamp.isoformat()))


class NbaTotalsTests(TestCase):
    def test_rate_estimators_and_overtime_pace(self):
        from nba_ratings import profiles
        games = [dict(teams={"1": 112, "2": 105}, possessions=106, minutes=53, date="2026-11-24",
                      tip="2026-11-24T23:00:00Z", id=str(i)) for i in range(5)]
        ratings = profiles(games)
        self.assertEqual(ratings["1"]["tempo"], 96)
        self.assertAlmostEqual(ratings["1"]["offense"], 11200 / 106)
        self.assertAlmostEqual(ratings["2"]["defense"], 11200 / 106)

    def test_kambi_provider_abbreviations_require_matching_nickname(self):
        from refresh import kambi_team_matches
        phoenix = {"abbreviation": "PHX", "name": "Suns"}
        self.assertTrue(kambi_team_matches("PHO Suns", phoenix, True))
        self.assertFalse(kambi_team_matches("PHO Rockets", phoenix, True))
        self.assertTrue(kambi_team_matches("LA Clippers", {"abbreviation": "LAC", "name": "Clippers"}, True))
