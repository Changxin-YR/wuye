import os
import unittest
from unittest.mock import patch

from dify_client import DeepSeekClient, OpenAICompatibleAgentClient, client_from_env


class ProviderTests(unittest.TestCase):
    def test_deepseek_uses_shared_openai_compatible_client(self):
        client = DeepSeekClient("https://api.deepseek.example/v1", "deepseek-secret", "deepseek-v4-pro")
        self.assertIsInstance(client, OpenAICompatibleAgentClient)
        self.assertEqual(client.provider, "deepseek")
        with patch.object(client, "_request", return_value={"id": "x", "choices": [{"message": {"content": "ok"}}]}) as request:
            result = client.chat("测试", "property-healthcheck")
        self.assertEqual(result["answer"], "ok")
        self.assertEqual(request.call_args.args[1], "/chat/completions")
        self.assertEqual(request.call_args.args[2]["model"], "deepseek-v4-pro")

    def test_client_from_env_reads_deepseek_key_without_exposing_it(self):
        with patch.dict(os.environ, {
            "AI_PROVIDER": "deepseek",
            "DEEPSEEK_API_KEY": "deepseek-secret",
            "DEEPSEEK_BASE_URL": "https://api.deepseek.com",
        }, clear=False):
            client = client_from_env()
        self.assertIsInstance(client, DeepSeekClient)
        self.assertEqual(client.model, "deepseek-v4-pro")
        self.assertNotIn("deepseek-secret", repr(client_from_env))

    def test_diagnose_shape_contains_provider_model_and_checks(self):
        client = DeepSeekClient("https://api.deepseek.example/v1", "deepseek-secret", "deepseek-v4-pro")
        with patch.object(client, "_request", return_value={"data": []}):
            result = client.check()
        self.assertEqual({result["provider"], result["model"], result["configured"]}, {"deepseek", "deepseek-v4-pro", True})
        self.assertIn("inference", result)
        self.assertIn("tool_call", result)


if __name__ == "__main__":
    unittest.main()
