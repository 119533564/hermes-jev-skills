"""Guard the two agent-facing skills against drift.

These are the parts a fleet depends on when it points its AGENTS.md gate at
`jev-computer-use` / `jev-browser-use`:

- the fleet note pointer, so runtime facts have exactly one home,
- the withdrawn preview schema named as incompatible,
- the path rule that a fleet can make Jev Ultrafast the required default,
- no machine-specific paths leaking into the public repo.
"""
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SKILLS = REPO / "skills"
FLEET_NOTE = "shared/rules/jev-computer-use-fleet.md"


def read(name: str) -> str:
    return (SKILLS / name / "SKILL.md").read_text(encoding="utf-8")


class SkillContentTests(unittest.TestCase):
    def test_every_skill_has_matching_frontmatter_name(self):
        for skill_md in sorted(SKILLS.glob("*/SKILL.md")):
            body = skill_md.read_text(encoding="utf-8")
            match = re.search(r"^name:\s*(\S+)\s*$", body, re.MULTILINE)
            if match is None:
                self.fail(f"{skill_md} has no name: in frontmatter")
            self.assertEqual(match.group(1), skill_md.parent.name)

    def test_computer_use_points_at_the_fleet_note(self):
        body = read("jev-computer-use")
        self.assertIn(FLEET_NOTE, body)
        self.assertIn("Managed fleets", body)

    def test_computer_use_names_the_withdrawn_schema(self):
        body = read("jev-computer-use")
        self.assertIn("hermes.cua_jev_choice_request_v1", body)
        self.assertIn("jev.action_choice_request_v1", body)
        self.assertIn("jev-latest", body)

    def test_browser_use_points_at_the_fleet_note_and_path_rule(self):
        body = read("jev-browser-use")
        self.assertIn(FLEET_NOTE, body)
        self.assertIn("Managed fleets", body)
        self.assertIn("required default", body)

    def test_no_machine_specific_paths_in_skills(self):
        for skill_md in sorted(SKILLS.glob("*/SKILL.md")):
            body = skill_md.read_text(encoding="utf-8")
            self.assertIsNone(
                re.search(r"/Users/[a-z][a-z0-9_-]+/|/home/[a-z][a-z0-9_-]+/", body),
                f"{skill_md} carries a machine-specific path",
            )


if __name__ == "__main__":
    unittest.main()
