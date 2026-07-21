# Local input logs

Place EBEAM dashboard log files here if you want to keep inputs beside the
analyzer. Input logs can also live anywhere else; pass their full or relative
path to `pumpdown_analyzer.py`.

Raw `inputs/*` files are intentionally ignored by Git because logs can be large
and can contain machine-specific paths or operational details. This README is
the only tracked file in the directory by default.

For automatic date detection, retain a filename containing
`log_YYYY-MM-DD`, such as `log_2026-08-03_09-15-00.txt`. For a differently
named file, pass `--date YYYY-MM-DD`.

For one experiment split across rotated logs, create a subdirectory containing
all of its `log_YYYY-MM-DD_HH-MM-SS.txt` fragments and pass that directory to
the analyzer. Chained directories do not support `--date`; their filenames
provide ordering and date information.
