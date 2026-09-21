"""Offline checks for the public skill's billing and credential safeguards."""

import importlib.util
import io
import json
import os
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "skills/image-generation/scripts/generate_image.py"
SPEC = importlib.util.spec_from_file_location("generate_image", SCRIPT)
generate_image = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(generate_image)


class GenerateImageSafetyTests(unittest.TestCase):
    def test_generation_requires_spend_confirmation_before_api_setup(self):
        with mock.patch.object(generate_image, "load_env"), mock.patch.object(
            sys, "argv", ["generate_image.py", "a garden", "--tier", "final"]
        ), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                generate_image.main()
        self.assertEqual(caught.exception.code, 2)

    def test_ultra_requires_separate_confirmation(self):
        with mock.patch.object(generate_image, "load_env"), mock.patch.object(
            sys,
            "argv",
            ["generate_image.py", "a garden", "--tier", "ultra", "--confirm-spend"],
        ), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                generate_image.main()
        self.assertEqual(caught.exception.code, 2)

    def test_pinned_model_cannot_bypass_tier(self):
        generate_image.validate_tier_model("final", "gemini-3.1-flash-image")
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            generate_image.validate_tier_model("final", "gemini-3-pro-image")

    def test_ultra_dry_run_needs_no_spend_flag_and_uses_cwd(self):
        fake_google = types.ModuleType("google")
        fake_genai = types.ModuleType("google.genai")
        fake_genai.Client = lambda **kwargs: object()
        fake_genai.types = types.SimpleNamespace(HttpOptions=lambda **kwargs: object())
        fake_google.genai = fake_genai
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            generate_image, "load_env"
        ), mock.patch.object(
            generate_image, "resolve_models", return_value={"ultra": "gemini-3-pro-image"}
        ), mock.patch.dict(
            sys.modules, {"google": fake_google, "google.genai": fake_genai}
        ), mock.patch.dict(
            os.environ, {"GEMINI_API_KEY": "test-only", "IMAGE_GEN_OUTPUT_ROOT": ""}
        ), mock.patch.object(
            Path, "cwd", return_value=Path(temp_dir)
        ), mock.patch.object(
            sys, "argv", ["generate_image.py", "a garden", "--tier", "ultra", "--dry-run"]
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(generate_image.main(), 0)
            plan = json.loads(output.getvalue())
            expected = Path(temp_dir) / "generated-images"
            self.assertEqual(Path(plan["out_dir"]).parent, expected)
            self.assertFalse(expected.exists())

    def test_api_error_redacts_key(self):
        key = "AIza" + "A" * 30
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": key}):
            message = generate_image.api_message(RuntimeError(f"request failed: key={key}"))
        self.assertNotIn(key, message)
        self.assertIn("[REDACTED]", message)

    def test_redacts_foreign_keys_of_both_formats(self):
        """A key the caller did not configure must still never reach the terminal."""
        for other in ("AIza" + "B" * 30, "AQ.Ab8" + "C" * 25):
            with self.subTest(key=other), mock.patch.dict(
                os.environ, {"GEMINI_API_KEY": "unrelated-configured-key"}
            ):
                message = generate_image.api_message(RuntimeError(f"denied for {other}"))
            self.assertNotIn(other, message)
            self.assertIn("[REDACTED]", message)

    def test_extra_image_in_one_response_is_saved_next_to_o(self):
        """A response carrying more images than -o names must not crash after billing."""
        fake_google = types.ModuleType("google")
        fake_genai = types.ModuleType("google.genai")
        fake_genai.Client = lambda **kwargs: object()
        fake_genai.types = types.SimpleNamespace(HttpOptions=lambda **kwargs: object())
        fake_google.genai = fake_genai
        png = bytes.fromhex("89504e470d0a1a0a")
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            generate_image, "load_env"
        ), mock.patch.object(
            generate_image, "resolve_models", return_value={"final": "gemini-3.1-flash-image"}
        ), mock.patch.object(
            generate_image, "generate_once",
            return_value=([(png, "image/png"), (png, "image/png")], []),
        ), mock.patch.dict(
            sys.modules, {"google": fake_google, "google.genai": fake_genai}
        ), mock.patch.dict(
            os.environ, {"GEMINI_API_KEY": "test-only"}
        ), mock.patch.object(
            sys, "argv",
            ["generate_image.py", "a garden", "--tier", "final", "--confirm-spend",
             "--no-meta", "-o", str(Path(temp_dir) / "out.png")],
        ):
            output = io.StringIO()
            with redirect_stdout(output), redirect_stderr(io.StringIO()):
                self.assertEqual(generate_image.main(), 0)
            saved = [Path(f["path"]).name for f in json.loads(output.getvalue())["files"]]
        self.assertEqual(saved, ["out.png", "out_01.png"])

    def test_exported_key_wins_over_env_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env_file = Path(temp_dir) / ".env"
            env_file.write_text("GEMINI_API_KEY=file-value\n", encoding="utf-8")
            with mock.patch.dict(
                os.environ,
                {"GEMINI_ENV_FILE": str(env_file), "GEMINI_API_KEY": "exported-value"},
                clear=True,
            ):
                generate_image.load_env()
                self.assertEqual(os.environ["GEMINI_API_KEY"], "exported-value")


if __name__ == "__main__":
    unittest.main()
