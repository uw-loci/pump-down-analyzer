from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import pumpdown_analyzer as analyzer


WORKSPACE = Path(__file__).resolve().parents[1]
DEFAULT_REAL_LOG = WORKSPACE / "inputs" / "log_2026-07-17_17-54-01.txt"
REAL_LOG = Path(os.environ.get("PUMPDOWN_TEST_LOG", DEFAULT_REAL_LOG)).expanduser()


class EquipmentStateTests(unittest.TestCase):
    def test_documented_msb_first_mapping(self) -> None:
        states = analyzer.decode_equipment_states("10010010")
        active = {label for label, value in zip(analyzer.STATE_LABELS, states) if value}
        self.assertEqual(
            active,
            {"Pumps Power ON", "Relay 1 CLOSED", "Argon Gate OPEN"},
        )

    def test_short_values_are_normalized_to_eight_positions(self) -> None:
        states = analyzer.decode_equipment_states("1")
        self.assertEqual(
            {label for label, value in zip(analyzer.STATE_LABELS, states) if value},
            {"Argon Gate CLOSED"},
        )


class RealLogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not REAL_LOG.is_file():
            raise unittest.SkipTest(
                "Real-log regression fixture is not available. Set PUMPDOWN_TEST_LOG "
                "to run these optional integration checks."
            )
        cls.result = analyzer.parse_log(REAL_LOG)

    def test_pressure_baselines(self) -> None:
        self.assertEqual(len(self.result.readings), 15_567)
        self.assertAlmostEqual(self.result.minimum_reading.pressure_mbar, 9.26e-5)
        self.assertEqual(self.result.minimum_reading.timestamp, datetime(2026, 7, 17, 20, 5, 10))
        self.assertAlmostEqual(self.result.maximum_reading.pressure_mbar, 1_200.0)

    def test_initial_named_states(self) -> None:
        first = self.result.state_snapshots[0]
        active = {label for label, value in zip(analyzer.STATE_LABELS, first.states) if value}
        self.assertEqual(active, {"Turbo Gate CLOSED", "Argon Gate CLOSED"})

    def test_named_transition_count_and_representative_changes(self) -> None:
        transitions = [event for event in self.result.events if event.category == "Equipment state"]
        self.assertEqual(len(transitions), 26)
        signatures = {(event.timestamp.time().isoformat(), event.state_name, event.action) for event in transitions}
        self.assertIn(("18:11:06", "Pumps Power ON", "Deactivated"), signatures)
        self.assertIn(("18:12:09", "Turbo Vent OPEN", "Activated"), signatures)
        self.assertIn(("19:28:22", "Relay 1 CLOSED", "Activated"), signatures)

    def test_threshold_and_pressure_rise_detection(self) -> None:
        crossing = next(item for threshold, item in self.result.thresholds if math_close(threshold, 1e-4))
        self.assertEqual(crossing.timestamp, datetime(2026, 7, 17, 19, 28, 23))
        self.assertTrue(
            any(
                rise.start_timestamp <= datetime(2026, 7, 17, 18, 12, 18) <= rise.end_timestamp
                for rise in self.result.rises
            )
        )
        self.assertTrue(
            any(
                rise.start_timestamp <= datetime(2026, 7, 17, 20, 13, 33) <= rise.end_timestamp
                for rise in self.result.rises
            )
        )

    def test_public_text_outputs_have_named_states_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            pressure = directory / "pressure.csv"
            events = directory / "events.csv"
            equipment = directory / "equipment.csv"
            summary = directory / "summary.md"
            analyzer.write_pressure_csv(self.result, pressure)
            analyzer.write_events_csv(self.result, events)
            analyzer.write_equipment_csv(self.result, equipment)
            summary.write_text(analyzer.build_summary_markdown(self.result), encoding="utf-8")
            encoded_state_pattern = re.compile(r"(?<![A-Za-z0-9])[01]{8}(?![A-Za-z0-9])")
            for path in (pressure, events, equipment, summary):
                self.assertIsNone(encoded_state_pattern.search(path.read_text(encoding="utf-8-sig")), path.name)


class SyntheticLogTests(unittest.TestCase):
    def test_midnight_rollover_and_duplicate_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "log_2026-07-17_midnight.txt"
            path.write_text(
                "[23:59:59] - VERBOSE (VTRX) > VTRX States: 00000001\n"
                "[23:59:59] - VERBOSE (VTRX) > GUI updated with pressure: 1.00E+0 mbar\n"
                "[23:59:59] - VERBOSE (VTRX) > GUI updated with pressure: 9.00E-1 mbar\n"
                "[00:00:00] - VERBOSE (VTRX) > GUI updated with pressure: 8.00E-1 mbar\n",
                encoding="utf-8",
            )
            result = analyzer.parse_log(path)
            self.assertEqual(result.readings[1].sample_in_second, 2)
            self.assertEqual(result.readings[-1].timestamp, datetime(2026, 7, 18, 0, 0, 0))

    def test_cli_runs_from_a_renamed_repository_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            renamed_repository = temporary_path / "pump-down-analyzer"
            renamed_repository.mkdir()
            shutil.copy2(WORKSPACE / "pumpdown_analyzer.py", renamed_repository)
            shutil.copy2(WORKSPACE / "viewer_template.html", renamed_repository)

            log_directory = temporary_path / "new experiment logs"
            log_directory.mkdir()
            log_path = log_directory / "log_2030-01-02_smoke.txt"
            log_path.write_text(
                "[12:00:00] - VERBOSE (VTRX) > VTRX States: 00001001\n"
                "[12:00:00] - VERBOSE (VTRX) > GUI updated with pressure: 1.00E+0 mbar\n"
                "[12:00:01] - VERBOSE (VTRX) > GUI updated with pressure: 5.00E-1 mbar\n"
                "[12:00:02] - VERBOSE (VTRX) > VTRX States: 10001001\n"
                "[12:00:02] - VERBOSE (VTRX) > GUI updated with pressure: 2.00E-1 mbar\n",
                encoding="utf-8",
            )
            output_directory = temporary_path / "analysis results"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(renamed_repository / "pumpdown_analyzer.py"),
                    str(log_path),
                    "--out",
                    str(output_directory),
                ],
                cwd=temporary_path,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
            expected_suffixes = (
                "_pressure.csv",
                "_events.csv",
                "_equipment_states.csv",
                "_analysis.xlsx",
                "_summary.md",
                "_viewer.html",
            )
            for suffix in expected_suffixes:
                self.assertTrue(
                    (output_directory / f"{log_path.stem}{suffix}").is_file(),
                    suffix,
                )


def math_close(left: float, right: float) -> bool:
    return abs(left - right) <= max(abs(left), abs(right), 1.0) * 1e-12


if __name__ == "__main__":
    unittest.main()
