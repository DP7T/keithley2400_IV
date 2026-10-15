# Guide for continuing development of the Keithley 2400 I-V app (for AI assistants and developers)

Read this before changing the code. It describes the app, how it is organized, and the decisions the
user has already made. Do not undo these decisions without asking. For the history of the requests, see
[HOW_IT_WAS_BUILT.md](HOW_IT_WAS_BUILT.md). For the user-facing manual, see [README.md](README.md).

## What it is

A Windows desktop program (Python 3.10+, Tkinter + Matplotlib, PyVISA) for I-V and constant-source
measurements with a **Keithley 2400 SourceMeter** over GPIB, through an **NI GPIB-USB-HS** adapter
(default address `GPIB0::24::INSTR`). It needs the NI-488.2 driver. It is distributed as a single
`KeithleyIV.exe` (PyInstaller onefile) through GitHub releases.

The user is a physicist who runs the app on a lab PC and is not a programmer. They work from an iPhone,
a Mac and the Windows lab PC, test each version on the real instrument, and report back with
screenshots. Write in plain language for them. Keep the GUI simple.

## Files

| file | contents |
|---|---|
| `keithley2400_gui.pyw` | the whole app, in one file. **Keep the name**: desktop shortcuts, `build_exe.bat` and the workflow use it. |
| `build_exe.bat` | builds `dist\KeithleyIV.exe` locally with PyInstaller (`--onefile --windowed --collect-all pyvisa`) |
| `.github/workflows/build.yml` | builds the exe on Windows for every push to `main` (artifact) and attaches it to every published release |
| `README.md` | user manual: features, quick start, tips, data format, naming schemes, troubleshooting, disclaimer |
| `HOW_IT_WAS_BUILT.md` | the user's requests in order, and what each one added. Add a section for each update. |
| `screenshot_app.png`, `example_output.png` | images used by the README. Refresh `screenshot_app.png` when the layout changes. |
| `LICENSE` | MIT, © DP7T |

Sections inside `keithley2400_gui.pyw`, in order:

| part | contents |
|---|---|
| error handling | `_log_path()` (next to the script, or next to the exe when frozen), `LOG_FILE` = `keithley_iv_errors.log`, `_fatal_hook` (pop-up even for startup crashes), `hint_for()` (plain-language hints for VISA/GPIB/file errors) |
| `Keithley2400` | SCPI driver: `configure()` (mode, compliance, NPLC, 4-wire, rear, fixed or auto ranges), `measure()` → `(V, I, compliance, over_range)`, `check_errors()` reads `:SYST:ERR?` |
| `SimulatedSMU` | diode model with the same interface, used by the **Simulate** checkbox |
| `build_sweep()` | source points and cycle number for each shape |
| numbers | `parse_num()` (SI prefixes), `parse_duration()`, `fmt_si()`, `fmt_dur()`, `fmt_clock()`, `axis_scale()` |
| naming | `NAME_SCHEMES`, `name_tokens()`, `check_pattern()`, `next_name()`, `safe_part()` |
| resistance | `valid_rows()`, `fit_resistance()` (last N points), `fit_stats()` (whole run: R, R²) |
| CSV | `COLUMNS`, `csv_header()`, `csv_cells()`, `LiveCSV` (writes each row as it is measured) |
| GUI | `THEMES`, `PUB_COLORS`, `APP_VERSION`, `ScrollFrame`, `App` (layout, theme, plotting, run control, worker threads, queue, saving, PNG export) |

A data row is `(index, time_s, source, V, I, compliance, cycle, R_fit, over_range)`. The CSV columns
follow this order. **Only append new columns at the end**, so the user's analysis scripts keep working.

## Decisions already made (keep them)

**Safety**
- The output is switched off at the end of every run, on Stop, on errors and when the app is closed.
  If switching it off fails, the user is told to press OUTPUT on the front panel.
- After `configure()`, the instrument's error queue is read. If the 2400 rejected anything, the output is
  **never switched on**. It is read again after each run, and any errors are shown as a warning.
- The app asks for confirmation above 20 V or above 5000 points. The source and compliance limits of the
  2400 are checked before starting.

**Measurement modes**
- Modes: source V / measure I (default), or source I / measure V.
- Shapes:
  - *Single*, *Double (fwd + rev)*, *Bipolar (0 → + → − → 0)*.
  - *Cycle*: 0 → max → min → 0, repeated N times. The next cycle continues from the shared 0, and
    each point carries its cycle number.
  - *Constant (vs time)*: holds the source and records against time. Duration 0 = until Stop.
    A queue step needs a duration.
- **Fixed ranges:** both the source range and the measure range can be Auto or fixed.
  - If the sweep exceeds a fixed source range, starting is refused.
  - If compliance is above a fixed measure range, the user gets a warning (the 2400 limits at the
    range: range compliance, status bit 16).
  - Over-range readings (`9.9E37` or status bit 0) are stored as NaN and flagged in `over_range`.

**Number input**
- Every numeric field accepts `0.1`, `1e-1`, `100m`, `100 mV`, `10u`, `10µA` and `2k`.
  Lowercase `m` = milli, capital `M` = mega, and a comma works as the decimal point.
- Durations (constant mode, queue waits) accept `90`, `90 s`, `500 ms`, `10 min` and `1.5 h`.
  A lone `m` is rejected there, because it is ambiguous.

**Resistance**
- Live readout: a linear fit over the last N points (setting *R from last N points*). Points in
  compliance or over range are excluded.
- Under the readouts: a whole-run linear fit with R, R² and n. In constant mode it shows the mean R and
  its spread instead.
- A dashed fit line is drawn on the **screen plot only**.

**Saved PNG (the user likes it exactly as it is, so don't change the style without asking)**
- Publication style:
  - white background, black frame, Arial
  - no grid; outward ticks with minor ticks; about 5 rounded major ticks
  - units chosen automatically on both axes (mV, µA, …); log axes keep the base unit
- It follows the user's plot settings: line / scatter / both, line width, marker size, colour by cycle,
  and scale (linear, log |y|, log-log).
- **No fit line and no fit text in the PNG.** The R / R² fit must not affect the output figure.

**Screen plot**
- Dark theme by default, with a light option.
- Colour by cycle: palette colours for up to 7 cycles, and a plasma colormap for more.
- At most 12 runs are overlaid, with older runs faded.
- I-V plots and time plots are never mixed: the plot is cleared when the kind changes.

**Data**
- **One folder per measurement**, inside the data folder, containing `<name>.csv` and `<name>.png`.
  Queue runs go into a `Queue_<date>_<time>` folder that holds one folder per step. A queue folder
  left empty is removed.
- **Naming schemes:** see `NAME_SCHEMES` (default `Sample_Date_#`), plus a custom pattern built from
  `{sample} {date} {time} {n} {type} {range}`.
  - `{n}` continues after the highest number already in the folder.
  - Names are made Windows-safe and ASCII-only (µ → u).
  - The next name is previewed in the app.
- **Live saving:** `LiveCSV` writes and flushes every point as it is measured, with an fsync every 2 s.
- The CSV header is `#` lines with the instrument, date, sample, all settings and the queue step.
- **Stop (keep data)** vs **Stop & discard**:
  - Discard deletes only the files of the run that was running, and removes it from the plot.
  - In a queue, discard stops the queue and keeps the steps already finished.

**Queue**
- Each step stores a snapshot of the Sweep-tab settings from when it was added. Double-clicking a step
  loads it back into the Sweep tab.
- During a wait step the output is OFF.
- Errors stop the queue. Instrument warnings are collected and shown at the end, never as a blocking
  pop-up in the middle of an unattended queue.
- Queue results are always saved, even with auto-save off.

**GUI layout**
- Left side:
  - *Connection*
  - two tabs: **Sweep** (sections Sweep, Measurement, Saving; it scrolls on short screens) and **Queue**
  - always visible underneath: Sample, Start, *Stop (keep data)* / *Stop & discard*, *Save last as…* / *Clear plot*
- Right side: readouts (V, I, R from the last N points, points/cycle), the fit line text, a progress bar,
  the plot-style bar, and the plot.
- There was a separate **Data** tab; the user asked to move it back under Sweep. Do not reintroduce it.
- The window opens up to 980 px tall when the screen allows it, so the Sweep tab fits without scrolling.
- Settings and the queue are saved in `~/.keithley_iv_settings.json`. When adding settings, keep loading
  old files (for example, the old `logy` key is mapped to `scale`).

## How to test without hardware

- In the app, tick **Simulate** (`SimulatedSMU`). Everything works except the real instrument.
- Quick check: `python -m py_compile keithley2400_gui.pyw` (the workflow also does this).
- Testing the real window headless (Linux or CI) needs a Python with Tk and a virtual display:
  - run a small driver script under `xvfb-run`;
  - in it: `import keithley2400_gui`, create `App()`, set `v_simulate`, set the fields through the
    `v_*` variables, call `start()` / `run_queue()` / `stop_discard()`, and pump `app.update()` until
    `app.busy` is False;
  - replace `messagebox.*` with stubs so nothing blocks;
  - take screenshots with ImageMagick: `import -window root shot.png`.
- **Look at the screenshots**, not only at "no errors". Layout problems, such as buttons hidden behind
  the status bar or a section pushed below the visible area, were only caught that way.
- Check the folder and file layout on disk after runs, discards and queues.
- When adding hardware features, also add them to `SimulatedSMU`, so the app can always be tried
  without the instrument.
- Never claim something was tested on the real 2400 unless the user did it. Tell the user exactly what
  to check on the setup.

## Releasing

1. Bump `APP_VERSION` in `keithley2400_gui.pyw`, and add a section to `HOW_IT_WAS_BUILT.md` for the requests.
2. Push to `main`. The workflow builds the exe as an artifact (Actions tab), which can be tested before
   release.
3. The **user** publishes the GitHub release:
   - Releases → Draft a new release → tag `vX.Y`, **Create new tag on publish**, target `main`.
   - The workflow attaches `KeithleyIV.exe` in about 5 minutes.
   - The download link is `https://github.com/DP7T/keithley2400_IV/releases/latest`.
4. Updating on a PC = replacing the exe. There is nothing to uninstall; settings and data stay.

The assistant's session could push to `main` but could **not** create or move tags or releases (HTTP 403).
Ask the user to publish releases. Because tags can't be moved from the session, any history rewrite
leaves the old release tag pointing to the old commit, and the user then has to delete and re-create the
tag and release.

## Working with the user (privacy and history)

- **Commit as** `DP7T <342493853+DP7T@users.noreply.github.com>`. Never use or publish the user's
  personal email.
- **Commit messages:** a short, plain summary. The user allows exactly one attribution line:
  `Co-Authored-By: Claude <noreply@anthropic.com>`.
  **Never** add session links, conversation IDs or any other session-related information to commits,
  files or anything public.
- The user wants a **clean public history**. The repository was re-created once to remove old commits.
  - Do not force-push or rewrite history without asking: it leaves "force pushed" traces in the activity
    log, and the release tag can't be moved from the session.
  - Normal new commits for additions are fine.
- Keep changes small and testable. Ask before changing the conventions above.
- Explain errors and setup steps in plain words. The user sends photos and screenshots.

## Status

- **v1.0:** first release. Sweeps, cycles, constant mode, queue, live saving, fixed ranges, R/R² fit,
  log-log, publication PNG, error reporting, exe build.
- **v1.1:** Saving section moved into the Sweep tab, Stop & discard, one folder per measurement,
  naming schemes. Tested in simulation (real window, headless); the user still has to publish the
  release and test it on the instrument.
