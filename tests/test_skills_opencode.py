import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from token_dashboard.skills import scan_catalog


class OpencodeSkillsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_opencode_config_skill_root_scanned(self):
        skill = self.tmp / ".config" / "opencode" / "skill" / "my-skill" / "SKILL.md"
        skill.parent.mkdir(parents=True, exist_ok=True)
        skill.write_text("x" * 400, encoding="utf-8")
        with patch.object(Path, "home", return_value=self.tmp):
            cat = scan_catalog()
        self.assertIn("my-skill", cat)
        self.assertEqual(cat["my-skill"]["chars"], 400)
        self.assertEqual(cat["my-skill"]["tokens"], 100)

    def test_agents_skills_root_scanned(self):
        skill = self.tmp / ".agents" / "skills" / "another-skill" / "SKILL.md"
        skill.parent.mkdir(parents=True, exist_ok=True)
        skill.write_text("y" * 200, encoding="utf-8")
        with patch.object(Path, "home", return_value=self.tmp):
            cat = scan_catalog()
        self.assertIn("another-skill", cat)
        self.assertEqual(cat["another-skill"]["tokens"], 50)


if __name__ == "__main__":
    unittest.main()
