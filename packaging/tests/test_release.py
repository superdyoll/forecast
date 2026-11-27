"""Release tooling that runs on every merge into main: the lockstep version
bump (packaging/version.py) and the deb builder's generated pieces
(packaging/deb/build_debs.py). Standard library only; checks that need
dpkg-deb or shellcheck skip where those aren't installed."""

import importlib.util
import io
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

PACKAGING = Path(__file__).resolve().parent.parent


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Version(unittest.TestCase):
    """version.py on a throwaway copy of the repository layout."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "packaging").mkdir()
        shutil.copy(PACKAGING / "version.py", root / "packaging/version.py")
        for app, deps in (("forecast", []), ("lowpower", ["forecast"])):
            src = root / "apps" / app / "src" / f"scout_{app}"
            src.mkdir(parents=True)
            (src / "__init__.py").write_text(f'"""{app}."""\n\n__version__ = "0.1.4"\n')
            pins = ", ".join(f'"scout-{d}==0.1.4"' for d in deps)
            (root / "apps" / app / "pyproject.toml").write_text(
                f'[project]\nname = "scout-{app}"\ndependencies = [{pins}]\n'
            )
        self.root = root
        self.v = load(root / "packaging/version.py", "version_under_test")

    def tearDown(self):
        self.tmp.cleanup()

    def test_bumps(self):
        b = self.v.bumped
        self.assertEqual(b("0.1.9", "patch"), "0.1.10")
        self.assertEqual(b("0.1.9", "minor"), "0.2.0")
        self.assertEqual(b("1.9.3", "major"), "2.0.0")
        with self.assertRaises(SystemExit):
            b("0.1.9", "build")

    def test_bump_rewrites_every_version_and_pin(self):
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(self.v.main(["bump", "patch"]), 0)
        self.assertEqual(out.getvalue(), "0.1.5\n")
        self.assertEqual(set(self.v.versions().values()), {"0.1.5"})
        init = (self.root / "apps/lowpower/src/scout_lowpower/__init__.py").read_text()
        self.assertIn('"""lowpower."""', init)  # the rest of the file kept
        toml = (self.root / "apps/lowpower/pyproject.toml").read_text()
        self.assertIn('"scout-forecast==0.1.5"', toml)
        self.assertIn('name = "scout-lowpower"', toml)

    def test_out_of_step_versions_are_refused(self):
        init = self.root / "apps/forecast/src/scout_forecast/__init__.py"
        init.write_text('__version__ = "0.2.0"\n')
        with self.assertRaises(SystemExit) as e:
            self.v.current()
        self.assertIn("versions differ", str(e.exception))
        with redirect_stdout(io.StringIO()):
            self.v.main(["set", "0.3.0"])  # the documented way out
        self.assertEqual(self.v.current(), "0.3.0")

    def test_bad_versions_are_refused(self):
        for bad in ("1.0", "v1.0.0", "1.0.0rc1"):
            with self.subTest(bad), self.assertRaises(SystemExit):
                self.v.write(bad)

    def test_repository_is_in_step(self):
        real = load(PACKAGING / "version.py", "version_real")
        self.assertRegex(real.current(), r"^\d+\.\d+\.\d+$")


class Debs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.b = load(PACKAGING / "deb/build_debs.py", "build_debs_under_test")

    def scripts(self):
        for name, spec in self.b.PACKAGES.items():
            for script, text in self.b.maintainer_scripts(name, spec).items():
                yield f"{name}.{script}", text

    def test_maintainer_scripts_are_valid_posix_shell(self):
        for label, text in self.scripts():
            with self.subTest(label):
                self.assertTrue(text.startswith("#!/bin/sh\nset -e\n"))
                self.assertTrue(text.endswith("exit 0\n"))
                r = subprocess.run(
                    ["sh", "-n"], input=text, text=True, capture_output=True
                )
                self.assertEqual(r.returncode, 0, r.stderr)

    @unittest.skipUnless(shutil.which("shellcheck"), "shellcheck not installed")
    def test_maintainer_scripts_pass_shellcheck(self):
        for label, text in self.scripts():
            with self.subTest(label):
                r = subprocess.run(
                    ["shellcheck", "-s", "sh", "-"],
                    input=text,
                    text=True,
                    capture_output=True,
                )
                self.assertEqual(r.returncode, 0, r.stdout)

    def test_systemd_calls_are_guarded(self):
        # Installing in a chroot or a container without systemd must work.
        for label, text in self.scripts():
            for line in text.splitlines():
                if (
                    "systemctl" in line
                    and "enable" not in line
                    and "disable" not in line
                ):
                    with self.subTest(label, line=line):
                        self.assertTrue(
                            "have_systemd" in line or "|| true" in line, line
                        )

    def test_units_are_enabled_on_first_install_and_restarted_on_upgrade(self):
        post = self.b.maintainer_scripts(
            "scout-lowpower", self.b.PACKAGES["scout-lowpower"]
        )
        self.assertIn('if [ -z "$2" ]; then', post["postinst"])  # first install
        self.assertIn("systemctl enable scout-lowpower.service", post["postinst"])
        self.assertIn("systemctl try-restart scout-lowpower.service", post["postinst"])
        self.assertIn("rm -rf /var/lib/scout-lowpower", post["postrm"])
        self.assertIn('if [ "$1" = purge ]', post["postrm"])

    def test_package_set_is_pinned_together(self):
        P = self.b.PACKAGES
        self.assertEqual(
            set(P),
            {"scout", "scout-forecast", "scout-lowpower", "scout-web", "scout-sim"},
        )
        for name, spec in P.items():
            for dep in spec.get("depends", []):
                if dep.startswith("scout"):
                    self.assertIn("(= {v})", dep, f"{name}: {dep}")
        self.assertNotIn(
            "scout-sim (= {v})", P["scout"]["depends"]
        )  # never on the boat
        for name, spec in P.items():
            for src in spec.get("files", {}).values():
                if isinstance(src, Path):
                    self.assertTrue(src.exists(), f"{name}: {src}")
            for unit in spec.get("units", []):
                self.assertTrue((PACKAGING / "systemd" / unit).exists(), unit)

    def test_launcher_runs_the_entry_point(self):
        text = self.b.launcher("json.tool:main")
        self.assertTrue(text.startswith("#!/usr/bin/python3 -I\n"))
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "launch"
            f.write_text(text.replace("#!/usr/bin/python3 -I", f"#!{sys.executable}"))
            r = subprocess.run(
                [sys.executable, str(f)], input="[1]", text=True, capture_output=True
            )
            self.assertEqual(r.stdout.split(), ["[", "1", "]"])

    @unittest.skipUnless(shutil.which("dpkg-deb"), "dpkg-deb not installed")
    def test_apt_index_hashes_match_the_files(self):
        import hashlib

        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            stage = out / "stage"
            (stage / "DEBIAN").mkdir(parents=True)
            (stage / "DEBIAN/control").write_text(
                "Package: t\nVersion: 1.0\nArchitecture: all\nMaintainer: x <x@x>\n"
                "Description: t\n"
            )
            subprocess.run(
                ["dpkg-deb", "--build", str(stage), str(out / "t_1.0_all.deb")],
                check=True,
                capture_output=True,
            )
            self.b.apt_index(out)
            packages = (out / "Packages").read_text()
            deb = (out / "t_1.0_all.deb").read_bytes()
            self.assertIn(f"SHA256: {hashlib.sha256(deb).hexdigest()}", packages)
            release = (out / "Release").read_text()
            gz = (out / "Packages.gz").read_bytes()
            self.assertIn(hashlib.sha256(gz).hexdigest(), release)


if __name__ == "__main__":
    unittest.main()
