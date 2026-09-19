import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("jev_install", Path(__file__).resolve().parents[1] / "install.py")
install = importlib.util.module_from_spec(spec)
spec.loader.exec_module(install)

CONFIG = """model:
  default: some/model   # keep this comment
plugins:
  enabled:
  - coagent-observer
  disabled: []
  entries:
    resource-lifecycle:
      allow_tool_override: false
security:
  redact_secrets: true
"""


class InstallTests(unittest.TestCase):
    def test_home_warning_flags_a_profile_scoped_install(self):
        self.assertIsNotNone(install.home_warning(Path("/srv/hermes/profiles/devbot")))
        self.assertIsNone(install.home_warning(Path("/srv/hermes")))

    def test_enable_and_disable_touch_only_the_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.yaml"
            config.write_text(CONFIG)
            self.assertEqual(install.enable_plugin(config, True), "enabled")
            self.assertEqual(install.enable_plugin(config, True), "already enabled")
            text = config.read_text()
            self.assertIn("  enabled:\n  - hermes-jev\n  - coagent-observer\n", text)
            self.assertIn("# keep this comment", text)
            self.assertEqual(install.enable_plugin(config, False), "disabled")
            self.assertEqual(config.read_text(), CONFIG)

    def test_empty_inline_list_and_missing_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.yaml"
            config.write_text("plugins:\n  enabled: []\nother: 1\n")
            install.enable_plugin(config, True)
            self.assertEqual(config.read_text(), "plugins:\n  enabled:\n  - hermes-jev\nother: 1\n")
            config.write_text("other: 1\n")
            install.enable_plugin(config, True)
            self.assertIn("plugins:\n  enabled:\n  - hermes-jev", config.read_text())

    def test_full_install_links_every_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "hermes"
            for home in (root, root / "profiles" / "alpha"):
                home.mkdir(parents=True)
                (home / "config.yaml").write_text(CONFIG)
            report = install.install_hermes(root, "alpha", check=False)
            self.assertTrue((root / "plugins" / "hermes-jev" / "jevkit" / "client.py").is_file())
            self.assertTrue((root / "profiles" / "alpha" / "plugins" / "hermes-jev").is_symlink())
            self.assertTrue((root / "profiles" / "alpha" / "skills" / "jev" / "jev-setup" / "SKILL.md").is_file())
            self.assertEqual(report["enabled_in"], ["alpha: enabled"])
            self.assertNotIn("hermes-jev", (root / "config.yaml").read_text())
            install.uninstall_hermes(root)
            self.assertFalse((root / "profiles" / "alpha" / "plugins" / "hermes-jev").exists())
            self.assertEqual((root / "profiles" / "alpha" / "config.yaml").read_text(), CONFIG)


if __name__ == "__main__":
    unittest.main()
