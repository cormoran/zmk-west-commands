import platform
import shutil
import subprocess
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent
BUILD_DIR = REPO_ROOT.parent / "build"


def run_west(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["west", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )


class WestCommandsTests(unittest.TestCase):
    @unittest.skipUnless(
        platform.system() == "Linux", "zmk-test is only supported on Linux"
    )
    def test_zmk_test_runs_and_logs_results(self):
        tests_build = BUILD_DIR / "tests"
        if tests_build.exists():
            shutil.rmtree(tests_build)

        result = run_west(["zmk-test", "tests"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS: test1", result.stdout)
        self.assertIn("PASS: test2", result.stdout)

        pass_log = tests_build / "pass-fail.log"
        self.assertTrue(pass_log.exists(), "pass-fail.log should be generated")
        log_content = pass_log.read_text()
        for name in ("test1", "test2"):
            self.assertIn(f"PASS: {name}", log_content)

    def test_zmk_build_generates_expected_configs(self):
        artifacts = [
            "with_logging",
            "studio",
            "nice_nano_v2__corne_left",
            "nice_nano_v2__corne_right",
            "seeeduino_xiao_ble__tester_xiao",
        ]
        for artifact in artifacts:
            shutil.rmtree(BUILD_DIR / artifact, ignore_errors=True)

        result = run_west(["zmk-build", "tests/build_yaml"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        expected_config_entries = {
            "with_logging": ["CONFIG_ZMK_USB_LOGGING=y"],
            "studio": ["CONFIG_ZMK_STUDIO=y", "CONFIG_USB_CDC_ACM=y"],
            "nice_nano_v2__corne_left": [
                "CONFIG_BOARD_NICE_NANO_V2=y",
                "CONFIG_SHIELD_CORNE_LEFT=y",
            ],
            "nice_nano_v2__corne_right": [
                "CONFIG_BOARD_NICE_NANO_V2=y",
                "CONFIG_SHIELD_CORNE_RIGHT=y",
            ],
            "seeeduino_xiao_ble__tester_xiao": [
                "CONFIG_BOARD_SEEEDUINO_XIAO_BLE=y",
                "CONFIG_SHIELD_TESTER_XIAO=y",
            ],
        }

        for artifact, entries in expected_config_entries.items():
            config_path = BUILD_DIR / artifact / "zephyr" / ".config"
            self.assertTrue(config_path.exists(), f"{artifact} .config is missing")
            config_text = config_path.read_text()
            for entry in entries:
                self.assertIn(entry, config_text, f"{entry} not found in {artifact}")

    def test_zmk_build_no_yaml_cli_targets_no_run(self):
        artifact_dir = BUILD_DIR / "seeeduino_xiao_ble__tester_xiao"
        shutil.rmtree(artifact_dir, ignore_errors=True)

        result = run_west(
            [
                "zmk-build",
                "tests/test1",
                "-b",
                "seeeduino_xiao_ble",
                "-s",
                "tester_xiao",
                "-n",
                "-q",
            ]
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(
            artifact_dir.exists(), "no-run should not create build outputs"
        )

    def test_zmk_build_fails_without_targets(self):
        result = run_west(["zmk-build", "tests/test1", "-n"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No build targets found", result.stdout + result.stderr)

    def test_zmk_build_with_zmk_config(self):
        result = run_west(
            ["zmk-build", "tests/zmk-config/config", "-m", "tests/zmk-config", "-q"]
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        artifact = "seeeduino_xiao_ble__my_awesome_keyboard"
        config_path = BUILD_DIR / artifact / "zephyr" / ".config"
        self.assertTrue(config_path.exists(), f"{artifact} .config is missing")
        config_text = config_path.read_text()
        for entry in [
            "CONFIG_SHIELD_MY_AWESOME_KEYBOARD=y",
            "CONFIG_MY_AWESOME_KEYBOARD_SPECIAL_FEATURE=y",
        ]:
            self.assertIn(entry, config_text, f"{entry} not found in {artifact}")


if __name__ == "__main__":
    unittest.main()
