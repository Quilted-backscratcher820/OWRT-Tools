from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest

from support.check_requirements import dependency_entries


class InstallerTests(unittest.TestCase):
    def test_dependency_checker_ignores_comments_and_keeps_alternatives(self) -> None:
        root = Path(__file__).resolve().parent.parent
        entries = dependency_entries(root / "support" / "dependencies.txt")
        self.assertIn("libfreetype6-dev|libfreetype-dev", entries)
        self.assertNotIn("#", entries)
        self.assertNotIn("PySide6", entries)

    def test_system_setup_runs_full_apt_sequence(self) -> None:
        root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as temporary:
            fake = Path(temporary)
            log = fake / "apt.log"
            scripts = {
                "apt-get": "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$FAKE_APT_LOG\"\n",
                "dpkg-query": "#!/bin/sh\nprintf '%s' 'install ok installed'\n",
                "sudo": "#!/bin/sh\nexec \"$@\"\n",
            }
            for name, content in scripts.items():
                path = fake / name
                path.write_text(content, encoding="ascii")
                path.chmod(path.stat().st_mode | stat.S_IXUSR)
            environment = os.environ.copy()
            environment["PATH"] = f"{fake}:{environment['PATH']}"
            environment["FAKE_APT_LOG"] = str(log)
            result = subprocess.run(
                ["sh", str(root / "support" / "system_setup.sh")],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            commands = log.read_text(encoding="ascii").splitlines()
            self.assertEqual(commands[0], "update -y")
            self.assertEqual(commands[1], "full-upgrade -y")
            self.assertTrue(commands[2].startswith("install -y "))
            self.assertEqual(commands[-3:], ["autoremove --purge -y", "autoclean -y", "clean -y"])
