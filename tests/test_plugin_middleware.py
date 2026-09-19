"""Hermetic tests for the plugin's routing middleware.

The plugin module is loaded from the repo and its Jev call is replaced with a spy,
so these tests never touch the network, the real decision log or the real key.
"""
import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock

PLUGIN = Path(__file__).resolve().parents[1] / "hermes" / "plugin" / "hermes-jev"
REPO = Path(__file__).resolve().parents[1]
# The repo keeps jevkit at the root while the installed plugin bundles a copy inside
# its own directory, so the package path has to cover both layouts.
spec = importlib.util.spec_from_file_location(
    "hermes_jev_under_test", PLUGIN / "__init__.py",
    submodule_search_locations=[str(PLUGIN), str(REPO)])
plugin = importlib.util.module_from_spec(spec)
sys.modules["hermes_jev_under_test"] = plugin
spec.loader.exec_module(plugin)

DEFAULT = "deepseek/deepseek-v4.1-flash"
HARD = "The scheduler deadlocks under load. Find the race and propose a fix."


class RoutingMiddlewareTests(unittest.TestCase):
    def setUp(self):
        self.decisions = []

        def fake_decide(prompt, **kwargs):
            self.decisions.append(kwargs)
            if kwargs.get("pinned"):
                return {"routed": False, "model": kwargs.get("current"), "model_id": None,
                        "reason": "you pinned this model"}
            return {"routed": True, "model": "openrouter:moonshotai/kimi-k3",
                    "model_id": "moonshotai/kimi-k3", "reason": "hard coding", "notice": "Jev: kimi-k3"}

        for patch in (
            mock.patch.object(plugin, "_setting", lambda name, default: "on" if name == "routing" else default),
            mock.patch.object(plugin, "_default_model", lambda: DEFAULT),
            mock.patch.object(plugin, "_log", lambda entry: None),
            mock.patch.object(plugin.route, "decide", side_effect=fake_decide),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def turn(self, session: str, turn_id: str, model: str):
        plugin._on_pre_llm_call(session_id=session, turn_id=turn_id, user_message=HARD)
        return plugin._on_llm_request(
            request={"messages": [{"role": "user", "content": HARD}], "model": model},
            session_id=session, turn_id=turn_id, model=model, provider="openrouter")

    def test_bare_default_model_is_not_treated_as_pinned(self):
        result = self.turn("s1", "t1", DEFAULT)
        self.assertEqual(self.decisions[0]["current"], f"openrouter:{DEFAULT}")
        self.assertFalse(self.decisions[0]["pinned"])
        self.assertEqual(result["request"]["model"], "moonshotai/kimi-k3")

    def test_prefixed_model_is_not_double_prefixed_and_still_routes(self):
        result = self.turn("s2", "t1", f"openrouter:{DEFAULT}")
        self.assertEqual(self.decisions[0]["current"], f"openrouter:{DEFAULT}")
        self.assertFalse(self.decisions[0]["pinned"])
        self.assertEqual(result["request"]["model"], "moonshotai/kimi-k3")

    def test_a_model_the_user_picked_is_never_overridden(self):
        result = self.turn("s3", "t1", "openrouter:z-ai/glm-5.3-flash")
        self.assertTrue(self.decisions[0]["pinned"])
        self.assertIsNone(result)

    def test_a_prefixed_pin_is_still_recognised_as_a_pin(self):
        result = self.turn("s4", "t1", "openrouter:z-ai/glm-5.3-flash")
        self.assertEqual(self.decisions[0]["current"], "openrouter:z-ai/glm-5.3-flash")
        self.assertIsNone(result)

    def test_off_mode_never_calls_jev(self):
        with mock.patch.object(plugin, "_setting", lambda name, default: "off" if name == "routing" else default):
            result = self.turn("s5", "t1", DEFAULT)
        self.assertIsNone(result)

    def test_a_stale_turn_id_is_ignored(self):
        plugin._on_pre_llm_call(session_id="s6", turn_id="t2", user_message=HARD)
        result = plugin._on_llm_request(
            request={"messages": [{"role": "user", "content": HARD}], "model": DEFAULT},
            session_id="s6", turn_id="t1", model=DEFAULT, provider="openrouter")
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
