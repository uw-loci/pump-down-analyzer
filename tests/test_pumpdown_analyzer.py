from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree

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
            self.assertEqual(result.source_sha256, hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertEqual(len(result.source_logs), 1)

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


class ChainedLogTests(unittest.TestCase):
    def _build_chain(self, root: Path) -> Path:
        directory = root / "2030-01-02 pump down"
        directory.mkdir()
        # Create the files in a deliberately non-chronological order.
        (directory / "log_2030-01-03_00-00-10.txt").write_text(
            "[00:00:10] - VERBOSE (VTRX) > VTRX States: 10000001\n"
            "[00:00:10] - VERBOSE (VTRX) > GUI updated with pressure: 6.00E-1 mbar\n",
            encoding="utf-8",
        )
        (directory / "log_2030-01-02_23-59-58.txt").write_text(
            "[23:59:58] - VERBOSE (VTRX) > VTRX States: 00000001\n"
            "[23:59:59] - VERBOSE (VTRX) > GUI updated with pressure: 1.00E+0 mbar\n"
            "[00:00:00] - VERBOSE (VTRX) > GUI updated with pressure: 9.00E-1 mbar\n",
            encoding="utf-8",
        )
        (directory / "log_2030-01-03_00-00-02.txt").write_text(
            "[00:00:02] - INFO (Main) > Process launch\n"
            "[00:00:03] - INFO (Dashboard) > Dashboard updates cancelled.\n",
            encoding="utf-8",
        )
        (directory / "log_2030-01-03_00-00-00.txt").write_text(
            "[00:00:00] - INFO (Utils) > Log file created\n"
            "[00:00:00] - VERBOSE (VTRX) > VTRX States: 00000001\n"
            "[00:00:00] - VERBOSE (VTRX) > GUI updated with pressure: 8.00E-1 mbar\n"
            "[00:00:01] - VERBOSE (VTRX) > VTRX States: 10000001\n"
            "[00:00:01] - VERBOSE (VTRX) > GUI updated with pressure: 7.00E-1 mbar\n",
            encoding="utf-8",
        )
        (directory / "notes.txt").write_text("ignored", encoding="utf-8")
        return directory

    def test_directory_chain_orders_fragments_and_preserves_continuity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = self._build_chain(Path(temporary))
            paths, source_path, experiment_name = analyzer.resolve_input_logs(directory)
            self.assertEqual(
                [path.name for path in paths],
                [
                    "log_2030-01-02_23-59-58.txt",
                    "log_2030-01-03_00-00-00.txt",
                    "log_2030-01-03_00-00-02.txt",
                    "log_2030-01-03_00-00-10.txt",
                ],
            )
            result = analyzer.parse_logs(
                paths,
                source_path=source_path,
                experiment_name=experiment_name,
            )

            self.assertEqual(result.experiment_name, directory.name)
            self.assertEqual(len(result.source_logs), 4)
            self.assertEqual([item.pressure_reading_count for item in result.source_logs], [2, 2, 0, 1])
            self.assertEqual(len(result.readings), 5)
            self.assertEqual(result.readings[1].timestamp, datetime(2030, 1, 3, 0, 0, 0))
            self.assertEqual(result.readings[2].timestamp, datetime(2030, 1, 3, 0, 0, 0))
            self.assertEqual(result.readings[2].sample_in_second, 2)
            self.assertEqual(result.readings[2].source_log, "log_2030-01-03_00-00-00.txt")
            self.assertEqual(result.last_reading.elapsed_seconds, 11)

            transitions = [event for event in result.events if event.category == "Equipment state"]
            self.assertEqual(len(transitions), 1)
            self.assertEqual(transitions[0].source_log, "log_2030-01-03_00-00-00.txt")
            gaps = [event for event in result.events if event.category == "Pressure data gap"]
            self.assertEqual(len(gaps), 1)
            self.assertEqual(gaps[0].message, "No pressure readings for 9 seconds")
            self.assertNotEqual(result.source_sha256, result.source_logs[0].sha256)
            self.assertEqual(
                result.source_sha256,
                analyzer.parse_logs(paths, source_path=source_path, experiment_name=experiment_name).source_sha256,
            )

    def test_chain_provenance_is_exported_to_every_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = self._build_chain(root)
            paths, source_path, experiment_name = analyzer.resolve_input_logs(directory)
            result = analyzer.parse_logs(paths, source_path=source_path, experiment_name=experiment_name)

            pressure_csv = root / "pressure.csv"
            equipment_csv = root / "equipment.csv"
            events_csv = root / "events.csv"
            workbook_path = root / "analysis.xlsx"
            analyzer.write_pressure_csv(result, pressure_csv)
            analyzer.write_equipment_csv(result, equipment_csv)
            analyzer.write_events_csv(result, events_csv)
            analyzer.write_workbook(result, workbook_path)

            with pressure_csv.open(encoding="utf-8-sig", newline="") as handle:
                pressure_rows = list(csv.DictReader(handle))
            self.assertEqual(pressure_rows[0]["source_log"], "log_2030-01-02_23-59-58.txt")
            with equipment_csv.open(encoding="utf-8-sig", newline="") as handle:
                self.assertIn("source_log", next(csv.reader(handle)))
            with events_csv.open(encoding="utf-8-sig", newline="") as handle:
                self.assertIn("source_log", next(csv.reader(handle)))

            namespace = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
            with zipfile.ZipFile(workbook_path) as workbook:
                shared_root = ElementTree.fromstring(workbook.read("xl/sharedStrings.xml"))
                shared_strings = [
                    "".join(node.text or "" for node in item.findall(".//x:t", namespace))
                    for item in shared_root.findall("x:si", namespace)
                ]

                def worksheet_values(sheet_number: int, first_row_only: bool = False) -> list[str]:
                    root_node = ElementTree.fromstring(
                        workbook.read(f"xl/worksheets/sheet{sheet_number}.xml")
                    )
                    cells = root_node.findall(".//x:sheetData/x:row[@r='1']/x:c", namespace)
                    if not first_row_only:
                        cells = root_node.findall(".//x:sheetData/x:row/x:c", namespace)
                    values: list[str] = []
                    for cell in cells:
                        value = cell.find("x:v", namespace)
                        if value is not None and cell.get("t") == "s":
                            values.append(shared_strings[int(value.text)])
                    return values

                self.assertIn("Source Log", worksheet_values(2, first_row_only=True))
                self.assertIn("Source Log", worksheet_values(3, first_row_only=True))
                self.assertIn("Source Log", worksheet_values(4, first_row_only=True))
                self.assertIn("Ordered source log manifest", worksheet_values(1))

            payload = analyzer.build_viewer_payload(result, analyzer.build_summary_markdown(result))
            self.assertEqual(payload["schema_version"], 2)
            self.assertEqual(len(payload["source_logs"]), 4)
            self.assertEqual(payload["sample_meta"][0][1], "log_2030-01-02_23-59-58.txt")
            self.assertEqual(payload["events"][0]["source_log"], "log_2030-01-03_00-00-00.txt")

            annotations = root / "annotations.json"
            annotations.write_text(
                json.dumps(
                    {
                        "source_log_sha256": result.source_sha256,
                        "annotations": [
                            {
                                "id": "note-1",
                                "kind": "point",
                                "start_timestamp_local": "2030-01-03T00:00:01",
                                "note": "Boundary checked",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            annotated = analyzer.parse_logs(paths, annotations_path=annotations)
            self.assertEqual(len(annotated.annotations), 1)
            self.assertFalse(any("different source fingerprint" in item for item in annotated.warnings))

    def test_chain_validation_errors_are_actionable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            empty = root / "empty"
            empty.mkdir()
            with self.assertRaisesRegex(ValueError, "No non-recursive"):
                analyzer.resolve_input_logs(empty)

            malformed = root / "malformed"
            malformed.mkdir()
            (malformed / "log_bad.txt").write_text("[12:00:00] - INFO (Main) > Process launch\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "log_YYYY-MM-DD_HH-MM-SS"):
                analyzer.resolve_input_logs(malformed)
            with self.assertRaisesRegex(ValueError, "--date can only"):
                analyzer.resolve_input_logs(malformed, explicit_date="2030-01-01")

            no_pressure = root / "no pressure"
            no_pressure.mkdir()
            only = no_pressure / "log_2030-01-01_12-00-00.txt"
            only.write_text("[12:00:00] - INFO (Main) > Process launch\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "No pressure readings"):
                analyzer.parse_logs([only])

            overlap = root / "overlap"
            overlap.mkdir()
            first = overlap / "log_2030-01-01_12-00-00.txt"
            second = overlap / "log_2030-01-01_12-00-05.txt"
            first.write_text(
                "[12:00:00] - VERBOSE (VTRX) > VTRX States: 00000001\n"
                "[12:00:10] - VERBOSE (VTRX) > GUI updated with pressure: 1.00E+0 mbar\n",
                encoding="utf-8",
            )
            second.write_text(
                "[12:00:05] - VERBOSE (VTRX) > GUI updated with pressure: 9.00E-1 mbar\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "overlap or are out of order"):
                analyzer.parse_logs([second, first])

    def test_cli_uses_directory_name_for_chained_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = self._build_chain(root)
            output = root / "outputs"
            completed = subprocess.run(
                [sys.executable, str(WORKSPACE / "pumpdown_analyzer.py"), str(directory), "--out", str(output)],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
            self.assertIn("5 pressure readings from 4 source log(s)", completed.stdout)
            for suffix in (
                "_pressure.csv",
                "_events.csv",
                "_equipment_states.csv",
                "_analysis.xlsx",
                "_summary.md",
                "_viewer.html",
            ):
                self.assertTrue((output / f"{directory.name}{suffix}").is_file(), suffix)

    def test_viewer_extrema_do_not_spread_large_reading_arrays(self) -> None:
        template = (WORKSPACE / "viewer_template.html").read_text(encoding="utf-8")
        self.assertNotRegex(template, r"Math\.(?:min|max)\(\.\.\.")


def math_close(left: float, right: float) -> bool:
    return abs(left - right) <= max(abs(left), abs(right), 1.0) * 1e-12


if __name__ == "__main__":
    unittest.main()
