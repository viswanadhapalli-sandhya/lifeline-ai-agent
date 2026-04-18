import unittest

from app.services.agent_service import detect_intent, extract_items_from_message


class TestAgentIntentRouting(unittest.TestCase):
    def test_missing_ingredients_intent(self):
        self.assertEqual(detect_intent("I don't have eggs"), "missing_ingredients")
        self.assertEqual(detect_intent("I am out of rice and curd"), "missing_ingredients")

    def test_travel_intent(self):
        self.assertEqual(detect_intent("I am traveling for 4 days"), "travel_update")
        self.assertEqual(detect_intent("I am travelling this week"), "travel_update")

    def test_completion_intent(self):
        self.assertEqual(detect_intent("finished workout"), "completion_update")
        self.assertEqual(detect_intent("I completed my workout"), "completion_update")
        self.assertEqual(detect_intent("day 1 completed"), "general_chat")
        self.assertEqual(
            detect_intent("I feel full and happy and I've even done my workout of today's plan"),
            "completion_update",
        )

    def test_today_plan_intent(self):
        self.assertEqual(detect_intent("what is plan for today"), "today_plan_request")
        self.assertEqual(detect_intent("what to do today"), "today_plan_request")
        self.assertEqual(detect_intent("what's today's plan"), "today_plan_request")
        self.assertEqual(detect_intent("today workout"), "today_plan_request")

    def test_progress_intent(self):
        self.assertEqual(detect_intent("show my progress"), "progress_query")
        self.assertEqual(detect_intent("show stats"), "progress_query")

    def test_general_chat_fallback(self):
        self.assertEqual(detect_intent("how are you coach"), "general_chat")
        self.assertEqual(detect_intent("I feel happy and full"), "general_chat")
        self.assertEqual(detect_intent(""), "general_chat")


class TestMissingItemExtraction(unittest.TestCase):
    def test_extract_multiple_items(self):
        self.assertEqual(
            extract_items_from_message("I don't have eggs and rice"),
            ["eggs", "rice"],
        )

    def test_extract_out_of_pattern(self):
        self.assertEqual(
            extract_items_from_message("I am out of milk, paneer and oats"),
            ["milk", "paneer", "oats"],
        )

    def test_extract_no_pattern(self):
        self.assertEqual(
            extract_items_from_message("no curd and bananas"),
            [],
        )

    def test_extract_ignores_plan_noise(self):
        self.assertEqual(
            extract_items_from_message("I don't have today's plan"),
            [],
        )


if __name__ == "__main__":
    unittest.main()
