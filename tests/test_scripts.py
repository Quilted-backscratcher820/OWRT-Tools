from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from core.scripts import ScriptError, stage_build_script, verify_staged_script


class ScriptTests(unittest.TestCase):
    def test_script_is_converted_checked_authorized_and_hashed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "Settings.sh"
            source.write_bytes(b"#!/bin/bash\r\nprintf 'ok\\n'\r\n")
            staged = root / "staged"
            spec = stage_build_script(source, staged)
            target = verify_staged_script(staged, spec)
            self.assertNotIn(b"\r", target.read_bytes())
            self.assertEqual(target.stat().st_mode & 0o777, 0o755)
            self.assertRegex(spec.filename, r"^[0-9a-f]{16}-Settings\.sh$")

            target.write_text("#!/bin/bash\necho changed\n", encoding="utf-8")
            with self.assertRaisesRegex(ScriptError, "校验失败"):
                verify_staged_script(staged, spec)

    def test_invalid_shell_syntax_is_rejected_without_staged_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "broken.sh"
            source.write_text("#!/bin/bash\nif then\n", encoding="utf-8")
            staged = root / "staged"
            with self.assertRaisesRegex(ScriptError, "语法校验失败"):
                stage_build_script(source, staged)
            self.assertFalse(any(path for path in staged.iterdir() if not path.name.startswith(".")))
