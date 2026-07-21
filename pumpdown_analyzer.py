#!/usr/bin/env python3
"""Extract, analyze, and visualize pressure history from EBEAM dashboard logs.

The command accepts one log or one directory of chained logs and produces CSV
files, a native Excel workbook, a factual Markdown summary, and a self-contained
offline HTML viewer with point/range annotations.
VTRX switch values are decoded immediately into named Boolean equipment states;
the encoded state value is never written to a public artifact.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import hashlib
import json
import math
import re
import sys
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Iterable, Optional, Sequence


# VTRX serializes these inputs MSB-first. Position 4 is connected through the
# optocoupler to the 972B Relay 1 normally-open contact, so HIGH means closed.
STATE_DEFINITIONS: tuple[tuple[str, str], ...] = (
    ("pumps_power_on", "Pumps Power ON"),
    ("turbo_rotor_on", "Turbo Rotor ON"),
    ("turbo_vent_open", "Turbo Vent OPEN"),
    ("relay_1_closed", "Relay 1 CLOSED"),
    ("turbo_gate_closed", "Turbo Gate CLOSED"),
    ("turbo_gate_open", "Turbo Gate OPEN"),
    ("argon_gate_open", "Argon Gate OPEN"),
    ("argon_gate_closed", "Argon Gate CLOSED"),
)
STATE_KEYS = tuple(item[0] for item in STATE_DEFINITIONS)
STATE_LABELS = tuple(item[1] for item in STATE_DEFINITIONS)

LOG_LINE_RE = re.compile(
    r"^\[(?P<clock>\d{2}:\d{2}:\d{2})\]\s+-\s+"
    r"(?P<level>[A-Z]+)\s+\((?P<source>[^)]+)\)\s+>\s+(?P<message>.*)$"
)
PRESSURE_RE = re.compile(
    r"^GUI updated with pressure:\s*"
    r"(?P<value>[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?)\s+"
    r"(?P<unit>\S+)\s*$"
)
STATE_RE = re.compile(r"^VTRX States:\s*(?P<state>[01]{1,8})\s*$")
DATE_IN_FILENAME_RE = re.compile(r"log_(?P<date>\d{4}-\d{2}-\d{2})(?:_|\b)", re.IGNORECASE)
CHAIN_LOG_FILENAME_RE = re.compile(
    r"^log_(?P<date>\d{4}-\d{2}-\d{2})_(?P<clock>\d{2}-\d{2}-\d{2})\.txt$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PressureReading:
    sample_index: int
    timestamp: datetime
    elapsed_seconds: float
    pressure_mbar: Optional[float]
    raw_pressure: str
    raw_unit: str
    sample_in_second: int
    source_log: str
    line_number: int
    quality_flag: str
    states: tuple[Optional[bool], ...]

    @property
    def active_state_summary(self) -> str:
        active = [label for label, value in zip(STATE_LABELS, self.states) if value is True]
        return "; ".join(active) if active else "None active"


@dataclass(frozen=True)
class StateSnapshot:
    timestamp: datetime
    states: tuple[bool, ...]
    source_log: str
    line_number: int


@dataclass
class Event:
    timestamp: datetime
    category: str
    message: str
    level: str = "INFO"
    source: str = "Analyzer"
    source_log: str = ""
    line_number: Optional[int] = None
    end_timestamp: Optional[datetime] = None
    state_name: str = ""
    action: str = ""
    pressure_mbar: Optional[float] = None
    origin: str = "automatic"
    event_id: str = ""


@dataclass(frozen=True)
class PressureRise:
    start_timestamp: datetime
    end_timestamp: datetime
    start_pressure_mbar: float
    peak_pressure_mbar: float
    factor: float
    nearby_state_changes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Annotation:
    annotation_id: str
    kind: str
    start_timestamp: datetime
    end_timestamp: Optional[datetime]
    category: str
    note: str


@dataclass(frozen=True)
class SourceLog:
    path: Path
    sha256: str
    log_start: Optional[datetime]
    log_end: Optional[datetime]
    pressure_reading_count: int


@dataclass
class AnalysisResult:
    source_path: Path
    source_logs: tuple[SourceLog, ...]
    experiment_name: str
    source_sha256: str
    inferred_date: date
    log_start: datetime
    log_end: datetime
    readings: list[PressureReading]
    state_snapshots: list[StateSnapshot]
    events: list[Event]
    rises: list[PressureRise]
    thresholds: list[tuple[float, PressureReading]]
    warnings: list[str] = field(default_factory=list)
    annotations: list[Annotation] = field(default_factory=list)

    @property
    def first_reading(self) -> PressureReading:
        return self.readings[0]

    @property
    def last_reading(self) -> PressureReading:
        return self.readings[-1]

    @property
    def valid_readings(self) -> list[PressureReading]:
        return [item for item in self.readings if item.pressure_mbar is not None and item.pressure_mbar > 0]

    @property
    def minimum_reading(self) -> PressureReading:
        return min(self.valid_readings, key=lambda item: (item.pressure_mbar, item.sample_index))

    @property
    def maximum_reading(self) -> PressureReading:
        return max(self.valid_readings, key=lambda item: (item.pressure_mbar, -item.sample_index))

    @property
    def duration_seconds(self) -> float:
        return (self.last_reading.timestamp - self.first_reading.timestamp).total_seconds()


def decode_equipment_states(encoded_state: str) -> tuple[bool, ...]:
    """Decode an MSB-first VTRX value into the eight named indicator states."""

    value = encoded_state.strip()
    if not value or len(value) > 8 or any(char not in "01" for char in value):
        raise ValueError("VTRX equipment state must contain one to eight binary digits")
    normalized = value.zfill(8)
    return tuple(char == "1" for char in normalized)


def infer_log_date(path: Path, explicit_date: Optional[str] = None) -> date:
    if explicit_date:
        try:
            return date.fromisoformat(explicit_date)
        except ValueError as exc:
            raise ValueError("--date must use YYYY-MM-DD") from exc
    match = DATE_IN_FILENAME_RE.search(path.name)
    if not match:
        raise ValueError(
            f"Could not infer a date from {path.name!r}; provide --date YYYY-MM-DD"
        )
    return date.fromisoformat(match.group("date"))


def _chain_filename_timestamp(path: Path) -> datetime:
    match = CHAIN_LOG_FILENAME_RE.fullmatch(path.name)
    if not match:
        raise ValueError(
            f"Chained log filename must use log_YYYY-MM-DD_HH-MM-SS.txt: {path.name}"
        )
    try:
        return datetime.strptime(
            f"{match.group('date')}_{match.group('clock')}", "%Y-%m-%d_%H-%M-%S"
        )
    except ValueError as exc:
        raise ValueError(f"Invalid timestamp in chained log filename: {path.name}") from exc


def resolve_input_logs(
    input_path: Path, explicit_date: Optional[str] = None
) -> tuple[list[Path], Path, str]:
    """Resolve one file or one experiment directory into ordered source logs."""

    resolved = input_path.expanduser().resolve()
    if resolved.is_file():
        return [resolved], resolved, resolved.stem
    if not resolved.exists():
        raise FileNotFoundError(f"Input path not found: {resolved}")
    if not resolved.is_dir():
        raise ValueError(f"Input path is neither a file nor a directory: {resolved}")
    if explicit_date:
        raise ValueError("--date can only be used when analyzing a single log file")

    candidates = sorted(
        (path.resolve() for path in resolved.glob("log_*.txt") if path.is_file()),
        key=lambda path: path.name.lower(),
    )
    if not candidates:
        raise ValueError(f"No non-recursive log_*.txt files were found in {resolved}")
    ordered = sorted(candidates, key=lambda path: (_chain_filename_timestamp(path), path.name.lower()))
    return ordered, resolved, resolved.name


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _source_fingerprint(source_hashes: Sequence[str]) -> str:
    if len(source_hashes) == 1:
        return source_hashes[0]
    digest = hashlib.sha256()
    digest.update(b"pumpdown-analyzer-chain-v1\0")
    for source_hash in source_hashes:
        digest.update(bytes.fromhex(source_hash))
    return digest.hexdigest()


def _classify_relevant_event(source: str, message: str) -> Optional[str]:
    if source == "SIC" and "Interlock Vacuum" in message:
        return "Vacuum interlock"
    if source == "Machine Status" and "Pressure Below" in message:
        return "Pressure status"
    if source == "VTRX":
        lowered = message.lower()
        phrases = (
            "no data",
            "incomplete data",
            "recovered",
            "serial connection established",
            "closed serial port",
            "data processing error",
        )
        if any(phrase in lowered for phrase in phrases):
            return "VTRX/data"
    return None


def _nearest_pressure(readings: Sequence[PressureReading], timestamp: datetime) -> Optional[float]:
    if not readings:
        return None
    stamps = [item.timestamp for item in readings]
    position = bisect.bisect_left(stamps, timestamp)
    candidates = []
    if position < len(readings):
        candidates.append(readings[position])
    if position:
        candidates.append(readings[position - 1])
    candidates = [item for item in candidates if item.pressure_mbar is not None]
    if not candidates:
        return None
    closest = min(candidates, key=lambda item: abs((item.timestamp - timestamp).total_seconds()))
    return closest.pressure_mbar


def _nearest_reading(
    readings: Sequence[PressureReading], timestamp: datetime
) -> Optional[PressureReading]:
    if not readings:
        return None
    stamps = [item.timestamp for item in readings]
    position = bisect.bisect_left(stamps, timestamp)
    candidates = []
    if position < len(readings):
        candidates.append(readings[position])
    if position:
        candidates.append(readings[position - 1])
    return min(candidates, key=lambda item: abs((item.timestamp - timestamp).total_seconds()))


def _detect_pressure_rises(
    readings: Sequence[PressureReading],
    factor_threshold: float = 100.0,
    window_seconds: float = 60.0,
    merge_seconds: float = 120.0,
) -> list[PressureRise]:
    valid = [item for item in readings if item.pressure_mbar is not None and item.pressure_mbar > 0]
    window: deque[PressureReading] = deque()
    candidates: list[PressureRise] = []

    for current in valid:
        cutoff = current.timestamp - timedelta(seconds=window_seconds)
        while window and window[0].timestamp < cutoff:
            window.popleft()
        window.append(current)
        minimum = min(window, key=lambda item: item.pressure_mbar)
        factor = current.pressure_mbar / minimum.pressure_mbar
        if factor >= factor_threshold and current.timestamp > minimum.timestamp:
            candidates.append(
                PressureRise(
                    start_timestamp=minimum.timestamp,
                    end_timestamp=current.timestamp,
                    start_pressure_mbar=minimum.pressure_mbar,
                    peak_pressure_mbar=current.pressure_mbar,
                    factor=factor,
                )
            )

    episodes: list[PressureRise] = []
    for candidate in candidates:
        if not episodes or candidate.start_timestamp > episodes[-1].end_timestamp + timedelta(seconds=merge_seconds):
            episodes.append(candidate)
            continue
        previous = episodes[-1]
        start_pressure = min(previous.start_pressure_mbar, candidate.start_pressure_mbar)
        peak_pressure = max(previous.peak_pressure_mbar, candidate.peak_pressure_mbar)
        episodes[-1] = PressureRise(
            start_timestamp=min(previous.start_timestamp, candidate.start_timestamp),
            end_timestamp=max(previous.end_timestamp, candidate.end_timestamp),
            start_pressure_mbar=start_pressure,
            peak_pressure_mbar=peak_pressure,
            factor=peak_pressure / start_pressure,
        )
    return episodes


def _first_decade_crossings(readings: Sequence[PressureReading]) -> list[tuple[float, PressureReading]]:
    valid = [item for item in readings if item.pressure_mbar is not None and item.pressure_mbar > 0]
    if not valid:
        return []
    maximum = max(item.pressure_mbar for item in valid)
    minimum = min(item.pressure_mbar for item in valid)
    maximum_exponent = math.floor(math.log10(maximum))
    minimum_exponent = math.floor(math.log10(minimum))
    crossings: list[tuple[float, PressureReading]] = []
    for exponent in range(maximum_exponent, minimum_exponent, -1):
        threshold = 10.0**exponent
        first = next((item for item in valid if item.pressure_mbar < threshold), None)
        if first is not None:
            crossings.append((threshold, first))
    return crossings


def _load_annotations(path: Optional[Path], expected_hash: str) -> tuple[list[Annotation], list[str]]:
    if path is None:
        return [], []
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    warnings: list[str] = []
    if isinstance(payload, dict):
        supplied_hash = payload.get("source_log_sha256")
        if supplied_hash and supplied_hash != expected_hash:
            warnings.append("Imported annotations were created for a different source fingerprint.")
        records = payload.get("annotations", [])
    elif isinstance(payload, list):
        records = payload
    else:
        raise ValueError("Annotation JSON must be an object or array")

    annotations: list[Annotation] = []
    for index, record in enumerate(records, start=1):
        if not isinstance(record, dict):
            raise ValueError(f"Annotation {index} is not an object")
        kind = str(record.get("kind", "point")).lower()
        if kind not in {"point", "range"}:
            raise ValueError(f"Annotation {index} has unsupported kind {kind!r}")
        start_text = record.get("start_timestamp_local") or record.get("start_timestamp")
        if not start_text:
            raise ValueError(f"Annotation {index} is missing a start timestamp")
        start = datetime.fromisoformat(str(start_text))
        end_text = record.get("end_timestamp_local") or record.get("end_timestamp")
        end = datetime.fromisoformat(str(end_text)) if end_text else None
        if kind == "range" and end is None:
            raise ValueError(f"Range annotation {index} is missing an end timestamp")
        if end is not None and end < start:
            start, end = end, start
        annotations.append(
            Annotation(
                annotation_id=str(record.get("id") or record.get("annotation_id") or f"imported-{index}"),
                kind=kind,
                start_timestamp=start,
                end_timestamp=end,
                category=str(record.get("category") or "Observation"),
                note=str(record.get("note") or "").strip(),
            )
        )
    return annotations, warnings


def parse_log(
    log_path: Path,
    explicit_date: Optional[str] = None,
    annotations_path: Optional[Path] = None,
) -> AnalysisResult:
    resolved = log_path.expanduser().resolve()
    return parse_logs(
        [resolved],
        explicit_date=explicit_date,
        annotations_path=annotations_path,
        source_path=resolved,
        experiment_name=resolved.stem,
    )


def parse_logs(
    log_paths: Sequence[Path],
    explicit_date: Optional[str] = None,
    annotations_path: Optional[Path] = None,
    source_path: Optional[Path] = None,
    experiment_name: Optional[str] = None,
) -> AnalysisResult:
    paths = [Path(path).expanduser().resolve() for path in log_paths]
    if not paths:
        raise ValueError("At least one log file is required")
    if len(paths) > 1:
        if explicit_date:
            raise ValueError("--date can only be used when analyzing a single log file")
        paths.sort(key=lambda path: (_chain_filename_timestamp(path), path.name.lower()))
    for path in paths:
        if not path.is_file():
            raise FileNotFoundError(f"Log file not found: {path}")

    selected_source = (source_path or (paths[0] if len(paths) == 1 else paths[0].parent)).resolve()
    selected_name = experiment_name or (paths[0].stem if len(paths) == 1 else selected_source.name)
    base_date = infer_log_date(paths[0], explicit_date)
    source_hashes = [_file_sha256(path) for path in paths]
    source_hash = _source_fingerprint(source_hashes)
    annotations, annotation_warnings = _load_annotations(annotations_path, source_hash)

    readings: list[PressureReading] = []
    snapshots: list[StateSnapshot] = []
    events: list[Event] = []
    source_logs: list[SourceLog] = []
    warnings = list(annotation_warnings)
    current_states: Optional[tuple[bool, ...]] = None
    sample_counts: dict[datetime, int] = {}
    log_start: Optional[datetime] = None
    log_end: Optional[datetime] = None
    previous_source_end: Optional[datetime] = None
    malformed_pressure_count = 0
    malformed_state_count = 0

    for log_path, file_hash in zip(paths, source_hashes):
        file_base_date = infer_log_date(log_path, explicit_date if len(paths) == 1 else None)
        day_offset = 0
        previous_clock: Optional[time] = None
        file_start: Optional[datetime] = None
        file_end: Optional[datetime] = None
        reading_count_before = len(readings)

        with log_path.open("r", encoding="utf-8", errors="replace") as handle:
            for line_number, raw_line in enumerate(handle, start=1):
                match = LOG_LINE_RE.match(raw_line.rstrip("\r\n"))
                if not match:
                    continue
                clock = time.fromisoformat(match.group("clock"))
                if previous_clock is not None:
                    prior_seconds = previous_clock.hour * 3600 + previous_clock.minute * 60 + previous_clock.second
                    current_seconds = clock.hour * 3600 + clock.minute * 60 + clock.second
                    if prior_seconds - current_seconds > 12 * 3600:
                        day_offset += 1
                previous_clock = clock
                timestamp = datetime.combine(file_base_date + timedelta(days=day_offset), clock)
                if file_start is None:
                    if previous_source_end is not None and timestamp < previous_source_end:
                        raise ValueError(
                            f"Chained logs overlap or are out of order: {log_path.name} starts at "
                            f"{timestamp:%Y-%m-%d %H:%M:%S}, before the preceding log ended at "
                            f"{previous_source_end:%Y-%m-%d %H:%M:%S}"
                        )
                    file_start = timestamp
                file_end = timestamp
                log_start = timestamp if log_start is None else log_start
                log_end = timestamp
                level = match.group("level")
                source = match.group("source")
                message = match.group("message")

                state_match = STATE_RE.match(message) if source == "VTRX" else None
                if state_match:
                    try:
                        new_states = decode_equipment_states(state_match.group("state"))
                    except ValueError:
                        malformed_state_count += 1
                        continue
                    if current_states is None or new_states != current_states:
                        snapshots.append(StateSnapshot(timestamp, new_states, log_path.name, line_number))
                        if current_states is not None:
                            for index, (old, new) in enumerate(zip(current_states, new_states)):
                                if old == new:
                                    continue
                                action = "Activated" if new else "Deactivated"
                                state_name = STATE_LABELS[index]
                                events.append(
                                    Event(
                                        timestamp=timestamp,
                                        category="Equipment state",
                                        message=f"{state_name} {action.lower()}",
                                        level="INFO",
                                        source="VTRX",
                                        source_log=log_path.name,
                                        line_number=line_number,
                                        state_name=state_name,
                                        action=action,
                                    )
                                )
                        current_states = new_states
                    continue
                if source == "VTRX" and message.startswith("VTRX States:"):
                    malformed_state_count += 1
                    continue

                pressure_match = PRESSURE_RE.match(message) if source == "VTRX" else None
                if pressure_match:
                    raw_value = pressure_match.group("value")
                    raw_unit = pressure_match.group("unit")
                    value = float(raw_value)
                    pressure_mbar: Optional[float]
                    if raw_unit.lower() != "mbar":
                        pressure_mbar = None
                        quality = f"Unsupported unit: {raw_unit}"
                    elif value <= 0:
                        pressure_mbar = value
                        quality = "Nonpositive pressure"
                    elif current_states is None:
                        pressure_mbar = value
                        quality = "Missing equipment state"
                    else:
                        pressure_mbar = value
                        quality = "OK"
                    sample_in_second = sample_counts.get(timestamp, 0) + 1
                    sample_counts[timestamp] = sample_in_second
                    states: tuple[Optional[bool], ...]
                    states = current_states if current_states is not None else (None,) * len(STATE_LABELS)
                    first_timestamp = readings[0].timestamp if readings else timestamp
                    readings.append(
                        PressureReading(
                            sample_index=len(readings) + 1,
                            timestamp=timestamp,
                            elapsed_seconds=(timestamp - first_timestamp).total_seconds(),
                            pressure_mbar=pressure_mbar,
                            raw_pressure=raw_value,
                            raw_unit=raw_unit,
                            sample_in_second=sample_in_second,
                            source_log=log_path.name,
                            line_number=line_number,
                            quality_flag=quality,
                            states=states,
                        )
                    )
                    continue
                if source == "VTRX" and message.startswith("GUI updated with pressure:"):
                    malformed_pressure_count += 1
                    continue

                category = _classify_relevant_event(source, message)
                if category:
                    events.append(
                        Event(
                            timestamp=timestamp,
                            category=category,
                            message=message,
                            level=level,
                            source=source,
                            source_log=log_path.name,
                            line_number=line_number,
                        )
                    )

        source_logs.append(
            SourceLog(
                path=log_path,
                sha256=file_hash,
                log_start=file_start,
                log_end=file_end,
                pressure_reading_count=len(readings) - reading_count_before,
            )
        )
        if file_end is not None:
            previous_source_end = file_end

    if not readings:
        noun = "log" if len(paths) == 1 else "chained logs"
        raise ValueError(f"No pressure readings were found in the {noun}")
    if log_start is None or log_end is None:
        raise ValueError("No timestamped log records were found")
    if malformed_pressure_count:
        warnings.append(f"Skipped {malformed_pressure_count} malformed pressure record(s).")
    if malformed_state_count:
        warnings.append(f"Skipped {malformed_state_count} malformed equipment-state record(s).")

    # Detect internal and trailing pressure-data gaps.
    for previous, current in zip(readings, readings[1:]):
        gap = (current.timestamp - previous.timestamp).total_seconds()
        if gap > 5:
            events.append(
                Event(
                    timestamp=previous.timestamp + timedelta(seconds=1),
                    end_timestamp=current.timestamp,
                    category="Pressure data gap",
                    message=f"No pressure readings for {gap:.0f} seconds",
                    level="WARNING",
                )
            )
    trailing_gap = (log_end - readings[-1].timestamp).total_seconds()
    if trailing_gap > 5:
        events.append(
            Event(
                timestamp=readings[-1].timestamp + timedelta(seconds=1),
                end_timestamp=log_end,
                category="Pressure data gap",
                message=f"Pressure data ended {trailing_gap:.0f} seconds before the log ended",
                level="WARNING",
            )
        )

    rises = _detect_pressure_rises(readings)
    state_events = [item for item in events if item.category == "Equipment state"]
    enriched_rises: list[PressureRise] = []
    for rise in rises:
        nearby = [
            f"{item.timestamp:%H:%M:%S}: {item.state_name} {item.action.lower()}"
            for item in state_events
            if rise.start_timestamp - timedelta(seconds=180)
            <= item.timestamp
            <= rise.end_timestamp + timedelta(seconds=180)
        ]
        enriched = PressureRise(
            start_timestamp=rise.start_timestamp,
            end_timestamp=rise.end_timestamp,
            start_pressure_mbar=rise.start_pressure_mbar,
            peak_pressure_mbar=rise.peak_pressure_mbar,
            factor=rise.factor,
            nearby_state_changes=tuple(nearby),
        )
        enriched_rises.append(enriched)
        events.append(
            Event(
                timestamp=rise.start_timestamp,
                end_timestamp=rise.end_timestamp,
                category="Major pressure rise",
                message=(
                    f"Pressure increased from {rise.start_pressure_mbar:.3E} to "
                    f"{rise.peak_pressure_mbar:.3E} mbar ({rise.factor:,.0f}x)"
                ),
                level="WARNING",
            )
        )

    for annotation in annotations:
        events.append(
            Event(
                timestamp=annotation.start_timestamp,
                end_timestamp=annotation.end_timestamp,
                category=annotation.category,
                message=annotation.note,
                level="NOTE",
                source="Manual annotation",
                origin="manual",
            )
        )

    for event in events:
        event.pressure_mbar = _nearest_pressure(readings, event.timestamp)
        if not event.source_log and event.origin != "manual":
            nearest = _nearest_reading(readings, event.timestamp)
            if nearest is not None:
                event.source_log = nearest.source_log
    events.sort(key=lambda item: (item.timestamp, item.category, item.state_name, item.message))
    for index, event in enumerate(events, start=1):
        event.event_id = f"E{index:04d}"

    thresholds = _first_decade_crossings(readings)
    return AnalysisResult(
        source_path=selected_source,
        source_logs=tuple(source_logs),
        experiment_name=selected_name,
        source_sha256=source_hash,
        inferred_date=base_date,
        log_start=log_start,
        log_end=log_end,
        readings=readings,
        state_snapshots=snapshots,
        events=events,
        rises=enriched_rises,
        thresholds=thresholds,
        warnings=warnings,
        annotations=annotations,
    )


def build_active_state_intervals(result: AnalysisResult) -> list[dict[str, object]]:
    intervals: list[dict[str, object]] = []
    if not result.state_snapshots:
        return intervals
    run_end = result.last_reading.timestamp
    for state_index, state_name in enumerate(STATE_LABELS):
        active_start: Optional[datetime] = None
        active_source_log = ""
        active_line: Optional[int] = None
        for snapshot in result.state_snapshots:
            active = snapshot.states[state_index]
            if active and active_start is None:
                active_start = snapshot.timestamp
                active_source_log = snapshot.source_log
                active_line = snapshot.line_number
            elif not active and active_start is not None:
                intervals.append(
                    {
                        "state_index": state_index,
                        "state_name": state_name,
                        "start_timestamp": active_start,
                        "end_timestamp": snapshot.timestamp,
                        "duration_seconds": max(0.0, (snapshot.timestamp - active_start).total_seconds()),
                        "source_log": active_source_log,
                        "source_line_number": active_line,
                    }
                )
                active_start = None
                active_source_log = ""
                active_line = None
        if active_start is not None:
            intervals.append(
                {
                    "state_index": state_index,
                    "state_name": state_name,
                    "start_timestamp": active_start,
                    "end_timestamp": run_end,
                    "duration_seconds": max(0.0, (run_end - active_start).total_seconds()),
                    "source_log": active_source_log,
                    "source_line_number": active_line,
                }
            )
    intervals.sort(key=lambda item: (item["start_timestamp"], item["state_index"]))
    return intervals


def _format_duration(seconds: float) -> str:
    whole = int(round(seconds))
    hours, remainder = divmod(whole, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:d}:{minutes:02d}:{seconds:02d}"


def _format_pressure(value: Optional[float]) -> str:
    return "" if value is None else f"{value:.6E}"


def write_pressure_csv(result: AnalysisResult, output_path: Path) -> None:
    fieldnames = [
        "sample_index",
        "timestamp_local",
        "elapsed_seconds",
        "elapsed_minutes",
        "pressure_mbar",
        "pressure_raw",
        "unit_raw",
        "sample_in_second",
        "source_log",
        "log_line_number",
        "quality_flag",
        "active_equipment_states",
        *STATE_KEYS,
    ]
    with output_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for item in result.readings:
            row = {
                "sample_index": item.sample_index,
                "timestamp_local": item.timestamp.isoformat(timespec="seconds"),
                "elapsed_seconds": f"{item.elapsed_seconds:.0f}",
                "elapsed_minutes": f"{item.elapsed_seconds / 60:.6f}",
                "pressure_mbar": _format_pressure(item.pressure_mbar),
                "pressure_raw": item.raw_pressure,
                "unit_raw": item.raw_unit,
                "sample_in_second": item.sample_in_second,
                "source_log": item.source_log,
                "log_line_number": item.line_number,
                "quality_flag": item.quality_flag,
                "active_equipment_states": item.active_state_summary,
            }
            for key, state in zip(STATE_KEYS, item.states):
                row[key] = "" if state is None else ("TRUE" if state else "FALSE")
            writer.writerow(row)


def write_events_csv(result: AnalysisResult, output_path: Path) -> None:
    fieldnames = [
        "event_id",
        "timestamp_local",
        "end_timestamp_local",
        "elapsed_seconds",
        "category",
        "equipment_state",
        "action",
        "level",
        "source",
        "message",
        "pressure_mbar_at_event",
        "source_log",
        "log_line_number",
        "origin",
    ]
    first = result.first_reading.timestamp
    with output_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for event in result.events:
            writer.writerow(
                {
                    "event_id": event.event_id,
                    "timestamp_local": event.timestamp.isoformat(timespec="seconds"),
                    "end_timestamp_local": event.end_timestamp.isoformat(timespec="seconds") if event.end_timestamp else "",
                    "elapsed_seconds": f"{(event.timestamp - first).total_seconds():.0f}",
                    "category": event.category,
                    "equipment_state": event.state_name,
                    "action": event.action,
                    "level": event.level,
                    "source": event.source,
                    "message": event.message,
                    "pressure_mbar_at_event": _format_pressure(event.pressure_mbar),
                    "source_log": event.source_log,
                    "log_line_number": event.line_number or "",
                    "origin": event.origin,
                }
            )


def write_equipment_csv(result: AnalysisResult, output_path: Path) -> None:
    fieldnames = [
        "timestamp_local",
        "elapsed_seconds",
        "equipment_state",
        "status",
        "record_type",
        "source_log",
        "source_line_number",
    ]
    first = result.first_reading.timestamp
    rows: list[dict[str, object]] = []
    if result.state_snapshots:
        initial = result.state_snapshots[0]
        for label, active in zip(STATE_LABELS, initial.states):
            rows.append(
                {
                    "timestamp_local": initial.timestamp.isoformat(timespec="seconds"),
                    "elapsed_seconds": f"{(initial.timestamp - first).total_seconds():.0f}",
                    "equipment_state": label,
                    "status": "Active" if active else "Inactive",
                    "record_type": "Initial state",
                    "source_log": initial.source_log,
                    "source_line_number": initial.line_number,
                }
            )
    for event in result.events:
        if event.category != "Equipment state":
            continue
        rows.append(
            {
                "timestamp_local": event.timestamp.isoformat(timespec="seconds"),
                "elapsed_seconds": f"{(event.timestamp - first).total_seconds():.0f}",
                "equipment_state": event.state_name,
                "status": "Active" if event.action == "Activated" else "Inactive",
                "record_type": "Transition",
                "source_log": event.source_log,
                "source_line_number": event.line_number or "",
            }
        )
    with output_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def build_summary_markdown(result: AnalysisResult) -> str:
    minimum = result.minimum_reading
    maximum = result.maximum_reading
    lines = [
        "# Pump-Down Anomaly Summary",
        "",
        f"- Experiment: `{result.experiment_name}`",
        f"- Source logs: {len(result.source_logs):,}",
        f"- Source fingerprint SHA-256: `{result.source_sha256}`",
        f"- Pressure samples: {len(result.readings):,}",
        f"- Pressure-history span: {_format_duration(result.duration_seconds)} "
        f"({result.first_reading.timestamp:%Y-%m-%d %H:%M:%S} to {result.last_reading.timestamp:%Y-%m-%d %H:%M:%S})",
        f"- Reported pressure range: {minimum.pressure_mbar:.3E} to {maximum.pressure_mbar:.3E} mbar",
        f"- Minimum pressure: {minimum.pressure_mbar:.3E} mbar at {minimum.timestamp:%Y-%m-%d %H:%M:%S}",
        "",
        "## Source log manifest",
        "",
        "| # | Source log | SHA-256 | Log span | Pressure samples |",
        "|---:|---|---|---|---:|",
    ]
    for index, source_log in enumerate(result.source_logs, start=1):
        span = "No timestamped records"
        if source_log.log_start is not None and source_log.log_end is not None:
            span = (
                f"{source_log.log_start:%Y-%m-%d %H:%M:%S} to "
                f"{source_log.log_end:%Y-%m-%d %H:%M:%S}"
            )
        lines.append(
            f"| {index} | `{source_log.path.name}` | `{source_log.sha256}` | "
            f"{span} | {source_log.pressure_reading_count:,} |"
        )
    lines.extend(
        [
            "",
            "## First readings below decade thresholds",
            "",
            "| Threshold (mbar) | First timestamp | Reading (mbar) |",
            "|---:|---|---:|",
        ]
    )
    for threshold, item in result.thresholds:
        lines.append(f"| {threshold:.0E} | {item.timestamp:%Y-%m-%d %H:%M:%S} | {item.pressure_mbar:.3E} |")

    lines.extend(["", "## Major pressure rises", ""])
    if not result.rises:
        lines.append("No pressure increase met the 100x-within-60-seconds detection rule.")
    else:
        for index, rise in enumerate(result.rises, start=1):
            lines.append(
                f"### Rise {index}: {rise.start_timestamp:%H:%M:%S} to {rise.end_timestamp:%H:%M:%S}"
            )
            lines.append("")
            lines.append(
                f"Pressure increased from {rise.start_pressure_mbar:.3E} to "
                f"{rise.peak_pressure_mbar:.3E} mbar ({rise.factor:,.0f}x)."
            )
            if rise.nearby_state_changes:
                lines.append("")
                lines.append("Named equipment-state changes within three minutes of the rise:")
                lines.extend(f"- {item}" for item in rise.nearby_state_changes)
            else:
                lines.append("")
                lines.append("No named equipment-state changes were logged within three minutes of this rise.")
            lines.append("")

    gap_events = [event for event in result.events if event.category == "Pressure data gap"]
    lines.extend(["", "## Data quality and interpretation", ""])
    lines.append(
        "- The log records timestamps only to the nearest second; repeated readings in the same second are retained without fabricated sub-second timing."
    )
    lines.append(
        f"- {len(result.state_snapshots):,} distinct equipment-state snapshots and "
        f"{sum(event.category == 'Equipment state' for event in result.events):,} named indicator transitions were found."
    )
    for gap in gap_events:
        lines.append(f"- {gap.timestamp:%H:%M:%S}: {gap.message}.")
    for warning in result.warnings:
        lines.append(f"- Warning: {warning}")
    lines.extend(
        [
            "",
            "This report describes temporal evidence only. Nearby equipment changes are not, by themselves, proof of causation.",
            "",
        ]
    )
    return "\n".join(lines)


def _downsample_chart_rows(
    readings: Sequence[PressureReading], max_points: int = 30_000
) -> tuple[list[PressureReading], str]:
    valid = [item for item in readings if item.pressure_mbar is not None and item.pressure_mbar > 0]
    if len(valid) <= max_points:
        return valid, "Full pressure series"
    bucket_count = max(1, max_points // 4)
    bucket_size = math.ceil(len(valid) / bucket_count)
    selected: dict[int, PressureReading] = {}
    for start in range(0, len(valid), bucket_size):
        bucket = valid[start : start + bucket_size]
        representatives = (
            bucket[0],
            min(bucket, key=lambda item: item.pressure_mbar),
            max(bucket, key=lambda item: item.pressure_mbar),
            bucket[-1],
        )
        for item in representatives:
            selected[item.sample_index] = item
    return [selected[key] for key in sorted(selected)], "Min/max envelope sampled to protect Excel chart performance"


def write_workbook(result: AnalysisResult, output_path: Path) -> None:
    try:
        import xlsxwriter
    except ImportError as exc:
        raise RuntimeError(
            "XlsxWriter is required for Excel output. Install dependencies with: "
            "python -m pip install -r requirements.txt"
        ) from exc

    workbook = xlsxwriter.Workbook(output_path)
    workbook.set_properties(
        {
            "title": "Pump-Down Pressure Analysis",
            "subject": "Pressure history and decoded equipment-state timeline",
            "author": "Pump-Down Analyzer",
            "comments": f"Generated from {result.experiment_name} ({len(result.source_logs)} source log(s))",
        }
    )
    summary = workbook.add_worksheet("Summary")
    pressure_sheet = workbook.add_worksheet("Pressure Data")
    equipment_sheet = workbook.add_worksheet("Equipment States")
    events_sheet = workbook.add_worksheet("Events & Notes")
    chart_sheet = workbook.add_worksheet("Chart Data")

    navy = "#18324A"
    blue = "#2D6FA3"
    pale_blue = "#EAF2F8"
    green = "#D9EAD3"
    green_text = "#245B2A"
    gray = "#E7E9EC"
    gray_text = "#525A61"
    amber = "#FFF2CC"
    red = "#F4CCCC"
    white = "#FFFFFF"

    title_format = workbook.add_format(
        {"bold": True, "font_size": 18, "font_color": white, "bg_color": navy, "align": "left", "valign": "vcenter"}
    )
    section_format = workbook.add_format(
        {"bold": True, "font_size": 11, "font_color": white, "bg_color": blue, "align": "left", "valign": "vcenter"}
    )
    label_format = workbook.add_format({"bold": True, "font_color": navy, "bg_color": pale_blue})
    text_format = workbook.add_format({"font_color": "#20262B"})
    integer_format = workbook.add_format({"num_format": "#,##0"})
    pressure_format = workbook.add_format({"num_format": "0.000E+00"})
    timestamp_format = workbook.add_format({"num_format": "yyyy-mm-dd hh:mm:ss"})
    duration_format = workbook.add_format({"num_format": "[h]:mm:ss"})
    header_format = workbook.add_format(
        {"bold": True, "font_color": white, "bg_color": navy, "border": 0, "text_wrap": True, "valign": "vcenter"}
    )
    bool_active = workbook.add_format({"bg_color": green, "font_color": green_text, "align": "center"})
    bool_inactive = workbook.add_format({"bg_color": gray, "font_color": gray_text, "align": "center"})
    warning_format = workbook.add_format({"bg_color": amber, "font_color": "#7F6000"})
    error_format = workbook.add_format({"bg_color": red, "font_color": "#8B1A1A"})

    for sheet in (summary, pressure_sheet, equipment_sheet, events_sheet, chart_sheet):
        sheet.hide_gridlines(2)

    # Pressure Data: one row per observation, with typed values and named states.
    pressure_headers = [
        "Sample Index",
        "Timestamp Local",
        "Elapsed Seconds",
        "Elapsed Minutes",
        "Pressure (mbar)",
        "Pressure Raw",
        "Unit Raw",
        "Sample In Second",
        "Source Log",
        "Log Line Number",
        "Quality Flag",
        "Active Equipment States",
        *STATE_LABELS,
    ]
    pressure_sheet.write_row(0, 0, pressure_headers, header_format)
    for row_index, item in enumerate(result.readings, start=1):
        pressure_sheet.write_number(row_index, 0, item.sample_index, integer_format)
        pressure_sheet.write_datetime(row_index, 1, item.timestamp, timestamp_format)
        pressure_sheet.write_number(row_index, 2, item.elapsed_seconds, integer_format)
        pressure_sheet.write_number(row_index, 3, item.elapsed_seconds / 60)
        if item.pressure_mbar is None:
            pressure_sheet.write_blank(row_index, 4, None, pressure_format)
        else:
            pressure_sheet.write_number(row_index, 4, item.pressure_mbar, pressure_format)
        pressure_sheet.write_string(row_index, 5, item.raw_pressure)
        pressure_sheet.write_string(row_index, 6, item.raw_unit)
        pressure_sheet.write_number(row_index, 7, item.sample_in_second, integer_format)
        pressure_sheet.write_string(row_index, 8, item.source_log)
        pressure_sheet.write_number(row_index, 9, item.line_number, integer_format)
        pressure_sheet.write_string(row_index, 10, item.quality_flag)
        pressure_sheet.write_string(row_index, 11, item.active_state_summary)
        for offset, state in enumerate(item.states, start=12):
            if state is None:
                pressure_sheet.write_blank(row_index, offset, None)
            else:
                pressure_sheet.write_string(row_index, offset, "TRUE" if state else "FALSE")
    pressure_last_row = len(result.readings)
    pressure_last_col = len(pressure_headers) - 1
    pressure_sheet.add_table(
        0,
        0,
        pressure_last_row,
        pressure_last_col,
        {
            "name": "PressureDataTable",
            "style": "Table Style Medium 2",
            "columns": [{"header": item} for item in pressure_headers],
        },
    )
    pressure_sheet.freeze_panes(1, 2)
    pressure_sheet.set_column(0, 0, 12)
    pressure_sheet.set_column(1, 1, 21, timestamp_format)
    pressure_sheet.set_column(2, 3, 15)
    pressure_sheet.set_column(4, 4, 16, pressure_format)
    pressure_sheet.set_column(5, 10, 17)
    pressure_sheet.set_column(11, 11, 54)
    pressure_sheet.set_column(12, pressure_last_col, 19)
    pressure_sheet.set_row(0, 34)
    state_range = (1, 12, pressure_last_row, pressure_last_col)
    pressure_sheet.conditional_format(*state_range, {"type": "text", "criteria": "containing", "value": "TRUE", "format": bool_active})
    pressure_sheet.conditional_format(*state_range, {"type": "text", "criteria": "containing", "value": "FALSE", "format": bool_inactive})
    pressure_sheet.conditional_format(1, 10, pressure_last_row, 10, {"type": "text", "criteria": "not containing", "value": "OK", "format": warning_format})

    # Equipment States: transition history plus active intervals.
    transition_headers = ["Timestamp Local", "Elapsed Seconds", "Equipment State", "Status", "Source Log", "Log Line Number"]
    equipment_sheet.write_row(0, 0, transition_headers, header_format)
    transition_rows: list[tuple[datetime, float, str, str, str, int]] = []
    if result.state_snapshots:
        first_snapshot = result.state_snapshots[0]
        for label, active in zip(STATE_LABELS, first_snapshot.states):
            transition_rows.append(
                (
                    first_snapshot.timestamp,
                    (first_snapshot.timestamp - result.first_reading.timestamp).total_seconds(),
                    label,
                    "Active" if active else "Inactive",
                    first_snapshot.source_log,
                    first_snapshot.line_number,
                )
            )
    for event in result.events:
        if event.category == "Equipment state":
            transition_rows.append(
                (
                    event.timestamp,
                    (event.timestamp - result.first_reading.timestamp).total_seconds(),
                    event.state_name,
                    "Active" if event.action == "Activated" else "Inactive",
                    event.source_log,
                    event.line_number or 0,
                )
            )
    for row_index, row in enumerate(transition_rows, start=1):
        equipment_sheet.write_datetime(row_index, 0, row[0], timestamp_format)
        equipment_sheet.write_number(row_index, 1, row[1], integer_format)
        equipment_sheet.write_string(row_index, 2, row[2])
        equipment_sheet.write_string(row_index, 3, row[3])
        equipment_sheet.write_string(row_index, 4, row[4])
        equipment_sheet.write_number(row_index, 5, row[5], integer_format)
    if transition_rows:
        equipment_sheet.add_table(
            0,
            0,
            len(transition_rows),
            len(transition_headers) - 1,
            {"name": "EquipmentTransitionsTable", "style": "Table Style Medium 2", "columns": [{"header": item} for item in transition_headers]},
        )
        equipment_sheet.conditional_format(1, 3, len(transition_rows), 3, {"type": "text", "criteria": "containing", "value": "Active", "format": bool_active})
        equipment_sheet.conditional_format(1, 3, len(transition_rows), 3, {"type": "text", "criteria": "containing", "value": "Inactive", "format": bool_inactive})

    interval_headers = ["Equipment State", "Start Timestamp", "End Timestamp", "Duration Seconds", "Source Log", "Source Log Line"]
    equipment_sheet.write_row(0, 7, interval_headers, header_format)
    intervals = build_active_state_intervals(result)
    for row_index, interval in enumerate(intervals, start=1):
        equipment_sheet.write_string(row_index, 7, str(interval["state_name"]))
        equipment_sheet.write_datetime(row_index, 8, interval["start_timestamp"], timestamp_format)
        equipment_sheet.write_datetime(row_index, 9, interval["end_timestamp"], timestamp_format)
        equipment_sheet.write_number(row_index, 10, float(interval["duration_seconds"]), integer_format)
        equipment_sheet.write_string(row_index, 11, str(interval["source_log"]))
        equipment_sheet.write_number(row_index, 12, int(interval["source_line_number"]), integer_format)
    if intervals:
        equipment_sheet.add_table(
            0,
            7,
            len(intervals),
            12,
            {"name": "ActiveStateIntervalsTable", "style": "Table Style Medium 4", "columns": [{"header": item} for item in interval_headers]},
        )
    equipment_sheet.freeze_panes(1, 0)
    equipment_sheet.set_column(0, 1, 21)
    equipment_sheet.set_column(2, 3, 24)
    equipment_sheet.set_column(4, 5, 28)
    equipment_sheet.set_column(6, 6, 3)
    equipment_sheet.set_column(7, 7, 24)
    equipment_sheet.set_column(8, 9, 21)
    equipment_sheet.set_column(10, 12, 20)
    equipment_sheet.set_row(0, 34)

    # Events and manual notes.
    event_headers = [
        "Event ID",
        "Timestamp Local",
        "End Timestamp Local",
        "Elapsed Seconds",
        "Category",
        "Equipment State",
        "Action",
        "Level",
        "Source",
        "Message",
        "Pressure at Event (mbar)",
        "Source Log",
        "Log Line Number",
        "Origin",
    ]
    events_sheet.write_row(0, 0, event_headers, header_format)
    for row_index, event in enumerate(result.events, start=1):
        events_sheet.write_string(row_index, 0, event.event_id)
        events_sheet.write_datetime(row_index, 1, event.timestamp, timestamp_format)
        if event.end_timestamp:
            events_sheet.write_datetime(row_index, 2, event.end_timestamp, timestamp_format)
        events_sheet.write_number(row_index, 3, (event.timestamp - result.first_reading.timestamp).total_seconds(), integer_format)
        events_sheet.write_string(row_index, 4, event.category)
        if event.state_name:
            events_sheet.write_string(row_index, 5, event.state_name)
        if event.action:
            events_sheet.write_string(row_index, 6, event.action)
        events_sheet.write_string(row_index, 7, event.level)
        events_sheet.write_string(row_index, 8, event.source)
        events_sheet.write_string(row_index, 9, event.message)
        if event.pressure_mbar is not None:
            events_sheet.write_number(row_index, 10, event.pressure_mbar, pressure_format)
        if event.source_log:
            events_sheet.write_string(row_index, 11, event.source_log)
        if event.line_number is not None:
            events_sheet.write_number(row_index, 12, event.line_number, integer_format)
        events_sheet.write_string(row_index, 13, event.origin)
    if result.events:
        events_sheet.add_table(
            0,
            0,
            len(result.events),
            len(event_headers) - 1,
            {"name": "EventsAndNotesTable", "style": "Table Style Medium 2", "columns": [{"header": item} for item in event_headers]},
        )
        events_sheet.conditional_format(1, 7, len(result.events), 7, {"type": "text", "criteria": "containing", "value": "WARNING", "format": warning_format})
        events_sheet.conditional_format(1, 7, len(result.events), 7, {"type": "text", "criteria": "containing", "value": "ERROR", "format": error_format})
        events_sheet.conditional_format(1, 7, len(result.events), 7, {"type": "text", "criteria": "containing", "value": "CRITICAL", "format": error_format})
    events_sheet.freeze_panes(1, 2)
    events_sheet.set_column(0, 0, 10)
    events_sheet.set_column(1, 2, 21)
    events_sheet.set_column(3, 4, 18)
    events_sheet.set_column(5, 6, 24)
    events_sheet.set_column(7, 8, 18)
    events_sheet.set_column(9, 9, 68)
    events_sheet.set_column(10, 13, 21)
    events_sheet.set_row(0, 34)

    # Chart helper range preserves source-row lineage and extremes when sampling.
    chart_rows, chart_mode = _downsample_chart_rows(result.readings)
    chart_sheet.write_row(0, 0, ["Timestamp Local", "Pressure (mbar)", "Source Sample Index"], header_format)
    for row_index, item in enumerate(chart_rows, start=1):
        chart_sheet.write_datetime(row_index, 0, item.timestamp, timestamp_format)
        chart_sheet.write_number(row_index, 1, item.pressure_mbar, pressure_format)
        chart_sheet.write_number(row_index, 2, item.sample_index, integer_format)
    chart_sheet.write(0, 5, "Chart Data Mode", label_format)
    chart_sheet.write(1, 5, chart_mode, text_format)
    chart_sheet.write(3, 5, "Method", label_format)
    chart_sheet.write(
        4,
        5,
        "The full series is used up to 30,000 points. Larger series retain each time bucket's first, last, minimum, and maximum readings.",
        text_format,
    )
    chart_sheet.set_column(0, 0, 21)
    chart_sheet.set_column(1, 1, 18)
    chart_sheet.set_column(2, 2, 20)
    chart_sheet.set_column(5, 5, 60)
    chart_sheet.set_row(0, 34)
    chart_sheet.freeze_panes(1, 0)

    # Summary presentation and formula-backed headline metrics.
    summary.merge_range("A1:N2", "Pump-Down Pressure Analysis", title_format)
    summary.set_row(0, 28)
    summary.set_row(1, 12)
    summary.write("A4", "Experiment", label_format)
    summary.write("B4", result.experiment_name, text_format)
    summary.write("A5", "Source log count", label_format)
    summary.write_number("B5", len(result.source_logs), integer_format)
    summary.write("A6", "Source fingerprint", label_format)
    summary.write("B6", result.source_sha256, text_format)

    summary.write("A8", "Run metrics", section_format)
    summary.merge_range("A8:D8", "Run metrics", section_format)
    formula_end = len(result.readings) + 1
    summary.write("A9", "Pressure samples", label_format)
    summary.write_formula(
        "B9",
        f"=COUNT('Pressure Data'!$E$2:$E${formula_end})",
        integer_format,
        len(result.valid_readings),
    )
    summary.write("A10", "History span", label_format)
    summary.write_formula(
        "B10",
        f"=MAX('Pressure Data'!$B$2:$B${formula_end})-MIN('Pressure Data'!$B$2:$B${formula_end})",
        duration_format,
        result.duration_seconds / 86400,
    )
    summary.write("A11", "Minimum pressure (mbar)", label_format)
    summary.write_formula(
        "B11",
        f"=MIN('Pressure Data'!$E$2:$E${formula_end})",
        pressure_format,
        result.minimum_reading.pressure_mbar,
    )
    summary.write("A12", "Minimum timestamp", label_format)
    summary.write_datetime("B12", result.minimum_reading.timestamp, timestamp_format)
    summary.write("A13", "Maximum pressure (mbar)", label_format)
    summary.write_formula(
        "B13",
        f"=MAX('Pressure Data'!$E$2:$E${formula_end})",
        pressure_format,
        result.maximum_reading.pressure_mbar,
    )
    summary.write("A14", "Named state transitions", label_format)
    summary.write_number(
        "B14", sum(event.category == "Equipment state" for event in result.events), integer_format
    )

    summary.merge_range("A16:D16", "First readings below decade thresholds", section_format)
    summary.write_row("A17", ["Threshold (mbar)", "Timestamp", "Reading (mbar)", "Sample Index"], header_format)
    for offset, (threshold, item) in enumerate(result.thresholds, start=18):
        summary.write_number(offset - 1, 0, threshold, pressure_format)
        summary.write_datetime(offset - 1, 1, item.timestamp, timestamp_format)
        summary.write_number(offset - 1, 2, item.pressure_mbar, pressure_format)
        summary.write_number(offset - 1, 3, item.sample_index, integer_format)

    rise_start_row = 19 + len(result.thresholds)
    summary.merge_range(rise_start_row - 1, 0, rise_start_row - 1, 4, "Major pressure rises and nearby named state changes", section_format)
    summary.write_row(
        rise_start_row,
        0,
        ["Start", "End", "From (mbar)", "To (mbar)", "Factor", "Nearby named equipment changes"],
        header_format,
    )
    for index, rise in enumerate(result.rises, start=rise_start_row + 1):
        summary.write_datetime(index, 0, rise.start_timestamp, timestamp_format)
        summary.write_datetime(index, 1, rise.end_timestamp, timestamp_format)
        summary.write_number(index, 2, rise.start_pressure_mbar, pressure_format)
        summary.write_number(index, 3, rise.peak_pressure_mbar, pressure_format)
        summary.write_number(index, 4, rise.factor, integer_format)
        summary.write(index, 5, "\n".join(rise.nearby_state_changes) or "None logged within three minutes", text_format)
        summary.set_row(index, max(36, min(165, 18 * (len(rise.nearby_state_changes) + 1))))

    note_row = rise_start_row + max(2, len(result.rises) + 2)
    summary.merge_range(note_row, 0, note_row, 4, "Interpretation notes", section_format)
    notes = [
        "Timestamps have one-second resolution; repeated readings retain their sequence without invented fractions.",
        "Pressure-rise detection uses a 100x increase within a rolling 60-second window and merges detections from the same episode.",
        "Nearby equipment changes are temporal evidence, not proof of causation.",
        *result.warnings,
    ]
    for offset, note in enumerate(notes, start=note_row + 1):
        summary.merge_range(offset, 0, offset, 4, f"• {note}", text_format)
    manifest_row = note_row + len(notes) + 2
    summary.merge_range(manifest_row, 0, manifest_row, 5, "Ordered source log manifest", section_format)
    summary.write_row(
        manifest_row + 1,
        0,
        ["#", "Source Log", "SHA-256", "Log Start", "Log End", "Pressure Samples"],
        header_format,
    )
    for index, source_log in enumerate(result.source_logs, start=1):
        row = manifest_row + 1 + index
        summary.write_number(row, 0, index, integer_format)
        summary.write_string(row, 1, source_log.path.name)
        summary.write_string(row, 2, source_log.sha256)
        if source_log.log_start is not None:
            summary.write_datetime(row, 3, source_log.log_start, timestamp_format)
        if source_log.log_end is not None:
            summary.write_datetime(row, 4, source_log.log_end, timestamp_format)
        summary.write_number(row, 5, source_log.pressure_reading_count, integer_format)
    summary.set_column("A:A", 25)
    summary.set_column("B:B", 34)
    summary.set_column("C:C", 68)
    summary.set_column("D:E", 21)
    summary.set_column("F:F", 70)
    summary.set_column("G:I", 3)
    summary.set_column("J:N", 14)
    summary.freeze_panes(3, 0)

    chart = workbook.add_chart({"type": "scatter", "subtype": "straight"})
    chart.add_series(
        {
            "name": "Pressure (mbar)",
            "categories": f"='Chart Data'!$A$2:$A${len(chart_rows) + 1}",
            "values": f"='Chart Data'!$B$2:$B${len(chart_rows) + 1}",
            "line": {"color": blue, "width": 1.25},
            "marker": {"type": "none"},
        }
    )
    chart.set_title({"name": "Pressure history (log scale)"})
    chart.set_x_axis(
        {
            "name": "Local time",
            "num_format": "hh:mm:ss",
            "major_gridlines": {"visible": False},
        }
    )
    chart.set_y_axis(
        {
            "name": "Pressure (mbar)",
            "log_base": 10,
            "num_format": "0.0E+00",
            "major_gridlines": {"visible": True, "line": {"color": "#D9DEE3", "width": 0.5}},
        }
    )
    chart.set_legend({"none": True})
    chart.set_chartarea({"border": {"none": True}})
    chart.set_plotarea({"border": {"color": "#B7C0C8", "width": 0.5}})
    chart.set_size({"width": 900, "height": 520})
    summary.insert_chart("H4", chart)

    workbook.close()


def build_viewer_payload(result: AnalysisResult, summary_markdown: str) -> dict[str, object]:
    first = result.first_reading.timestamp
    readings = [
        [round(item.elapsed_seconds, 3), item.pressure_mbar]
        for item in result.readings
        if item.pressure_mbar is not None and item.pressure_mbar > 0
    ]
    sample_meta = [
        [item.sample_in_second, item.source_log, item.line_number, item.quality_flag]
        for item in result.readings
        if item.pressure_mbar is not None and item.pressure_mbar > 0
    ]
    intervals = [
        [
            int(item["state_index"]),
            round((item["start_timestamp"] - first).total_seconds(), 3),
            round((item["end_timestamp"] - first).total_seconds(), 3),
        ]
        for item in build_active_state_intervals(result)
    ]
    state_changes = [
        [
            round((event.timestamp - first).total_seconds(), 3),
            STATE_LABELS.index(event.state_name),
            1 if event.action == "Activated" else 0,
            event.source_log,
            event.line_number,
        ]
        for event in result.events
        if event.category == "Equipment state"
    ]
    events = [
        {
            "id": event.event_id,
            "t": round((event.timestamp - first).total_seconds(), 3),
            "t1": round((event.end_timestamp - first).total_seconds(), 3) if event.end_timestamp else None,
            "category": event.category,
            "state": event.state_name,
            "action": event.action,
            "level": event.level,
            "source": event.source,
            "message": event.message,
            "pressure": event.pressure_mbar,
            "source_log": event.source_log,
            "line": event.line_number,
            "origin": event.origin,
        }
        for event in result.events
        if event.origin != "manual"
    ]
    return {
        "schema_version": 2,
        "source_name": result.experiment_name,
        "source_stem": result.experiment_name,
        "source_log_sha256": result.source_sha256,
        "source_logs": [
            {
                "name": source_log.path.name,
                "sha256": source_log.sha256,
                "log_start_timestamp_local": (
                    source_log.log_start.isoformat(timespec="seconds") if source_log.log_start else None
                ),
                "log_end_timestamp_local": (
                    source_log.log_end.isoformat(timespec="seconds") if source_log.log_end else None
                ),
                "pressure_reading_count": source_log.pressure_reading_count,
            }
            for source_log in result.source_logs
        ],
        "start_timestamp_local": first.isoformat(timespec="seconds"),
        "end_timestamp_local": result.last_reading.timestamp.isoformat(timespec="seconds"),
        "duration_seconds": result.duration_seconds,
        "reading_count": len(result.readings),
        "minimum_pressure_mbar": result.minimum_reading.pressure_mbar,
        "minimum_timestamp_local": result.minimum_reading.timestamp.isoformat(timespec="seconds"),
        "maximum_pressure_mbar": result.maximum_reading.pressure_mbar,
        "readings": readings,
        "sample_meta": sample_meta,
        "state_names": list(STATE_LABELS),
        "initial_state_source": (
            [result.state_snapshots[0].source_log, result.state_snapshots[0].line_number]
            if result.state_snapshots
            else ["", None]
        ),
        "active_state_intervals": intervals,
        "state_changes": state_changes,
        "events": events,
        "thresholds": [
            {
                "threshold_mbar": threshold,
                "timestamp_local": item.timestamp.isoformat(timespec="seconds"),
                "elapsed_seconds": item.elapsed_seconds,
                "pressure_mbar": item.pressure_mbar,
            }
            for threshold, item in result.thresholds
        ],
        "summary_markdown": summary_markdown,
    }


def write_viewer(
    result: AnalysisResult,
    output_path: Path,
    template_path: Path,
    summary_markdown: str,
) -> None:
    template = template_path.read_text(encoding="utf-8")
    payload = json.dumps(build_viewer_payload(result, summary_markdown), separators=(",", ":"), ensure_ascii=False)
    annotation_payload = json.dumps(
        [
            {
                "id": item.annotation_id,
                "kind": item.kind,
                "start_timestamp_local": item.start_timestamp.isoformat(timespec="seconds"),
                "end_timestamp_local": item.end_timestamp.isoformat(timespec="seconds") if item.end_timestamp else None,
                "category": item.category,
                "note": item.note,
            }
            for item in result.annotations
        ],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    # Avoid ending a script element if a note contains HTML-like text.
    payload = payload.replace("</", "<\\/")
    annotation_payload = annotation_payload.replace("</", "<\\/")
    rendered = template.replace("__PUMPDOWN_DATA_JSON__", payload).replace(
        "__PUMPDOWN_ANNOTATIONS_JSON__", annotation_payload
    )
    output_path.write_text(rendered, encoding="utf-8", newline="\n")


def _check_targets(targets: Iterable[Path], overwrite: bool) -> None:
    existing = [path for path in targets if path.exists()]
    if existing and not overwrite:
        joined = "\n  ".join(str(path) for path in existing)
        raise FileExistsError(
            "Refusing to overwrite existing output files. Re-run with --overwrite:\n  " + joined
        )


def run_analysis(args: argparse.Namespace) -> dict[str, Path]:
    input_path = Path(args.input_log).expanduser()
    log_paths, source_path, experiment_name = resolve_input_logs(input_path, explicit_date=args.date)
    annotations_path = Path(args.annotations).expanduser() if args.annotations else None
    result = parse_logs(
        log_paths,
        explicit_date=args.date,
        annotations_path=annotations_path,
        source_path=source_path,
        experiment_name=experiment_name,
    )
    output_dir = Path(args.out).expanduser() if args.out else Path("outputs") / experiment_name
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = experiment_name
    paths = {
        "pressure_csv": output_dir / f"{stem}_pressure.csv",
        "events_csv": output_dir / f"{stem}_events.csv",
        "equipment_csv": output_dir / f"{stem}_equipment_states.csv",
        "workbook": output_dir / f"{stem}_analysis.xlsx",
        "summary": output_dir / f"{stem}_summary.md",
        "viewer": output_dir / f"{stem}_viewer.html",
    }
    _check_targets(paths.values(), args.overwrite)
    summary_markdown = build_summary_markdown(result)
    write_pressure_csv(result, paths["pressure_csv"])
    write_events_csv(result, paths["events_csv"])
    write_equipment_csv(result, paths["equipment_csv"])
    paths["summary"].write_text(summary_markdown, encoding="utf-8", newline="\n")
    write_workbook(result, paths["workbook"])
    template_path = Path(__file__).resolve().with_name("viewer_template.html")
    if not template_path.is_file():
        raise FileNotFoundError(f"Viewer template not found beside the script: {template_path}")
    write_viewer(result, paths["viewer"], template_path, summary_markdown)

    print(f"Processed {len(result.readings):,} pressure readings from {len(result.source_logs):,} source log(s).")
    print(
        f"Minimum: {result.minimum_reading.pressure_mbar:.3E} mbar at "
        f"{result.minimum_reading.timestamp:%Y-%m-%d %H:%M:%S}"
    )
    print(
        f"Named equipment transitions: "
        f"{sum(event.category == 'Equipment state' for event in result.events):,}"
    )
    for label, path in paths.items():
        print(f"{label}: {path}")
    return paths


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extract pump-down pressure history, decoded equipment states, Excel analysis, and an offline viewer from one log or one directory of chained logs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  python pumpdown_analyzer.py inputs/log_2026-08-03_09-15-00.txt
  python pumpdown_analyzer.py "inputs/2026-07-21 pump down"
  python pumpdown_analyzer.py D:/pump-logs/run-17.txt --date 2026-08-03
  python pumpdown_analyzer.py run.txt --out D:/analyses/run-17 --overwrite

For a directory, non-recursive log_YYYY-MM-DD_HH-MM-SS.txt files are chained as
one experiment. The default output directory is ./outputs/<input-name>, relative
to the current working directory. Input logs are never modified.
""",
    )
    parser.add_argument("input_log", help="Dashboard log file or chained-experiment directory to analyze")
    parser.add_argument(
        "--out",
        help="Output directory (default: ./outputs/<input-name> relative to the current working directory)",
    )
    parser.add_argument("--date", help="Override/inject a single log file date as YYYY-MM-DD")
    parser.add_argument("--annotations", help="Annotation JSON exported by the viewer")
    parser.add_argument("--overwrite", action="store_true", help="Replace generated output files if they already exist")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    try:
        run_analysis(args)
    except (FileNotFoundError, FileExistsError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
