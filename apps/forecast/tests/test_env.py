"""Settings from .env, and the layering between the apps."""

import ast
import os
import tempfile
import unittest
from importlib import util
from pathlib import Path

from scout_forecast import env


class DotenvTest(unittest.TestCase):
    def setUp(self):
        self.saved = dict(os.environ)
        self.saved_dir, self.saved_keys = env._dotenv_dir, set(env._dotenv_keys)
        self.saved_searched = env._searched

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.saved)
        env._dotenv_dir, env._dotenv_keys = self.saved_dir, self.saved_keys
        env._searched = self.saved_searched

    def _dotenv(self, text):
        d = tempfile.mkdtemp()
        f = Path(d) / ".env"
        f.write_text(text)
        return f

    def test_shell_wins_and_relative_csv_is_beside_dotenv(self):
        os.environ.pop(env.CSV_ENV, None)
        os.environ["SCOUT_TEST_SHELL"] = "shell"
        f = self._dotenv(
            "# comment\nexport SCOUT_TEST_SHELL=file\n" f"{env.CSV_ENV}='data/t.csv'\n"
        )
        env.load_dotenv(f)
        self.assertEqual(os.environ["SCOUT_TEST_SHELL"], "shell")
        self.assertEqual(env.csv_default(), str(f.parent.resolve() / "data/t.csv"))

    def test_relative_csv_from_shell_is_left_alone(self):
        os.environ[env.CSV_ENV] = "rel.csv"
        env.load_dotenv(self._dotenv(f"{env.CSV_ENV}=other.csv\n"))
        self.assertEqual(env.csv_default(), "rel.csv")


class LayeringTest(unittest.TestCase):
    """scout-forecast is the base: it must not import the apps built on it."""

    def test_no_upward_imports(self):
        pkg = Path(util.find_spec("scout_forecast").origin).parent
        banned = ("scout_lowpower", "scout_web", "scout_sim", "model")
        for f in pkg.glob("*.py"):
            for node in ast.walk(ast.parse(f.read_text())):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    names = [node.module or ""]
                for n in names:
                    self.assertFalse(n.split(".")[0] in banned, f"{f.name}: {n}")


if __name__ == "__main__":
    unittest.main()
