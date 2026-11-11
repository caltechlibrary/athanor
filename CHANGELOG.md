# Changelog

All notable changes to Athanor are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-11-10

### Added

- `athanor` command that converts uncompressed or losslessly compressed
  video, such as v210, to FFV1/MKV with FLAC audio, and skips lossy video
  such as H.264 access copies.
- Verification of each conversion: the source’s saved MD5, decoded video and
  audio stream hashes, frame counts against the frames stored in the source,
  and significant video properties.
- Every stored frame is kept; a QuickTime edit list is recorded in the log
  rather than applied.
- A timecode track that is one entry at zero with no reel name is dropped; a
  meaningful start and reel name are kept as MKV tags; timecode with breaks,
  other data streams, and audio FLAC can’t hold stop the run for review.
- A `TRANSCODE.md` log for each file, recording the Athanor and ffmpeg
  versions and the commands that ran.
- Progress animation when run in a terminal; plain one-line messages in logs.

[Unreleased]: https://github.com/caltechlibrary/athanor/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/caltechlibrary/athanor/releases/tag/v0.1.0
