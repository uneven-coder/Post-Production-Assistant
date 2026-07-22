# PAE - Post Production Assistant

Desktop tool that takes multi-camera recordings, transcribes the audio, generates AI chapter titles, and exports a colour-coded Premiere Pro XML timeline.

Made by Cratior - "This tool uses AI to improve video editing by automatically segmenting projects. Cost is approximately $0.06 for a 10-minute video, varying based on video length, model choice, and other factors."

this has been made with help from ai, generating code, lots of documentation and stuff, but it has all been reviewed and tested, this project has grown in scope and complexity.
## Prerequisites

- Python 3.11+
- [ffmpeg](https://ffmpeg.org/) on PATH
- OpenAI API key
- [VLC media player](https://www.videolan.org/vlc/) installed (optional) - allows for the scrub/play preview in the Timeline tab.

## Installation

```bash
pip install -r requirements.txt
cp .env.example .env
# edit .env and set OPENAI_API_KEY
```

## Usage

```bash
python main.py                       # opens the UI
python main.py --run                 # headless run of config.json, no UI
python main.py --config PATH         # headless run of a specific config file
python main.py --silence-only        # headless run of config.json's silence_only_profile, silence-only
python main.py --youtube-silent-only # download a YouTube video from your clipboard, silence-only, then edit its cuts - see below
```

**important** The default config refrences videos from a non accessible folder so make sure to change this before using.

From the UI:
1. Edit `config.json` in the Config tab or use Browse to load a different file
3. Set your video paths and tags in the Videos tab
4. Press **Run** to execute the full pipeline, or **Silence Only** to run the `silence_only_profile`

The pipeline runs: silence detection (if enabled) → border processing → transcription → chapter generation → Premiere XML export → (optionally) moves input files to output folder.

## Configuration

All settings live in `config.json`, under `project`, split into **profiles**:

```json
"project": {
  "main_profile": { /* every setting below - the base configuration */ },
  "silence_only_profile": { /* shallow-merged over main_profile for --silence-only */ },
  "youtube_automation_profile": { /* shallow-merged over main_profile for --youtube-silent-only */ }
}
```

`main_profile` holds the full base configuration and is what a normal **Run** uses. The other two profiles are small override blocks - any key you set in them replaces the same key from `main_profile` (shallow merge, one level deep) when that shortcut runs; everything else is inherited unchanged. This is also how the Config tab, headless CLI, and `remove_silence.bat` / `youtube_silent_only.bat` all resolve settings, so editing `main_profile` changes every entry point at once.

The field table below lists paths relative to `main_profile` (i.e. `name` means `project.main_profile.name`) unless noted otherwise:

| Field | Description |
|---|---|
| `name` | Project name, used in the output folder name |
| `output_directory` | Path template. Supports `{project_name}`, `{time}`, and `{date}` |
| `output_options.move_inputs` | Move source videos into the output folder when `true` |
| `output_options.date_format` | `strftime` format for `{date}`, default `%Y-%m-%d` |
| `output_options.time_format` | time format for `{time}`, default `%H%M%S` |
| `videos[].tags` | `audio_source` marks which video to transcribe; `main` is the primary track |
| `videos[].overlay` | Position, scale, and border settings for overlay clips |
| `models.transcript_model` | Whisper model for transcription |
| `models.chapter_title_model` | GPT model for generating chapter titles |
| `models.semantic_segmentation_model` | GPT model for classifying transcript segments |
| `silence_removal.mode` | `off`, `mark`, or `only` - see [Silence removal](#silence-removal) |
| `silence_removal.min_silence_duration_s` | Minimum length of a quiet stretch to count as silence (default `0.6`) |
| `silence_removal.threshold_db` | Volume below which audio is considered silent (default `-35`) |
| `silence_removal.padding_s` | Seconds trimmed off each edge of a detected silence window so speech isn't clipped (default `0.12`) |
| `silence_removal.use_transcript` | When `true` (and mode is `mark`/`only`), the full audio is transcribed first and the transcript refines the silence cuts: quiet speech below the dB threshold is rescued, cut edges snap to word boundaries, and Whisper-flagged non-speech stretches become extra cut candidates. Transcribes the full (untrimmed) audio, so Whisper cost is higher than the pre-trimmed `mark` flow |
| `silence_removal.transcript.remove_segment_types` | Chapter-classification types (e.g. `["irrelevant"]`) whose windows are added to the cut list — requires `use_transcript` |
| `silence_removal.transcript.min_confidence` | Minimum classification confidence for a window to be cut (default `0.7`) |
| `silence_removal.transcript.speech_pad_s` | Padding around each transcribed word protected from cutting (default `0.08`) |
| `silence_removal.transcript.no_speech_prob_threshold` | Whisper `no_speech_prob` above which a segment counts as non-speech (default `0.85`) |
| `silence_removal.transcript.add_no_speech_segments` | Also cut loud-but-content-free stretches Whisper marks as non-speech (default `true`) |
| `silence_removal.long_pause.enabled` | Catch larger "nothing happening" sections: adds a loose second detection pass and (with `use_transcript`) cuts long no-speech gaps regardless of loudness (default `true` in the main profile) |
| `silence_removal.long_pause.min_speech_gap_s` | Stretches with no transcribed speech longer than this become cuts, even over music/background noise — requires `use_transcript` (default `4.0`) |
| `silence_removal.long_pause.edge_pad_s` | Breathing room left at each edge of a removed speech gap (default `0.5`) |
| `silence_removal.long_pause.loose_threshold_db` | Second detection pass threshold — more lenient than `threshold_db`, so quiet-but-not-silent audio qualifies (default `-25`) |
| `silence_removal.long_pause.loose_min_duration_s` | Only stretches at least this long are kept from the loose pass, so normal speech pauses aren't touched (default `5.0`) |
| `transcript_windows.target_segment_length_s` | Transcript window size for classification — smaller windows mean finer-grained chapters (default `60`) |
| `chapters.merging.max_chapter_duration_s` | Force a chapter split past this length (default `300`) |
| `chapters.merging.min_chapter_duration_s` | Chapters shorter than this are folded into a neighbor (default `45`) |
| `chapters.merging.split_on_topic_change` | Split same-type chapters when the classified topic shifts (default `true`) |
| `youtube_download.download_directory` | Where `--youtube-silent-only` saves the downloaded video, default `./videos/youtube_downloads/` |
| *(sibling of `main_profile`)* `silence_only_profile` | Optional override block shallow-merged over `main_profile` for `--silence-only` / **⚡ Silence Only** - see [Silence removal](#silence-removal) |
| *(sibling of `main_profile`)* `youtube_automation_profile` | Optional override block for `--youtube-silent-only` - see [YouTube Silent-Only Shortcut](#youtube-silent-only-shortcut) |

### Output folder naming

```json
"output_directory": "./output/{project_name}_{date}"
```

Produces `./output/My_Project_2025-06-25/` - one folder per run.

### Video tags

```json
"tags": ["audio_source", "main"]   // primary camera, transcribed
"tags": ["overlay"]                 // secondary camera / screen capture
```

### Adding a 9-patch border to an overlay

```json
"border": {
  "enabled": true,
  "nine_patch_path": { "path": "./assets/overlay_9patch.png" },
  "title": "Cam",
  "scale": 0.65,
  "background_color": [0, 0, 0],
  "font_path": "./assets/FuturaHeavy.otf",
  "font_size": 84
}
```

## Silence Removal

Set `project.main_profile.silence_removal.mode` in the config file to one of the following:

* **`off`** (default) - No silence detection. The pipeline behaves exactly as before.
* **`mark`** - Detects silence on the audio-source video, transcribes a silence-trimmed copy of the audio (resulting in a cleaner and cheaper transcript), then remaps the generated chapters back onto the original timeline. This runs alongside the normal pipeline (borders, transcript generation, and AI chapters).
* **`only`** - Skips borders, transcription, and AI chapter generation entirely and exports only a timeline with silent sections flagged. This is the fastest option for processing raw stream footage.

In both `mark` and `only` modes, silent clip segments are written to the exported Premiere `.xml` with `<enabled>FALSE</enabled>`. Premiere Pro natively renders disabled clips using grey diagonal hatching, providing a non-destructive visual indicator for every detected silent section without deleting anything.

Once reviewed, simply ripple-delete the hatched clips in Premiere. Video and audio clips are cut and disabled at identical points, ensuring everything remains in sync.

Silence detection uses the [`unsilence`](https://pypi.org/project/unsilence/) library's interval model to merge short or fragmented intervals and pad boundaries, rather than applying a simple per-interval cut. This avoids producing a large number of tiny cuts during naturally paced speech.

### Silence-Only Shortcut

`project.silence_only_profile` in `config.json` acts as an override block for quickly cleaning up recordings without modifying your main configuration. Any keys defined within it are shallow-merged over `project.main_profile` (typically a single video with borders disabled and `silence_removal.mode` set to `"only"`).

```json
"project": {
  "main_profile": { /* ... */ },
  "silence_only_profile": {
    "videos": [ /* single video, audio_source + main, border disabled */ ],
    "silence_removal": { "mode": "only" }
  }
}
```

Run it using:

```bash
python main.py --silence-only
```

Alternatively, you can double-click `remove_silence.bat` or click **Silence Only** in the UI toolbar. All three methods apply `silence_only_profile` on top of `main_profile` and force `silence_removal.mode` to `"only"` without changing what is saved to disk or displayed in the Config tab.

### YouTube Silent-Only Shortcut

Same idea as the shortcut above, but for a video that's *already* on YouTube instead of a local file:

1. Copy a YouTube Studio URL to your clipboard - `https://studio.youtube.com/video/VIDEO_ID/edit`, a live stream's URL, or a plain `youtube.com/watch?v=` / `youtu.be` link all work.
2. Run `python main.py --youtube-silent-only`, or double-click `youtube_silent_only.bat`.

It then, in order:

1. Reads the video ID from the clipboard.
2. Downloads the original file via YouTube Studio's own **⋮ → Download** action into `youtube_download.download_directory` (default `./videos/youtube_downloads/`).
3. Runs the silence-only pipeline on that download, applying `project.youtube_automation_profile` (a `silence_removal.mode: "only"` override, same shallow-merge behavior over `main_profile` as `silence_only_profile`).
4. Opens that same video's **Editor → Trim & cut** panel and shows a live progress bar directly on the page (download %, then pipeline stage). Once cuts are known, it applies every one of them automatically - no button, no click - then plays a notification sound when done. Because the video is already uploaded, there's no re-upload step.

```json
"project": {
  "main_profile": {
    "youtube_download": { "download_directory": "./videos/youtube_downloads/" }
  },
  "youtube_automation_profile": {
    "silence_removal": { "mode": "only" }
  }
}
```

---

## Timeline Preview

The Timeline tab always provides a preview of the edited result:

1. It may take a few moments to generate, but a thumbnail of the output (including overlays and borders positioned as they will appear in Premiere) is displayed automatically.
2. Clicking the thumbnail compiles a real video. Overlays and borders are composited, and if silence detection was used, silent sections are hard-cut out to match exactly what ripple-deleting the Premiere clips would produce.
3. Once compilation is complete, the thumbnail is replaced with an embedded VLC-based player featuring play/pause controls, seeking, and current/total duration indicators.

The preview is not 100% accurate. It reuses the same positioning constants as `premiere/builder.py` for overlay placement, but it should be treated as an approximation rather than an authoritative representation of the final output.

---

## YouTube Upload Automation

`project.youtube_automation` can upload videos directly to YouTube Studio and automatically mark detected silent sections using the **Editor → Cuts** tool. This allows YouTube to perform the final re-encode server-side instead of generating a locally rendered file.

While this approach is less efficient than local rendering, it allows the resulting edits to remain editable within YouTube Studio.

```json
"youtube_automation": {
  "enabled": false,
  "auto_launch_on_silence_only": true,
  "profile_dir": "",
  "browser_channel": "",
  "title_template": "{project_name}"
}
```

| Field                         | Description                                                                                                                         |
| ----------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| `enabled`                     | Master switch. When `false`, the **YouTube** toolbar button will still prompt before running.                                    |
| `auto_launch_on_silence_only` | If `enabled` is also `true`, automation launches automatically after `--silence-only` or `remove_silence.bat` completes.            |
| `profile_dir`                 | Chrome/Edge profile directory to reuse across runs (defaults to `~/.pae_chrome_profile`). You only need to log in once per profile. |
| `browser_channel`             | Force `"chrome"` or `"msedge"`. Leave blank to try Chrome first and fall back to Edge if necessary.                                 |
| `title_template`              | Video title template. `{project_name}` is automatically substituted.                                                                |

### How It Works

The feature uses [Playwright](https://playwright.dev/) to launch a Chrome or Edge window via `launch_persistent_context` (using the same DevTools protocol as Chrome's own developer tools). You can watch the automation in real time and take over the mouse or keyboard at any point.

The first run prompts you to log into the appropriate Google account. The selected browser profile is then reused for future runs.

The automation:

* Uploads the audio-source video.
* Completes the upload wizard without modifying the video's visibility settings (allowing YouTube to retain its default, typically **Private**).
* Creates a cut for each detected silent interval.

It intentionally does **not** click the Cuts editor's **Save** button. This final review and confirmation step is left to the user.

You can launch automation from the **YouTube** toolbar button at any time - it opens a dialog to pick which output to use (whatever you just ran this session, or any past output on disk) before starting - or automatically via `remove_silence.bat` or `python main.py --silence-only` when `auto_launch_on_silence_only` is enabled.

Once automation completes, the console pauses and waits for user input so the browser window remains open while you review, edit, or publish the video.

### Caveats

* Cuts are applied by calling the Trim & cut panel's own internal component methods directly (`addNewCutAtTime`/`approveCutById` etc. on its underlying element) rather than clicking through the UI - this avoids simulating hundreds of individual clicks, but it's an undocumented internal API rather than a stable public one, so a YouTube Studio update is more likely to break it silently than a labels/roles-based approach would. If a cut fails to apply, the failure is reported (which cut, and the underlying JS error) rather than applied manually.
* Applying a large number of cuts (a long stream can easily produce several hundred) is slow because Studio's own Trim & cut panel re-renders the timeline and cut list after every single insertion. To avoid the tab visibly freezing while that happens, the timeline and cut-row list are hidden for the whole batch (the progress bar shows a "UI paused" message in their place) and restored once done; a short pause between each cut still keeps the tab's event loop breathing throughout so it never looks fully unresponsive.
* Automating uploads on a personal account exists in a grey area of YouTube's terms regarding automated access. This feature is intended for individual workflows and is not designed for large-scale automation.
* Requires Google Chrome or Microsoft Edge to be installed locally.
* Chrome must not already be running when automation starts. If Chrome is open, the browser may briefly appear and close immediately, followed by a `"profile already open"` error on subsequent runs.
* If Chrome continues running in the background after all windows have been closed, disable **Continue running background apps when Google Chrome is closed** (`chrome://settings/system`) and terminate any remaining `chrome.exe` processes using Task Manager.
* Run PAE from a standard, non-elevated terminal. Running as Administrator causes Chrome to disable its sandbox and display an "unsupported command-line flag" warning banner on every launch.
* `--youtube-silent-only` reads the video URL from the clipboard (`Ctrl+C` it yourself before running) - it does not attempt to read whatever's currently selected on screen.
* `--youtube-silent-only` requires the signed-in automation profile to have access to the video (i.e. own or manage it) - Studio's Download action isn't available otherwise. Its menu/download selectors are just as best-effort as the rest of this feature; if Studio's layout changes, download it manually instead.


## Output

```
output/My_Project_2025-06-25/
├── My_Project.xml          # Premiere Pro / FCPXML timeline
├── chapters.json           # chapter list with timestamps and segment types
└── *_bordered.png          # rendered border images for overlay clips
```

Import `My_Project.xml` into Premiere Pro via **File → Import**.

## Project structure

```
ai/                 OpenAI client (transcription + chat)
border/             9-patch border rendering pipeline (numba-accelerated)
chapters/           Transcript segmentation and chapter title generation
premiere/           XMEML timeline builder
silence/            ffmpeg + unsilence-based silence detection and trimming
preview/            ffmpeg-based Timeline tab scrub/play preview renderer
youtube_automation/ Playwright-driven YouTube Studio upload/download + Cuts automation
tests/              Smoke tests (run via unittest discover)
assets/             9-patch PNGs, fonts
config.json         Project configuration (includes the silence_only_profile override)
config.py           Config loading, profile overrides, and path resolution
main.py             Pipeline orchestrator + headless CLI
app.py              Tkinter UI
remove_silence.bat          Double-click shortcut for --silence-only
youtube_silent_only.bat     Double-click shortcut for --youtube-silent-only
```
