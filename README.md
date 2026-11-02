# Soundboard

A Windows desktop soundboard that sends audio files to a selected playback device. Route it to a virtual audio cable to use the sounds as microphone input in Discord, games, meetings, or a recorder. You can mix your real microphone into the same output.

## Run

Double-click **start.bat**. The launcher uses the local `.venv` and installs missing dependencies on first launch. Python 3.9–3.12, 64-bit, is supported by the pinned dependency set. Internet access is needed for the first dependency installation.

Manual setup in PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

The existing project environment uses Python 3.9.13 and is ready to run. A newer Python installation is not required for that environment.

## Send sounds into your microphone input

Windows applications receive microphone audio through a recording device. This app needs a virtual audio driver to expose its playback as a recording device; it does not install a driver itself.

1. Download **VB-CABLE** from the [official VB-Audio website](https://vb-audio.com/Cable/). Extract it, run its installer as administrator, and restart Windows as instructed by VB-Audio.
2. Open Soundboard. Select **CABLE Input (VB-Audio Virtual Cable)** under **Send sounds to**.
3. In the app receiving your audio, choose **CABLE Output (VB-Audio Virtual Cable)** as its microphone/input.
4. Optionally enable **Mix in my microphone** and choose your physical microphone. This sends your voice and sounds together.
5. Click **Start output**, then click a sound pad. Clicking a pad also starts the selected output if it is disconnected.

```text
Audio files ── sound volume ─┐
                            ├── CABLE Input → CABLE Output → voice app
Your mic ──── mic volume ────┘

Audio files ── sound volume ─── headphones (optional monitoring)
```

The cable's names are from the cable's perspective: its **Input is a playback device**, and its **Output is a recording device**. VB-Audio documents that audio entering CABLE Input is forwarded to CABLE Output. You can use an existing equivalent virtual mixer/cable by selecting its playback endpoint and corresponding recording endpoint.

You do not need to change the Windows default microphone or speaker. Keep your receiving app's speaker output on your headphones. Use headphones for live microphone mixing to avoid acoustic feedback. Do not select the cable's own recording endpoint as the physical microphone; that creates a feedback loop.

## Controls

- **Add sounds** or drag files onto the window: WAV, MP3, FLAC, OGG, AIFF. Mono/stereo clips only, at most five minutes and 128 MB decoded; AAC/M4A is not supported. Files stay in their original locations.
- **Try three demo sounds**: generates three short musical cues locally, with no downloads.
- Click a pad to play it; click it again to stop it. Multiple pads can overlap, up to 32 at once.
- **Ctrl+Alt+1–9**: play/stop the first nine pads, even with another app focused. Filtering does not change pad assignments. These hotkeys are registered on Windows; unavailable combinations are reported in the app.
- **Ctrl+Alt+0**, **Escape** (while focused), or **Stop sounds**: stop every pad, including queued loads. **Your microphone stays connected.**
- **Disconnect output**: stop sounds, microphone capture, and headphone monitoring. Disconnect before changing device selections.
- **Loop** and each pad's volume slider apply during playback. The pad menu offers rename and removal. Removal does not delete the audio file.
- **Hear sounds in my headphones**: plays the sound pads to a second device. Your live microphone is excluded from this monitor to prevent a delayed self-echo.
- Library, volume, and device selections are saved to `data/library.json`. Routes and microphone capture start disconnected each launch.

## Troubleshooting

- **No CABLE Input device:** install the driver and restart Windows, then restart Soundboard. Refresh can update the list known to PortAudio; newly installed devices may require an app restart.
- **Sounds play locally but not in chat:** select the cable's playback endpoint in Soundboard and its recording endpoint in the receiving app. Check that input is unmuted and that push-to-talk is open if enabled.
- **Clips are cut off or muffled:** disable noise suppression, echo cancellation, and automatic gain in the receiving app if it treats sound effects as background noise.
- **Mic won't start:** allow desktop microphone access in Windows Settings; close apps using the device exclusively. Shared-mode WASAPI conversion handles many sample-rate differences. If opening still fails, set both devices to 48,000 Hz in their Windows Sound advanced properties where supported.
- **Device unplugged:** the route stops and displays a message. Reconnect the device, refresh, and start output again. Saved missing devices are not silently replaced.
- **Dropouts:** reduce concurrent sounds, use wired headphones, and avoid heavily loaded audio devices. Bluetooth microphones can switch the headset into a lower-quality hands-free profile.
- **Peak limit reached:** reduce sound/pad/mic volume. The mixer clamps output to prevent out-of-range samples; it is not a mastering limiter.
- **Moved file:** remove its old pad and add it from its new location.

## Implementation and checks

PySide6 provides the desktop interface. `sounddevice`/PortAudio handle playback and capture; `soundfile` decodes audio and SciPy resamples clips without changing pitch. A single worker handles file loading with a 256 MB LRU cache. Audio callbacks mix float32 stereo buffers. Microphone and monitor bridges use bounded 120 ms FIFOs; independent device clocks can produce occasional dropped samples or silence, so this is intended for soundboard use rather than studio synchronization. Audio is never uploaded or recorded to disk.

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m pip check
```

Tests cover mixing, looping, volume, clipping, bounded queues, resampling, cache limits, route cleanup, persistence, imports, and pending-play cancellation. UI tests use Qt's offscreen backend and fake audio devices; physical routing needs the installed driver and a receiving app to verify end to end.

API references: [sounddevice streams](https://python-sounddevice.readthedocs.io/en/0.5.3/api/streams.html), [soundfile](https://python-soundfile.readthedocs.io/en/latest/), [Windows global hotkeys](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-registerhotkey).
