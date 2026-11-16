import os
import tempfile
import unittest
from pathlib import Path, PurePosixPath

from sre import prepare_openclaw_staging as staging


@unittest.skipUnless(os.name == "posix", "Descriptores y O_NOFOLLOW de Linux")
class OpenClawAutomaticMemoryTests(unittest.TestCase):
    def setUp(self):
        temp = self.enterContext(tempfile.TemporaryDirectory())
        self.home = PurePosixPath(temp)
        self.editor = Path(temp) / "agency-editor"
        self.editor.mkdir(mode=0o700)
        (self.editor / "memory" / "dreaming").mkdir(parents=True)
        (self.editor / "memory").chmod(0o775)
        self.entries = {name: {"model": model} for name, model in staging.AGENTS.items()}
        self.entries["agency-editor"]["workspace"] = str(self.editor)

    def test_copies_project_memory_and_daily_notes(self):
        notes = {"MEMORY.md": b"Duradera", "memory/2026-11-15-guion.md": b"Nota del guion"}
        generated = {"memory/dreaming/2026-11-15.md": b"Derivada", "USER.md": b"Perfil"}
        for name, content in (notes | generated).items():
            (self.editor / name).write_bytes(content)
        (self.editor / "memory" / "2026-11-15-guion.md").chmod(0o664)
        inventory = staging.discover_memory(self.entries, self.home)
        _, files = staging.project(
            self.entries, inventory, "agency-state", self.home, staging.read_workspace_memory
        )
        self.assertEqual(len(files), 7)
        for name, content in notes.items():
            self.assertEqual(files[f"agency-editor/{name}"], content)

    def test_refuses_a_memory_symlink(self):
        secret = Path(self.home) / "out-of-scope.txt"
        secret.write_bytes(b"Synthetic out-of-scope data")
        (self.editor / "MEMORY.md").symlink_to(secret)
        with self.assertRaises(OSError):
            staging.discover_memory(self.entries, self.home)

    def test_rejects_a_workspace_shared_with_another_team_before_reading(self):
        self.entries["other-team"] = {"workspace": str(self.editor)}
        with self.assertRaisesRegex(ValueError, "superponen"):
            staging.discover_memory(self.entries, self.home)

    def test_requires_private_workspace_before_accepting_group_write(self):
        self.editor.chmod(0o755)
        with self.assertRaisesRegex(ValueError, "privado"):
            staging.discover_memory(self.entries, self.home)
