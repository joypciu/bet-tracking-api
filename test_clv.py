import unittest
from unittest.mock import AsyncMock, patch

import jwt

import main


class TestClvHistorics(unittest.IsolatedAsyncioTestCase):
    def test_closing_history_uses_last_prestart_price(self):
        odds, timestamp = main._closing_history_odds(
            [
                {
                    "datetime": "2026-06-13T09:55:00+00:00",
                    "american_odds": -125,
                },
                {
                    "datetime": "2026-06-13T10:01:00+00:00",
                    "american_odds": -140,
                },
            ],
            "2026-06-13T10:00:00Z",
        )
        self.assertEqual(odds, -125)
        self.assertEqual(timestamp, "2026-06-13T09:55:00+00:00")

    def test_book_matching_ignores_spacing_and_case(self):
        name, series = main._find_book_history(
            {"DraftKings": [{"american_odds": -120}]},
            "draft kings",
        )
        self.assertEqual(name, "DraftKings")
        self.assertEqual(series[0]["american_odds"], -120)

    def test_event_datetime_can_be_read_from_signed_context(self):
        context = jwt.encode(
            {"date": "2026-06-13 10:00:00Z"},
            "test-secret-with-at-least-32-bytes",
            algorithm="HS256",
        )
        self.assertEqual(
            main._historics_event_datetime(context),
            "2026-06-13 10:00:00Z",
        )

    async def test_calculator_uses_same_book_and_nvig_histories(self):
        bet = {
            "bet_id": "bet-1",
            "odds": -100,
            "book": "Draft Kings",
            "historics_context": "signed-context",
            "event_datetime": "2026-06-13T10:00:00Z",
        }
        historics = {
            "books": {
                "DraftKings": [
                    {
                        "datetime": "2026-06-13T09:55:00+00:00",
                        "american_odds": -125,
                    }
                ]
            },
            "nvig": [
                {
                    "datetime": "2026-06-13T09:56:00+00:00",
                    "american_odds": -118,
                }
            ],
        }
        with (
            patch.object(main.bet_tracking, "get_bet", return_value=bet),
            patch.object(main.bet_tracking, "update_bet_clv") as update,
            patch.object(
                main.historics_bridge,
                "fetch_historics",
                new=AsyncMock(return_value=historics),
            ),
        ):
            await main._calculate_clv_for_bet("bet-1")

        update.assert_called_once_with(
            "bet-1",
            0.1111,
            0.0826,
            "keepbetting_historics",
            "DraftKings",
            -125,
            -118,
            "2026-06-13T09:55:00+00:00",
        )

if __name__ == "__main__":
    unittest.main()
