from pathlib import Path
import tempfile
import unittest

from crypto_trader.data.upload import build_rclone_command


class TickUploadTests(unittest.TestCase):
    def test_build_rclone_copy_is_non_destructive_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            command = build_rclone_command(
                source=tmp_path,
                remote="gdrive:coinbase-data/raw",
                rclone_binary="/usr/bin/rclone",
            )

            self.assertEqual(
                command[:4],
                [
                    "/usr/bin/rclone",
                    "copy",
                    str(tmp_path),
                    "gdrive:coinbase-data/raw",
                ],
            )
            self.assertIn("--immutable", command)
            self.assertIn("--min-age", command)

    def test_build_rclone_move_requires_explicit_flag(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            command = build_rclone_command(
                source=Path(directory),
                remote="gdrive:coinbase-data/raw",
                move=True,
                minimum_age_minutes=0,
            )
            self.assertEqual(command[1], "move")
            self.assertNotIn("--min-age", command)
            self.assertIn("--delete-empty-src-dirs", command)
            self.assertNotIn("--include", command)
            self.assertNotIn("--exclude", command)
            self.assertEqual(
                [
                    command[index + 1]
                    for index, item in enumerate(command)
                    if item == "--filter"
                ],
                [
                    "+ **/*.csv.gz",
                    "+ **/*.csv.gz.manifest.json",
                    "- **",
                ],
            )

    def test_build_rclone_rejects_invalid_remote(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "remote"):
                build_rclone_command(source=Path(directory), remote="not-a-remote")


if __name__ == "__main__":
    unittest.main()
