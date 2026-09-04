# Data

Workbooks live here by default, and **nothing in this folder is committed** —
`.gitignore` excludes every `.xlsx`, `.xlsm`, `.xls` and `.csv` beneath it, so
production data never lands in the repository.

## Sample data

`data/sample/` is filled by:

```bash
python tools/generate_sample_data.py --months 20
```

It writes one workbook per year of realistic-looking production, with the same
shape a real export has: friendly headers, premium written as currency text, a
channel column, and web business booked to a house account. Delete the folder
once the dashboard is pointed at live files.

## Using your own workbooks

Either drop them in a folder here and leave the config alone, or — better —
point the config straight at wherever they already are:

```yaml
data:
  sources:
    - name: "production"
      path: "//fileserver/reports/daily"
      glob: "*.xlsx"
```

Then open the **Diagnostics** page to confirm every file was found and every
column resolved. See the main [README](../README.md) for the full mapping guide.
