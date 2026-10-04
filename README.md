# course-offline-dl

A small Python tool that saves the lecture videos and subtitles of Coursera courses **you are enrolled in**, so you can watch them offline: on a flight, on a slow connection, or just without 14 browser tabs open.

I built it because I had a paid Coursera subscription, patchy internet, and wanted my courses in a normal folder I could open with VLC. The older community tool for this stopped working after Coursera changed its login and API, so I wrote a fresh one and tried to make it hard to break.

> **Not affiliated with Coursera.** This is an unofficial personal project. Please read the [disclaimer](#disclaimer) before using it.

---

## What it does

- Downloads every lecture video of a course, in course order, into tidy module folders
- Saves subtitles (and optionally plain-text transcripts) next to each video
- Resumes interrupted downloads instead of starting over
- Retries on network hiccups and refreshes expired links automatically
- Skips files you already have, so re-running is safe
- Writes a log and a JSON report so you can see exactly what happened

What it does **not** do: quizzes, readings, labs and assignments. Videos and subtitles only.

## Quick start

You need Python 3.9 or newer.

```bash
git clone https://github.com/TheUsmanDev/course-offline-dl.git
cd course-offline-dl
pip install -r requirements.txt
```

### 1. Export your cookies

The tool never asks for your password. Instead it borrows the login session your browser already has.

1. Log in to [coursera.org](https://www.coursera.org) in your browser.
2. Install a cookie-export extension (for example "Get cookies.txt LOCALLY").
3. While on a Coursera tab, export cookies and save the file as `cookies.txt` in this folder.

### 2. Find the course slug

Open your course. The URL looks like this:

```
https://www.coursera.org/learn/python-for-everybody/home/...
```

The slug is the part after `/learn/`, here `python-for-everybody`. You can also paste the whole URL.

### 3. Peek first, then download

```bash
# See what the course contains (downloads nothing)
python coursera_downloader_upgraded.py python-for-everybody --cookies cookies.txt --list

# Download it
python coursera_downloader_upgraded.py python-for-everybody --cookies cookies.txt
```

Your files appear in `coursera_downloads/<course-slug>/`.

## What you get

```
coursera_downloads/
└── python-for-everybody/
    ├── 01 - Getting Started/
    │   ├── 01.01 - Welcome.mp4
    │   ├── 01.01 - Welcome.en.vtt
    │   ├── 01.02 - Installing Python.mp4
    │   └── ...
    ├── 02 - Essential Python/
    ├── download.log
    └── report.json
```

File names start with `lesson.lecture` numbers, so they sort in the same order as the course itself. Subtitle files share the video's name, so VLC picks them up automatically.

## Options

| Option | What it does | Default |
|---|---|---|
| `--cookies FILE` | Your exported cookies file | `cookies.txt` |
| `--out DIR` | Where to save everything | `coursera_downloads` |
| `--resolution 540p` | Preferred video quality (falls back to the best available) | `720p` |
| `--sub-langs en,ur` | Subtitle languages, comma separated | `en` |
| `--no-subs` | Skip subtitles | off |
| `--transcripts` | Also save plain-text transcripts | off |
| `--subs-only` | Skip videos, grab subtitles only | off |
| `--modules 1,3-5` | Only these modules | all |
| `--list` | Show the course structure and exit | off |
| `--workers N` | Parallel downloads (1 to 6) | `2` |
| `--api-delay S` | Minimum seconds between API calls | `0.5` |
| `--debug-api` | Save the raw course-structure response for troubleshooting | off |
| `-v` | More detailed console output | off |

Run `python coursera_downloader_upgraded.py --help` for the full list.

## Troubleshooting

**`Auth error` or `403`**
Your cookies expired. Log in again in the browser, re-export `cookies.txt`, and run the same command. Finished files are skipped, so you lose nothing.

**`Course nahi mila` / course not found**
Check the slug, and make sure the account you exported cookies from is actually enrolled in that course.

**"No lectures found"**
Coursera changes its API from time to time. Run again with `--debug-api` and open an issue with the `Diagnostic:` line from the output. That line contains only counts and field names, no personal data.

**A video has no sound**
Check your headphones first (this happened to me). If not, open the file in VLC. Still silent? Play the same lecture on coursera.org to see whether the original is silent.

**Some lectures failed**
Check `report.json` for the reason, then simply run the command again. It only retries what is missing.

## Good to know

- This relies on Coursera's internal web API, which is **not** a public, stable API. It can break without warning.
- I have tested it on a handful of courses. Yours might behave differently. If it does, please open an issue.
- Treat `cookies.txt` like a password. Anyone who has it can act as you on Coursera. It is already listed in `.gitignore`; keep it that way and delete it when you are done.

## Disclaimer

This tool is for **personal, offline use of material you have legitimate access to**, such as courses you are enrolled in or have paid for. Coursera's Terms of Use allow personal, non-commercial downloading only, and do not allow redistributing or selling course material. Please follow them.

Do not use this tool to share, republish or sell course content, or to access content you are not entitled to. You are responsible for how you use it. The software is provided "as is", without warranty of any kind, and the author is not liable for any consequences of its use, including account actions taken by Coursera.

"Coursera" is a trademark of Coursera, Inc. This project is not affiliated with, endorsed by, or sponsored by Coursera.

## Contributing

Bug reports and pull requests are welcome, especially when Coursera changes something and the tool needs a fix. When opening an issue, please include:

- Your OS and Python version
- The exact command you ran (with any personal info removed)
- The error message, or the `Diagnostic:` line if no lectures were found
- The relevant part of `download.log`

**Never attach your `cookies.txt` to an issue.**

## License

MIT. See [LICENSE](LICENSE).

Made by [TheUsmanDev](https://github.com/TheUsmanDev). If it saved you some time, a star on the repo is always appreciated. ⭐
