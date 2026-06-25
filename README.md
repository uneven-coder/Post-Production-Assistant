# PAE — Post Production Assistant

Desktop tool that takes multi-camera recordings, transcribes the audio, generates AI chapter titles, and exports a colour-coded Premiere Pro XML timeline.

Made by Cratior - "This tool uses AI to improve video editing by automatically segmenting projects. Cost is approximately $0.06 for a 10-minute video, varying based on video length, model choice, and other factors."

## Prerequisites

- Python 3.11+
- [ffmpeg](https://ffmpeg.org/) on PATH
- OpenAI API key

## Installation

```bash
pip install -r requirements.txt
cp .env.example .env
# edit .env and set OPENAI_API_KEY
```

## Usage

```bash
python main.py          # opens the UI
```

**important** The default config refrences videos from a non accessible folder so make sure to change this before using.

From the UI:
1. Edit `config.json` in the Config tab or use Browse to load a different file
3. Set your video paths and tags in the Videos tab
4. Press **Run** to execute the full pipeline

The pipeline runs: border processing → transcription → chapter generation → Premiere XML export → (optionally) moves input files to output folder.

## Configuration

All settings live in `config.json`. Key fields:

| Field | Description |
|---|---|
| `project.name` | Project name, used in the output folder name |
| `output_directory` | Path template. Supports `{project_name}`, `{time}`, and `{date}` |
| `output_options.move_inputs` | Move source videos into the output folder when `true` |
| `output_options.date_format` | `strftime` format for `{date}`, default `%Y-%m-%d` |
| `output_options.time_format` | time format for `{time}`, default `%H%M%S` |
| `videos[].tags` | `audio_source` marks which video to transcribe; `main` is the primary track |
| `videos[].overlay` | Position, scale, and border settings for overlay clips |
| `models.transcript_model` | Whisper model for transcription |
| `models.chapter_title_model` | GPT model for generating chapter titles |
| `models.semantic_segmentation_model` | GPT model for classifying transcript segments |

### Output folder naming

```json
"output_directory": "./output/{project_name}_{date}"
```

Produces `./output/My_Project_2025-06-25/` — one folder per run.

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
ai/           OpenAI client (transcription + chat)
border/       9-patch border rendering pipeline (numba-accelerated)
chapters/     Transcript segmentation and chapter title generation
premiere/     XMEML timeline builder
tests/        Smoke tests (run via unittest discover)
assets/       9-patch PNGs, fonts
config.json   Project configuration
config.py     Config loading and path resolution
main.py       Pipeline orchestrator
app.py        Tkinter UI
```
