import unittest
from unittest.mock import patch

from app.services import agent_service


class TestAgentRouterBehavior(unittest.TestCase):
    @patch("app.services.agent_service.handle_general_chat")
    @patch("app.services.agent_service.handle_missing_ingredients")
    def test_missing_ingredients_fallbacks_to_general_chat_when_items_empty(
        self,
        mock_missing_handler,
        mock_general_handler,
    ):
        mock_general_handler.return_value = {
            "intent": "general_chat",
            "response": "fallback",
            "data": {},
            "why_this_action": "fallback",
        }

        result = agent_service.run_agent_router("u1", "i don't have")

        mock_missing_handler.assert_not_called()
        mock_general_handler.assert_called_once_with("u1", "i don't have")
        self.assertEqual(result.get("intent"), "general_chat")

    @patch("app.services.agent_service.handle_missing_ingredients")
    @patch("app.services.agent_service.handle_general_chat")
    def test_missing_ingredients_routes_to_missing_handler_when_items_present(
        self,
        mock_general_handler,
        mock_missing_handler,
    ):
        mock_missing_handler.return_value = {
            "intent": "missing_ingredients",
            "response": "missing flow",
            "data": {},
            "why_this_action": "strict match",
        }

        result = agent_service.run_agent_router("u1", "I don't have eggs and rice")

        mock_missing_handler.assert_called_once_with("u1", "I don't have eggs and rice")
        mock_general_handler.assert_not_called()
        self.assertEqual(result.get("intent"), "missing_ingredients")

    @patch("app.services.agent_service.handle_completion_update")
    @patch("app.services.agent_service.handle_general_chat")
    @patch("app.services.agent_service.handle_missing_ingredients")
    def test_completion_routes_single_handler_only(
        self,
        mock_missing_handler,
        mock_general_handler,
        mock_completion_handler,
    ):
        mock_completion_handler.return_value = {
            "intent": "completion_update",
            "response": "done",
            "data": {},
            "why_this_action": "completion",
        }

        result = agent_service.run_agent_router("u1", "I've done today's workout")

        mock_completion_handler.assert_called_once_with("u1", "I've done today's workout")
        mock_missing_handler.assert_not_called()
        mock_general_handler.assert_not_called()
        self.assertEqual(result.get("intent"), "completion_update")


if __name__ == "__main__":
    unittest.main()
