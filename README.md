# Athanor

Athanor converts digitized video into lossless FFV1/MKV for long-term
preservation, and proves each conversion is lossless before it is trusted.
It is named for the alchemist’s furnace that holds a steady heat through
long, unattended work, alongside Caltech Archives’ other tools,
[Distillery](https://github.com/caltechlibrary/distillery) and
[Alchemist](https://github.com/caltechlibrary/alchemist).

## Scope

Athanor handles digitized video stored uncompressed, such as 10-bit 4:2:2
v210 in QuickTime. FFV1 reproduces those pictures exactly and typically
stores them in a third to a half of the space.

Lossy video, such as H.264 access copies, is skipped and left as it is: an
FFV1 copy of it would be larger without being any better. Born-digital
formats and audio-only files are outside the current scope.

## What a run does

For each source file, the `athanor` command:

1. Reads the file’s streams and skips it unless its video is uncompressed
   or losslessly compressed.
2. Stops for review when something would be lost in MKV: a timecode track
   that changes partway through, any other data stream, or audio FLAC can’t
   store losslessly. A timecode track holding one entry at zero, with no
   reel name, carries nothing beyond frame position and is dropped.
3. Checks the source against its saved `.md5` file, when there is one.
4. Transcodes to FFV1 (level 3, intra-only, slice CRCs) with FLAC audio,
   keeping every stored frame. A QuickTime edit list is recorded in the log
   rather than applied, because applying it drops frames.
5. Verifies the result: decoded video and audio stream hashes, frame count
   against the frames stored in the source, and significant video properties
   such as size, pixel format, aspect ratio, field order, and color.
6. Writes the MKV, its MD5, and a `TRANSCODE.md` log of every step into a
   timestamped batch directory, then copies the item’s other files beside
   them.

Where the source leaves field order or color range unspecified, ffmpeg
writes its own values into the MKV. The log marks these as ffmpeg’s labels.

Run in a terminal, Athanor animates its progress messages. When its output
goes to a log, such as in GitHub Actions, each message is printed once.

## Requirements

- Python 3.13 or later, managed with [uv](https://docs.astral.sh/uv/).
  Athanor uses only the standard library.
- ffmpeg and ffprobe with the FFV1 and FLAC encoders, such as Homebrew’s
  `ffmpeg-full`. Use one pinned ffmpeg version for a collection: stream
  hashes are comparable only between the same or compatible versions.

## Usage

From a clone of this repository, transcode every eligible file below a
directory of items:

```sh
uv run athanor --level parent \
  --src /path/to/collection --dst /path/to/output \
  --ffmpeg /path/to/ffmpeg --ffprobe /path/to/ffprobe
```

Transcode one file with `--level object --src /path/to/file.mov`. The
`--ffmpeg` and `--ffprobe` options default to the Linuxbrew paths under
`/home/linuxbrew/.linuxbrew/bin/`.

The exit status is 1 when any file fails or needs review; the summary at the
end lists each one with its reason.

To install the command from a release tag instead:

```sh
uv tool install git+https://github.com/caltechlibrary/athanor@v0.1.0
```

## Development

```sh
uv sync --dev
uv run pytest
```

Record changes under “Unreleased” in [CHANGELOG.md](CHANGELOG.md). To
release, move them under a new version heading, and set the same version in
`pyproject.toml`; a push to `main` that changes the version tags the release.

## Direction

- **Archivists start runs from GitHub.** They don’t work on the command
  line, so runs will start from GitHub Actions on a self-hosted runner, with
  the same validate, approve, and execute steps as Distillery Workflows.
  Those workflows, the issue form, and the runner’s settings live in a
  separate private repository, `athanor-workflows`. Running the command by
  hand stays available for development and one-off files.
- **A runner of its own.** A long transcode would otherwise hold the
  preservation runner for hours, so transcoding gets a separate runner
  label.
- **Before Distillery writes a version.** OCFL versions are immutable, so
  the choice between keeping an original and replacing it with FFV1 has to
  be made before Distillery preserves the item.
- **A library Distillery can call.** Distillery will depend on Athanor at a
  release tag, the way it depends on
  [arkminter](https://github.com/caltechlibrary/arkminter), and call its
  functions directly; the `athanor` command will become a thin layer over
  them.
- **Formats as needed.** Support for other source formats, keep-or-transcode
  decisions for lossy sources, audio, and access derivatives such as
  thumbnails is added when a collection calls for it, not ahead of time.

## Open questions

- Whether to correct ffmpeg’s field order and color range labels in the MKV
  to match the source, which needs MKVToolNix’s `mkvpropedit`.
- Which ffmpeg version the runner pins, and how an upgrade is rolled out.

## License

BSD 3-Clause; see [LICENSE](LICENSE).
