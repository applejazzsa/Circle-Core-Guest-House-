import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from core.management.commands.backup_local import Command


class BackupLocalPermissionsTests(SimpleTestCase):
    @mock.patch("core.management.commands.backup_local.subprocess.run")
    @mock.patch.object(Path, "chmod")
    def test_backup_artifacts_are_owner_readable_only(self, chmod, run):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            output = root / "backups"
            media = root / "media"
            media.mkdir()
            (media / "logo.png").write_bytes(b"example")

            def create_fake_dump(command, **kwargs):
                dump_argument = next(item for item in command if item.startswith("--file="))
                Path(dump_argument.removeprefix("--file=")).write_bytes(b"pg_dump")
                return SimpleNamespace(returncode=0, stderr="")

            run.side_effect = create_fake_dump

            with self.settings(MEDIA_ROOT=media):
                Command().handle(output=str(output))

            dump = next(output.glob("circlecore-db-*.dump"))
            media_archive = next(output.glob("circlecore-media-*.zip"))

            self.assertTrue(dump.exists())
            self.assertTrue(media_archive.exists())
            self.assertEqual(chmod.call_args_list, [mock.call(0o600), mock.call(0o600)])
