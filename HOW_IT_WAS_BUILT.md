# How this app was built

This app was built in one conversation with **Claude** (an AI assistant by Anthropic), over about a day,
by describing what was needed in plain language. No code was written by hand. The requests below
are summarized in the order they were given, so others can see how it grew and can build something
similar for their own instruments.

Requests are lightly paraphrased. Short quotes are the original wording.

---

## 1. First version: a script

> "Can we build a program for Keithley 2400 SMU for IV measurements"

**Result:** a Python command-line script (PyVISA) for voltage or current sweeps, with compliance, NPLC,
2/4-wire mode, a live plot and CSV export. The output is always switched off at the end. A simulation
mode allows testing without the instrument.

## 2. A graphical interface for the lab's hardware

> "I want UI tho and I use GPIB-USB-HS"

**Result:** a desktop app (Tkinter + Matplotlib) with a settings panel, a live plot, Start/Stop, a
progress bar, live V/I readouts and auto-save. The default address was set to `GPIB0::24::INSTR`.

## 3. Getting it onto the lab PC without logging in there

Requests: move it to the lab PC without logging in to Claude, and *"help me with terminal commands to prepare the PC"*.

**Result:** the app was packaged as a zip with a double-click launcher, and Claude gave
PowerShell steps to check and install Python, packages and the NI-488.2 driver. Problems solved along the way, from
photos of the screen:
- **"Python was not found"**: Windows had linked `python` to the Microsoft Store placeholder → use the `py` launcher.
- **The app closed when the terminal closed** → run it with `pyw` / save as `.pyw`.
- **Double-click still hit the Store placeholder** → a desktop shortcut that points straight at `pyw.exe`.

## 4. Look and feel

> "I want dark theme UI or optional, and plot style like linewidth, scatter plot, line plot should be choosable"

**Result:** a dark/light theme toggle, a Line / Scatter / Line + markers choice, line width and marker size
controls, and settings that are remembered between sessions.

## 5. Cycle sweeps

> "I want to enter max limit, min limit, step size and cycle number. It should always start from 0 source and end there, which is a cycle"

**Result:** a **Cycle** shape (0 → max → min → 0, repeated N times), with a cycle number column in the CSV.

> "Give option to different color cycles"

**Result:** a **Color by cycle** toggle that gives each cycle its own colour and legend entry.

## 6. Error reporting

> "Does the code prompt errors when there is any?"

**Result:** the app now reads the 2400's own error queue (and refuses to switch the output on if the setup was
rejected), shows pop-ups with plain-language hints for GPIB problems, shows a pop-up even for startup crashes,
and writes an error log file.

## 7. Publication figures, time mode, resistance, flexible input

Four requests in one message:
1. *Saved PNG should look like a reference figure* (white background, black frame, no grid), with
   **units chosen automatically** (mV instead of 0.0xx V, µA, …) on both axes.
2. **Constant-source mode** that measures against time.
3. **Real-time resistance**, fitted over the last few points.
4. Number fields should accept `0.1`, `1e-1` **and** `100m`.

## 8. "What else would be useful?"

Claude suggested a list of features. The ones chosen were:
1. **Live saving**: every point is written to disk as it is measured.
2. **Fixed range** for source and measurement (Claude then explained when and why to use it).
3. **Queue**: sweeps, constant steps and waits in sequence, with repeats.
4. **R and R²** from a linear fit, *"but it should not affect the output PNG"*.
5. **Log-log** scale (*"no need for dI/dV"*).

At this stage Claude also tested the real window in a virtual display with simulated data, which caught and fixed a crash
and a layout problem before delivery.

## 9. Making it portable

> "How can we make an executable?"

**Result:** `build_exe.bat` (PyInstaller) produces a single `KeithleyIV.exe` that needs no Python. Only the
NI-488.2 driver is needed to talk to the instrument.

---

## Tips for building your own

- **Start small, then add one feature at a time.** Every step above was tested before the next one.
- **Describe your exact hardware** (instrument model, GPIB/USB adapter); it changes the setup steps.
- **Send photos or screenshots of errors.** Most setup problems were solved that way.
- **Ask for a simulation mode** so you can try the app without the instrument.
- **Use a reference image** when you want a specific figure style.
- **Ask "what else would be useful?"**, then pick what fits your work.
