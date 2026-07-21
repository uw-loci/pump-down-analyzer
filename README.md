# Pump-Down Analyzer

Pump-Down Analyzer turns one EBEAM dashboard log—or a directory of chained logs
from one experiment—into reusable pressure data, an Excel report, an anomaly
summary, and a fully offline interactive viewer.

It creates:

- a pressure CSV with timestamps, sequence information, quality flags, and eight named equipment-state columns;
- named equipment-transition and event CSV files;
- an Excel workbook with a logarithmic pressure-versus-time chart;
- a factual Markdown anomaly summary; and
- an offline viewer with synchronized pressure/state zooming, panning, point and range notes, image export, and an annotated ZIP export.

The input log is read-only. Encoded VTRX switch values are decoded immediately
into named Boolean indicators and are not included in generated public files.

## Folder renaming and portability

The repository folder can be renamed or moved. No code depends on the name of
the parent directory, and generated workbooks, CSV files, summaries, and
viewers do not link back to an absolute repository path.

There is one intentional structural relationship: keep
`viewer_template.html` beside `pumpdown_analyzer.py`. The analyzer finds the
template relative to its own script file. Input logs may live anywhere.

The default output location is `./outputs/<input-name>/`, where the input name
is the file stem or experiment-directory name and `.` is the directory from
which the command is run. Run commands from the repository root if you want all
default outputs collected inside this repository.

## Repository layout

```text
pump-down-analyzer/
├── pumpdown_analyzer.py       Main command-line application
├── viewer_template.html      Offline viewer template; keep beside the script
├── requirements.txt          Runtime dependency
├── requirements-dev.txt      Optional workbook-verification dependency
├── inputs/                   Optional local log location; raw logs are ignored
├── outputs/                  Generated per-input analyses; ignored by Git
├── tests/                    Unit and optional real-log regression tests
└── tools/                    Optional artifact-validation utilities
```

## Install

Python 3.10 or later is recommended. A virtual environment keeps the one
runtime dependency isolated from the rest of the computer.

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

If PowerShell activation is disabled, use
`.\.venv\Scripts\python.exe` in place of `python` in the commands below.

## Analyze one pump-down

The log may be copied into `inputs/` or referenced in its existing location.

```powershell
python .\pumpdown_analyzer.py .\inputs\log_2026-08-03_09-15-00.txt
```

The date is inferred from `log_YYYY-MM-DD` in the filename. For another naming
scheme, provide the date explicitly:

```powershell
python .\pumpdown_analyzer.py "D:\Pump Logs\run-17.txt" --date 2026-08-03
```

Choose a different result location when desired:

```powershell
python .\pumpdown_analyzer.py "D:\Pump Logs\run-17.txt" --date 2026-08-03 --out "D:\Pump Analyses\run-17"
```

Use `--overwrite` when intentionally regenerating an existing analysis. Without
it, the program refuses to replace files.

Run `python .\pumpdown_analyzer.py --help` for the complete command reference.

## Analyze chained logs as one experiment

When the EBEAM dashboard rotates its log during one experiment, place all of
that experiment's fragments in one directory and pass the directory itself:

```powershell
python .\pumpdown_analyzer.py ".\inputs\2026-07-21 pump down"
```

The analyzer non-recursively collects `log_*.txt` files whose names use
`log_YYYY-MM-DD_HH-MM-SS.txt`, orders them by that timestamp, and produces one
combined analysis under `outputs/2026-07-21 pump down/`. Other files in the
directory are ignored.

Experiment-wide elapsed time, same-second sample sequencing, equipment state,
gap detection, and anomaly analysis continue across fragment boundaries. A
fragment with no pressure samples is retained in the source manifest and does
not prevent analysis as long as another fragment contains pressure data. True
timestamp overlaps are rejected instead of being silently merged.

`--date` is available only for single-file analysis. Chained files must carry
their dates and start times in their filenames.

## Generated files

For an input named `log_2026-08-03_09-15-00.txt`, the default output directory
contains:

```text
outputs/log_2026-08-03_09-15-00/
├── log_2026-08-03_09-15-00_pressure.csv
├── log_2026-08-03_09-15-00_equipment_states.csv
├── log_2026-08-03_09-15-00_events.csv
├── log_2026-08-03_09-15-00_analysis.xlsx
├── log_2026-08-03_09-15-00_summary.md
└── log_2026-08-03_09-15-00_viewer.html
```

Open the `_viewer.html` file directly in a current browser. It is self-contained
and does not require Python, a web server, or an internet connection after it
has been generated.

## Review, annotate, and share

1. Open the generated viewer.
2. Use the mouse wheel to zoom and drag to pan. The pressure history and all
   eight equipment-state lanes remain on the same time axis.
3. Select **Add point note** and click the pressure plot, or select
   **Add range note** and drag across a time span.
4. Select **Save annotated HTML** for a new viewer file containing the notes.
5. Select **Export analysis package** for a ZIP containing the annotated viewer,
   annotated PNG, extracted CSV files, annotation CSV/JSON, and Markdown summary.

The annotated ZIP is the most complete file to share with ChatGPT or Codex for
follow-up analysis. The annotated HTML and PNG are also useful when only the
visual context is needed.

To bring viewer notes back into a regenerated Excel workbook, extract the
`_annotations.json` file from the ZIP and run:

```powershell
python .\pumpdown_analyzer.py .\inputs\log_2026-08-03_09-15-00.txt --annotations .\notes\log_2026-08-03_09-15-00_annotations.json --overwrite
```

Annotation files include a source fingerprint: the raw log hash for one file or
a stable fingerprint of the ordered content hashes for a chain. A mismatch is
reported so notes are not silently associated with the wrong experiment.

## Analyze multiple independent logs

Passing a directory chains its logs into one experiment. To analyze unrelated
logs as separate experiments, invoke the analyzer once per file. Timestamped
EBEAM filenames create separate default output directories, so repeated
experiments do not overwrite one another.

To process every matching local input in PowerShell:

```powershell
Get-ChildItem .\inputs\log_*.txt | ForEach-Object {
    python .\pumpdown_analyzer.py $_.FullName
}
```

## Assumptions and interpretation

- Pressure values are normalized only when the log reports `mbar`; unsupported
  units remain in the dataset with a quality flag.
- Logs spanning midnight are handled, and multiple pressure samples with the
  same displayed second retain their original sequence.
- Chained logs are ordered from their filename timestamps. Last-known equipment
  state is carried forward across boundaries, while source filename and line
  number remain attached to every extracted record.
- Each named equipment bit is treated as an independent indicator. The analyzer
  does not infer whether simultaneous OPEN/CLOSED feedback is mechanically valid.
- Position 4 of the MSB-first VTRX state field is the 972B Relay 1 normally-open
  contact feedback. A value of `1` means the contact is closed and is displayed
  as `Relay 1 CLOSED`.
- Threshold crossings, pressure-data gaps, rapid rises, and nearby named state
  changes are evidence for investigation, not automatic root-cause conclusions.
- The analyzer does not modify the EBEAM dashboard or any original log.

## Tests and developer checks

Install the optional verification dependency and run the test suite:

```powershell
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```

The small synthetic tests always run. The detailed regression checks use the
original July 17 log when it is present locally. In another checkout, point the
tests at an equivalent private fixture without committing it:

```powershell
$env:PUMPDOWN_TEST_LOG = "D:\Pump Logs\log_2026-07-17_17-54-01.txt"
python -m unittest discover -s tests -v
```

Optional generated-artifact checks:

```powershell
python .\tools\check_workbook.py .\outputs\RUN\RUN_analysis.xlsx
node .\tools\check_viewer.mjs .\outputs\RUN\RUN_viewer.html
```

Node.js is needed only for the optional viewer syntax check, not to run the
analyzer or use its viewer.

## Starting the Git repository

After renaming this folder to `pump-down-analyzer`, open a new terminal in it
and run:

```powershell
git init
git add .
git commit -m "Initial pump-down analyzer"
```

The included `.gitignore` keeps raw input logs, generated outputs, QA renders,
Python caches, virtual environments, and locally exported annotation packages
out of the commit. Review the staged files with `git status` before committing,
and choose a license before publishing the repository publicly.
