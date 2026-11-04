# Soundboard

A Windows desktop soundboard that sends audio files to a selected playback device. Route it to a virtual audio cable to use the sounds as microphone input in Discord, games, meetings, or a recorder. You can mix your real microphone into the same output, with local noise reduction applied only to the microphone.

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
4. Optionally enable **Mix in my microphone** and choose your physical microphone. Leave **Reduce microphone noise** checked to reduce fan noise and typing while preserving sound files.
5. In Discord's **Voice & Video** settings, use the **Custom** input profile if needed to turn **Noise Suppression**, **Echo Cancellation**, and **Automatic Gain Control** off. Soundboard handles your mic noise reduction before mixing; Discord's filters would affect both sources again.
6. Click **Start output**, then click a sound pad. Clicking a pad also starts the selected output if it is disconnected.

```text
Audio files ───────────── sound volume ─┐
                                       ├── CABLE Input → CABLE Output → voice app
Your mic ── noise filter ── mic volume ──┘

Audio files ── sound volume ─── headphones (optional monitoring)
```

The cable's names are from the cable's perspective: its **Input is a playback device**, and its **Output is a recording device**. VB-Audio documents that audio entering CABLE Input is forwarded to CABLE Output. You can use an existing equivalent virtual mixer/cable by selecting its playback endpoint and corresponding recording endpoint.

You do not need to change the Windows default microphone or speaker. Keep your receiving app's speaker output on your headphones. Use headphones for live microphone mixing to avoid acoustic feedback. Do not select the cable's own recording endpoint as the physical microphone; that creates a feedback loop.

## Controls

- **Add sounds** or drag files onto the window: WAV, MP3, FLAC, OGG, AIFF. Mono/stereo clips only, at most five minutes and 128 MB decoded; AAC/M4A is not supported. Files stay in their original locations.
- **Try three demo sounds**: generates three short musical cues locally, with no downloads.
- Click a pad to play it; click it again to stop it. Multiple pads can overlap, up to 32 at once.
- **Shortcuts...** below the pads: click a sound's shortcut field, press a new combination, and **Save**. You can also use a pad's **... → Edit shortcut...** menu. Every sound can have a shortcut, including sounds beyond the first nine. **Clear** removes a shortcut; **Cancel** keeps the previous settings. Changes take effect immediately and persist after restarting.
- **Ctrl+Alt+1–9** are the initial defaults. New sounds use free defaults when available; removing or searching for sounds never shifts existing bindings. Use Ctrl/Alt with letters, digits, or navigation keys; F1–F24 except F12; or number-pad keys with Num Lock on. Shortcuts work while another app is focused. Duplicate, reserved, or already registered combinations are reported before saving. Soundboard hotkeys pause while the editor is open.
- **Ctrl+Alt+0** (configurable as **Stop sounds** in the editor), **Escape** (while focused), or the **Stop sounds** button: stop every pad, including queued loads. **Your microphone stays connected.**
- **Disconnect output**: stop sounds, microphone capture, and headphone monitoring. Disconnect before changing device selections.
- **Loop** and each pad's volume slider apply during playback. The pad menu offers rename and removal. Removal does not delete the audio file.
- **Hear sounds in my headphones**: plays the sound pads to a second device. Your live microphone is excluded from this monitor to prevent a delayed self-echo.
- **Reduce microphone noise**: local RNNoise suppression, enabled by default when mixing your mic. Disconnect output before changing it. It never processes sound pads.
- **Test microphone**: while disconnected, explicitly record up to ten seconds and compare **Original** versus **Filtered** through physical headphones. You can stop recording early; recording does not start just by opening the test. Samples stay in memory and are discarded on closing; test playback never goes through a virtual cable. The test uses the raw microphone level and a separate playback-volume control.
- Library, shortcuts, volume, device selections, and the noise-reduction preference are saved to `data/library.json`. Older libraries keep the first nine default shortcuts and default to noise reduction enabled. Routes and microphone capture start disconnected each launch.

## Microphone noise reduction

The filter runs locally on the CPU, using RNNoise's speech-noise suppression model. It targets everyday fan hiss, typing, and clicks. There is no additional hard voice gate or automatic gain boost, to favor natural quiet speech and intact word beginnings. This does not provide speaker echo cancellation or guaranteed isolation from other people's voices. Continue using headphones.

To evaluate it, disconnect output, select your microphone, and open **Test microphone**. Record a normal sentence, a pause with the fan running, and a quiet sentence while typing. Listen to both versions at the same playback volume. Check word beginnings, quiet words, and typing during speech. If you compare with Discord/Krisp, use the same microphone and repeat the same phrases and noises in a call; the software tests do not establish that RNNoise sounds as good as Krisp.

Filtering uses 48 kHz audio internally and Windows shared-mode conversion where needed. It does not change Windows default devices or their configured formats. A Bluetooth mic's limited capture quality cannot be recovered merely by converting it to 48 kHz. When filtering is off, the route uses the output's native sample rate as before.

If the filter cannot load or fails during playback, the route stops with a message instead of transmitting an unfiltered microphone. Close and reopen with **start.bat** to install missing dependencies. You can explicitly turn noise reduction off while disconnected to use an already-processed mic from another app.

## Troubleshooting

- **No CABLE Input device:** install the driver and restart Windows, then restart Soundboard. Refresh can update the list known to PortAudio; newly installed devices may require an app restart.
- **Sounds play locally but not in chat:** select the cable's playback endpoint in Soundboard and its recording endpoint in the receiving app. Check that input is unmuted and that push-to-talk is open if enabled.
- **Clips are cut off or muffled:** disable noise suppression, echo cancellation, and automatic gain in the receiving app if it treats sound effects as background noise.
- **Mic won't start:** allow desktop microphone access in Windows Settings; close apps using the device exclusively. Shared-mode WASAPI conversion handles many sample-rate differences. If opening still fails, set both devices to 48,000 Hz in their Windows Sound advanced properties where supported.
- **Device unplugged:** the route stops and displays a message. Reconnect the device, refresh, and start output again. Saved missing devices are not silently replaced.
- **Dropouts:** reduce concurrent sounds, use wired headphones, and avoid heavily loaded audio devices. Bluetooth microphones can switch the headset into a lower-quality hands-free profile.
- **Peak limit reached:** reduce sound/pad/mic volume. The mixer clamps output to prevent out-of-range samples; it is not a mastering limiter.
- **Moved file:** remove its old pad and add it from its new location.
- **Shortcut unavailable:** open **Shortcuts...**, choose another combination or clear the binding, then save. Windows/Meta combinations, F12, and Ctrl+Alt+Delete are reserved. Other apps can register shortcuts too; close another Soundboard instance if it is holding your keys. Regular shortcuts inside another app are not always detectable, so choose combinations you do not otherwise use there.

## Implementation and checks

PySide6 provides the desktop interface. `sounddevice`/PortAudio handle playback and capture; `soundfile` decodes audio and SciPy resamples clips without changing pitch. A single worker handles file loading with a 256 MB LRU cache. Audio callbacks mix float32 stereo buffers. A separate microphone worker processes 480-sample mono frames using the native library shipped with `pyrnnoise==0.4.3`; `audiolab==0.5.1` preserves compatibility with the existing Python 3.9 environment. No model downloads or network calls happen during filtering.

Raw and filtered microphone queues each hold at most 60 ms. Overflow discards oldest samples to prevent increasing delay; underruns produce silence. The unfiltered microphone and sound-monitor bridges retain their bounded 120 ms FIFOs. Independent device clocks can produce occasional dropped samples or silence, so this is intended for soundboard use rather than studio synchronization. Microphone audio is never uploaded or saved to disk, including in the test dialog.

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m soundboard.benchmark_noise --seconds 60
```

Tests cover mixing, looping, volume, clipping, bounded queues, resampling, cache limits, route cleanup, persistence, imports, pending-play cancellation, native filtering, arbitrary microphone callback sizes, failures without raw-mic fallback, and test-dialog capture/cancellation/device selection. UI tests use Qt's offscreen backend and fake audio devices; physical routing needs the installed driver and a receiving app to verify end to end.

The benchmark uses generated noise and opens no audio devices. To also generate comparison WAVs from a local speech fixture, add `--speech path\to\speech.wav --output data\validation`. These are explicit benchmark artifacts, separate from the memory-only microphone test.

On this PC, the 60-second generated-audio benchmark measured **1.93 ms p95 computation per 10 ms frame**, **20 ms signal delay**, and zero dropped samples in the paced worker check. Noise-only RMS fell by 32.8 dB; the generated speech-plus-noise fixture showed only a small speech-region SNR improvement (10.68 to 10.83 dB). These synthetic measurements are not a listening evaluation or a comparison with Krisp, and exclude audio-device and network latency. The local report and comparison WAVs are in `data/validation`.

API references: [sounddevice streams](https://python-sounddevice.readthedocs.io/en/0.5.3/api/streams.html), [soundfile](https://python-soundfile.readthedocs.io/en/latest/), [Windows global hotkeys](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-registerhotkey), [RNNoise](https://github.com/xiph/rnnoise), [pyrnnoise](https://github.com/pengzhendong/pyrnnoise).
