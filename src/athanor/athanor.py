"""Athanor: convert digitized video to lossless FFV1/MKV and verify the result."""

import argparse
import atexit
import concurrent.futures
import datetime
import hashlib
import importlib.metadata
import itertools
import json
import re
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import time
import threading

from pathlib import Path
from typing import List, Optional

# Define video extensions globally for DRY usage
VIDEO_EXTS = [
    ".mov", ".mp4", ".mkv", ".avi", ".mxf", ".webm", ".flv", ".wmv", ".mpg", ".mpeg", ".3gp", ".ogg", ".ogv"
]

# Video codecs that store pictures uncompressed or losslessly compressed.
# Only these are transcoded: FFV1 reproduces them exactly and is usually
# smaller. Anything else (H.264, ProRes, DV, MPEG-2, and so on) is lossy, and
# an FFV1 copy of it is larger without being any better, so it is left as is.
TRANSCODABLE_VIDEO_CODECS = {
    "012v", "ayuv", "bitpacked", "r10k", "r210", "rawvideo", "v210", "v308", "v408", "v410", "yuv4",
    "ffvhuff", "huffyuv", "magicyuv", "utvideo",
}

# Video properties that must come through the transcode unchanged.
SIGNIFICANT_VIDEO_PROPERTIES = [
    "width", "height", "pix_fmt", "sample_aspect_ratio", "display_aspect_ratio", "field_order",
    "color_range", "color_space", "color_transfer", "color_primaries", "r_frame_rate",
]
UNSPECIFIED_PROPERTY_VALUES = {None, "", "unknown", "0:1"}

# A timecode track holding one entry that starts at zero carries nothing
# beyond each frame's position in the file.
ZERO_TIMECODE = re.compile(r"^00[:;.]00[:;.]00[:;.]00$")

# FLAC stores integer PCM up to 24 bits and up to 8 channels losslessly.
FLAC_MAX_CHANNELS = 8
FLAC_UNSUPPORTED_AUDIO_CODECS = re.compile(r"^pcm_(f\d+|s32|s64|u32)")

# Default ffmpeg and ffprobe, from Linuxbrew; the command can point elsewhere.
FFMPEG_CMD = "/home/linuxbrew/.linuxbrew/bin/ffmpeg"
FFPROBE_CMD = "/home/linuxbrew/.linuxbrew/bin/ffprobe"


def athanor_version() -> str:
    try:
        return importlib.metadata.version("athanor")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def restore_tty():
    if sys.stdin.isatty():
        subprocess.run(['stty','sane'])


def auto_cleanup_processes(func):
    """Decorator that ensures subprocess cleanup on exit"""
    processes = []
    original_popen = None  # Declare in outer scope

    def cleanup():
        for proc in processes:
            if proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                except (ProcessLookupError, OSError):
                    pass

    def patched_popen(*args, **kwargs):
        # Use the original Popen to avoid recursion
        if original_popen is None:
            raise RuntimeError("subprocess.Popen patching failed")
        proc = original_popen(*args, **kwargs)
        processes.append(proc)
        return proc

    def wrapper(*args, **kwargs):
        # Setup cleanup
        atexit.register(cleanup)
        signal.signal(signal.SIGINT, lambda s, f: (cleanup(), exit(0)))

        # Store original Popen and patch it
        nonlocal original_popen
        original_popen = subprocess.Popen
        subprocess.Popen = patched_popen

        try:
            return func(*args, **kwargs)
        finally:
            subprocess.Popen = original_popen  # Restore original
            cleanup()

    return wrapper


class Spinner:
    """Animate a waiting message in a terminal.

    When output goes anywhere else, such as a GitHub Actions log, the message
    is printed once instead, so the log isn't filled with carriage returns.
    """
    # https://stackoverflow.com/a/57974583

    __default_spinner_symbols_list = ['.    ', '..   ', '...  ', '.... ', '.....', ' ....', '  ...', '   ..', '    .', '     ']

    def __init__(self, spinner_symbols_list: Optional[List[str]] = None):
        spinner_symbols_list = spinner_symbols_list if spinner_symbols_list else Spinner.__default_spinner_symbols_list
        self.__screen_lock = threading.Event()
        self.__spinner = itertools.cycle(spinner_symbols_list)
        self.__stop_event = False
        self.__thread = None
        self.__animated = sys.stdout.isatty()

    def get_spin(self):
        return self.__spinner

    def start(self, spinner_message: str):
        self.__stop_event = False
        if not self.__animated:
            print(spinner_message, flush=True)
            return
        time.sleep(0.3)

        def run_spinner(message):
            while not self.__stop_event:
                print("\r{message} {spinner}".format(message=message, spinner=next(self.__spinner)), end="")
                time.sleep(0.3)

            self.__screen_lock.set()

        self.__thread = threading.Thread(target=run_spinner, args=(spinner_message,), daemon=True)
        self.__thread.start()

    def stop(self):
        self.__stop_event = True
        if not self.__animated:
            return
        if self.__screen_lock.is_set():
            self.__screen_lock.wait()
            self.__screen_lock.clear()
            print("\r", end="")

        print()


def probe(path: Path) -> dict:
    """Return ffprobe's streams and format for path."""
    result = subprocess.run(
        [FFPROBE_CMD, "-v", "error", "-show_format", "-show_streams", "-of", "json", path.as_posix()],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise SystemExit(f"ffprobe failed on {path.name}: {result.stderr.strip()}")
    return json.loads(result.stdout)


def streams_of_type(probed: dict, codec_type: str) -> list:
    return [
        stream for stream in probed.get("streams", [])
        if stream.get("codec_type") == codec_type
        and not stream.get("disposition", {}).get("attached_pic")
    ]


def is_mov_family(probed: dict) -> bool:
    return "mov" in probed.get("format", {}).get("format_name", "").split(",")


def count_video_frames(path: Path, input_options: List[str]) -> List[int]:
    """Count the frames in each video stream by reading every packet."""
    result = subprocess.run(
        [
            FFPROBE_CMD, "-v", "error", *input_options, "-select_streams", "v", "-count_packets",
            "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", path.as_posix(),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise SystemExit(f"Frame count failed on {path.name}: {result.stderr.strip()}")
    return [int(value) for value in result.stdout.split()]


def md5_of_file(path: Path) -> str:
    digest = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def iter_atoms(data: bytes, start: int, end: int):
    """Yield (type, body_start, body_end) for each QuickTime atom in data[start:end]."""
    position = start
    while position + 8 <= end:
        size, kind = struct.unpack_from(">I4s", data, position)
        header = 8
        if size == 1:
            size = struct.unpack_from(">Q", data, position + 8)[0]
            header = 16
        elif size == 0:
            size = end - position
        if size < header or position + size > end:
            return
        yield kind, position + header, position + size
        position += size


def find_atom(data: bytes, start: int, end: int, *path: bytes):
    """Return (body_start, body_end) of the first atom at path below data[start:end]."""
    for kind, body_start, body_end in iter_atoms(data, start, end):
        if kind == path[0]:
            if len(path) == 1:
                return body_start, body_end
            found = find_atom(data, body_start, body_end, *path[1:])
            if found:
                return found
    return None


def read_moov_body(path: Path) -> Optional[bytes]:
    """Return the body of a QuickTime file's moov atom without reading the media data."""
    with open(path, "rb") as f:
        f.seek(0, 2)
        end = f.tell()
        position = 0
        while position + 8 <= end:
            f.seek(position)
            header = f.read(16)
            size, kind = struct.unpack_from(">I4s", header)
            header_size = 8
            if size == 1:
                size = struct.unpack_from(">Q", header, 8)[0]
                header_size = 16
            elif size == 0:
                size = end - position
            if size < header_size:
                return None
            if kind == b"moov":
                f.seek(position + header_size)
                return f.read(size - header_size)
            position += size
    return None


def read_timescale_and_duration(data: bytes, header: tuple):
    """Read the timescale and duration from an mvhd or mdhd atom body."""
    start = header[0]
    if data[start] == 1:
        return struct.unpack_from(">IQ", data, start + 20)
    return struct.unpack_from(">II", data, start + 12)


def read_mov_video_edit_lists(path: Path) -> List[dict]:
    """Describe the edit list of each video track in a QuickTime file.

    An edit list tells players which stretches of the stored frames to show.
    Each edit is returned in seconds: `media_start` is where it starts in the
    stored frames (None for an empty edit) and `duration` is how long it plays.
    """
    moov = read_moov_body(path)
    if moov is None:
        return []
    mvhd = find_atom(moov, 0, len(moov), b"mvhd")
    if mvhd is None:
        return []
    movie_timescale, _ = read_timescale_and_duration(moov, mvhd)
    tracks = []
    for kind, track_start, track_end in iter_atoms(moov, 0, len(moov)):
        if kind != b"trak":
            continue
        hdlr = find_atom(moov, track_start, track_end, b"mdia", b"hdlr")
        if hdlr is None or moov[hdlr[0] + 8:hdlr[0] + 12] != b"vide":
            continue
        mdhd = find_atom(moov, track_start, track_end, b"mdia", b"mdhd")
        stsz = find_atom(moov, track_start, track_end, b"mdia", b"minf", b"stbl", b"stsz")
        if mdhd is None or stsz is None:
            continue
        media_timescale, media_duration = read_timescale_and_duration(moov, mdhd)
        sample_count = struct.unpack_from(">I", moov, stsz[0] + 8)[0]
        edits = []
        elst = find_atom(moov, track_start, track_end, b"edts", b"elst")
        if elst is not None:
            version = moov[elst[0]]
            entry_count = struct.unpack_from(">I", moov, elst[0] + 4)[0]
            entry_format, entry_size = (">QqI", 20) if version == 1 else (">IiI", 12)
            for index in range(entry_count):
                duration, media_time, rate = struct.unpack_from(entry_format, moov, elst[0] + 8 + index * entry_size)
                edits.append({
                    "duration": duration / movie_timescale,
                    "media_start": None if media_time == -1 else media_time / media_timescale,
                    "rate": rate / 65536,
                })
        tracks.append({
            "media_duration": media_duration / media_timescale,
            "sample_count": sample_count,
            "edits": edits,
        })
    return tracks


def edit_list_hides_nothing(track: dict) -> bool:
    """True when a track's edit list plays every stored frame once, in order."""
    edits = track["edits"]
    if not edits:
        return True
    if len(edits) != 1 or edits[0]["media_start"] != 0 or edits[0]["rate"] != 1:
        return False
    frame_duration = track["media_duration"] / max(track["sample_count"], 1)
    return abs(edits[0]["duration"] - track["media_duration"]) <= frame_duration


def analyze_data_streams(probed: dict):
    """Decide what happens to the source's data streams.

    MKV has no timecode tracks, and ffmpeg drops data streams with -dn. A
    timecode track that holds one entry starting at zero is dropped, along
    with the copy of it ffmpeg keeps as a video stream tag. A single timecode
    entry that starts elsewhere or names a reel is kept as video stream tags.
    Anything else would be lost, so it stops the transcode for review.

    Returns (problems, notes, transcode_options).
    """
    problems, notes, options = [], [], []
    timecode_streams = []
    every_timecode_empty = True
    for stream in streams_of_type(probed, "data"):
        if stream.get("codec_tag_string") == "tmcd":
            timecode_streams.append(stream)
        else:
            problems.append(
                f"Data stream {stream['index']} ({stream.get('codec_tag_string') or stream.get('codec_name')}) "
                "would be dropped; review whether it holds anything worth keeping."
            )
    for stream in timecode_streams:
        tags = stream.get("tags", {})
        start = tags.get("timecode", "")
        reel = tags.get("reel_name", "")
        entries = stream.get("nb_frames")
        count = "1 entry" if entries == "1" else f"{entries or 'an unknown number of'} entries"
        described = f"Timecode stream {stream['index']}: {count}, starts at {start or 'unknown'}, reel name {reel or 'none'}."
        if entries == "1" and ZERO_TIMECODE.match(start) and not reel:
            notes.append(f"{described} Dropped: every frame's timecode is just its position in the file.")
            continue
        every_timecode_empty = False
        if entries != "1":
            problems.append(f"{described} MKV can record only a starting timecode; review before transcoding.")
        elif len(timecode_streams) > 1:
            problems.append(f"{described} The source has {len(timecode_streams)} timecode streams; review before transcoding.")
        elif reel:
            notes.append(f"{described} Kept as TIMECODE and REEL_NAME tags on the MKV video stream.")
            options += ["-metadata:s:v", f"reel_name={reel}"]
        else:
            notes.append(f"{described} Kept as a TIMECODE tag on the MKV video stream.")
    if timecode_streams and every_timecode_empty:
        options += ["-metadata:s:v", "timecode="]
    return problems, notes, options


def analyze_audio_streams(probed: dict) -> List[str]:
    """Return reasons any audio stream can't be stored losslessly as FLAC."""
    problems = []
    for stream in streams_of_type(probed, "audio"):
        codec = stream.get("codec_name", "")
        channels = int(stream.get("channels") or 0)
        if FLAC_UNSUPPORTED_AUDIO_CODECS.match(codec):
            problems.append(f"Audio stream {stream['index']} is {codec}, which FLAC can't store losslessly.")
        if channels > FLAC_MAX_CHANNELS:
            problems.append(f"Audio stream {stream['index']} has {channels} channels; FLAC holds at most {FLAC_MAX_CHANNELS}.")
    return problems


def compare_video_properties(source: dict, output: dict):
    """Compare significant video properties of the source and the MKV.

    Returns (rows, changed, labeled): table rows, properties whose specified
    value changed, and properties the source leaves unspecified but ffmpeg
    labels in the MKV.
    """
    rows, changed, labeled = [], [], []
    for source_stream, output_stream in zip(streams_of_type(source, "video"), streams_of_type(output, "video")):
        for name in SIGNIFICANT_VIDEO_PROPERTIES:
            before = source_stream.get(name)
            after = output_stream.get(name)
            if before == after:
                result = "same"
            elif before in UNSPECIFIED_PROPERTY_VALUES:
                result = "unspecified in source; labeled by ffmpeg"
                labeled.append(name)
            else:
                result = "CHANGED"
                changed.append(name)
            rows.append(
                f"| {source_stream['index']} | {name} | {before or 'unspecified'} | {after or 'unspecified'} | {result} |"
            )
    return rows, changed, labeled


def parse_streamhash_lines(md5_output):
    parsed = {"v": [], "a": []}
    for line in md5_output.splitlines():
        stripped = line.strip()
        if "MD5=" not in stripped:
            continue

        tokens = [token.strip() for token in stripped.split(",")]
        stream_type = None
        if len(tokens) > 1 and tokens[1] in {"v", "a"}:
            stream_type = tokens[1]
        elif tokens and (tokens[0] == "v" or tokens[0].startswith("v:")):
            stream_type = "v"
        elif tokens and (tokens[0] == "a" or tokens[0].startswith("a:")):
            stream_type = "a"

        if stream_type is None:
            continue

        hash_value = stripped.split("MD5=", 1)[1].strip()
        if hash_value:
            parsed[stream_type].append(hash_value)

    return parsed


def streamhash_command(path: Path, input_options: List[str]) -> List[str]:
    return [
        FFMPEG_CMD, "-hide_banner", *input_options, "-i", path.as_posix(),
        "-map", "0:v", "-map", "0:a?", "-f", "streamhash", "-hash", "md5", "-",
    ]


def display_command(command: List[str], names: dict) -> str:
    """Render a command for the log, with file paths shortened to file names."""
    return shlex.join(names.get(argument, argument) for argument in command)


def replace_last_stem_segment(stem: str, replacement: str) -> str:
    parts = stem.split("_")
    if len(parts) > 1:
        parts[-1] = replacement
        return "_".join(parts)
    return f"{stem}_{replacement}"


def build_output_paths(source_video_path: Path):
    ffv1_stem = replace_last_stem_segment(source_video_path.stem, "FFV1")
    transcode_log_stem = replace_last_stem_segment(source_video_path.stem, "TRANSCODE")
    output_mkv_path = source_video_path.with_name(f"{ffv1_stem}.mkv")
    output_mkv_md5_path = Path(f"{output_mkv_path.as_posix()}.md5")
    transcode_log_path = source_video_path.with_name(f"{transcode_log_stem}.md")
    return output_mkv_path, output_mkv_md5_path, transcode_log_path


def build_destination_paths(source_video_path: Path, source_root: Path, destination_root: Path):
    relative_source_path = source_video_path.relative_to(source_root)
    source_destination_path = destination_root.joinpath(relative_source_path)
    ffv1_stem = replace_last_stem_segment(source_video_path.stem, "FFV1")
    transcode_log_stem = replace_last_stem_segment(source_video_path.stem, "TRANSCODE")
    output_mkv_path = source_destination_path.with_name(f"{ffv1_stem}.mkv")
    output_mkv_md5_path = Path(f"{output_mkv_path.as_posix()}.md5")
    transcode_log_path = source_destination_path.with_name(f"{transcode_log_stem}.md")
    return output_mkv_path, output_mkv_md5_path, transcode_log_path


def copy_item_siblings(source_item_dir: Path, transcoded_video_paths: List[Path], source_root: Path, destination_root: Path):
    """Copy everything in an item directory except the transcoded sources and their MD5 files."""
    relative_item_dir = source_item_dir.relative_to(source_root)
    destination_item_dir = destination_root.joinpath(relative_item_dir)
    destination_item_dir.mkdir(parents=True, exist_ok=True)

    skip = set()
    for transcoded_video_path in transcoded_video_paths:
        skip.add(transcoded_video_path.resolve())
        skip.add(Path(f"{transcoded_video_path.as_posix()}.md5").resolve())

    for source_path in source_item_dir.iterdir():
        if source_path.resolve() in skip:
            continue
        destination_path = destination_item_dir.joinpath(source_path.name)
        if destination_path.exists():
            continue
        if source_path.is_dir():
            shutil.copytree(source_path, destination_path)
        else:
            shutil.copy2(source_path, destination_path)


def copy_siblings_of_transcoded(transcoded: List[Path], source_root: Path, destination_root: Path):
    """Copy item files once every source in the batch has been processed.

    Waiting until the end keeps a second source in the same item directory
    (another reel, say) from being copied before it is transcoded.
    """
    by_item_dir = {}
    for video_path in transcoded:
        by_item_dir.setdefault(video_path.parent, []).append(video_path)
    print("⏳ COPYING ITEM FILES TO DESTINATION")
    for item_dir, video_paths in by_item_dir.items():
        copy_item_siblings(item_dir, video_paths, source_root, destination_root)
    print("✅ COPIED ITEM FILES")


@auto_cleanup_processes
def main(p: Path, source_root: Path, destination_root: Path) -> Optional[str]:
    """Transcode p to FFV1/MKV and verify it.

    Returns None once p is transcoded, or the reason p was skipped. Raises
    SystemExit when p fails or needs review.
    """
    output_mkv_path, output_mkv_md5_path, transcode_log_path = build_destination_paths(p, source_root, destination_root)
    source_md5_path = Path(f"{p.as_posix()}.md5")
    error_log_path = transcode_log_path.with_name(f"{transcode_log_path.stem}--ERROR.md")

    print(f"\n📂 {p.parent.name}")
    source_probe = probe(p)
    video_streams = streams_of_type(source_probe, "video")
    audio_streams = streams_of_type(source_probe, "audio")
    subtitle_streams = streams_of_type(source_probe, "subtitle")
    data_streams = streams_of_type(source_probe, "data")
    print(f"🎥 {p.name} contains {len(video_streams)} video streams")
    print(f"🎧 {p.name} contains {len(audio_streams)} audio streams")
    print(f"🔇 {p.name} contains {len(subtitle_streams)} subtitle streams")
    print(f"🧾 {p.name} contains {len(data_streams)} data streams")

    # only uncompressed or losslessly compressed video is worth converting
    if not video_streams:
        print("⏭️  SKIPPED: no video stream")
        return "no video stream"
    other_codecs = sorted({s.get("codec_name", "unknown") for s in video_streams} - TRANSCODABLE_VIDEO_CODECS)
    if other_codecs:
        reason = f"video is {', '.join(other_codecs)}; only uncompressed or lossless video is transcoded"
        print(f"⏭️  SKIPPED: {reason}")
        return reason

    # read every stored frame: an edit list can hide frames from ffmpeg,
    # and its rounding at segment joins drops real ones
    source_input_options = ["-ignore_editlist", "1"] if is_mov_family(source_probe) else []
    edit_list_tracks = read_mov_video_edit_lists(p) if source_input_options else []
    edited_tracks = [(number, track) for number, track in enumerate(edit_list_tracks) if not edit_list_hides_nothing(track)]

    problems, timecode_notes, timecode_options = analyze_data_streams(source_probe)
    problems += analyze_audio_streams(source_probe)
    if problems:
        print("❌ NEEDS REVIEW")
        for problem in problems:
            print(f"   {problem}")
        output_mkv_path.parent.mkdir(parents=True, exist_ok=True)
        with open(error_log_path, "w") as f:
            f.write("# ❌ NEEDS REVIEW BEFORE TRANSCODING\n\n")
            f.writelines(f"- {problem}\n" for problem in problems)
        raise SystemExit("Needs review: " + " ".join(problems))

    output_mkv_path.parent.mkdir(parents=True, exist_ok=True)
    with open(transcode_log_path, "w") as f:
        f.write(
            "# TRANSCODING LOG\n\nThese were the steps used to convert the source file to a lossless FFV1/MKV file.\n\n"
        )
        f.write(f"Transcoded with Athanor {athanor_version()}.\n\n")
        f.write("## Source analysis\n\n")
        f.write("Streams in the source file:\n\n")
        for stream in source_probe.get("streams", []):
            f.write(
                f"- {stream['index']}: {stream.get('codec_type')} "
                f"{stream.get('codec_name') or stream.get('codec_tag_string')}"
                + (f", {stream['nb_frames']} stored frames" if stream.get("codec_type") == "video" and stream.get("nb_frames") else "")
                + "\n"
            )
        f.write("\n")
        if edited_tracks:
            f.write(
                "The source has an edit list, which tells players to show only some of the stored frames, "
                "or to show them in segments. The FFV1 file keeps every stored frame and does not apply "
                "the edit list. These are its entries, in seconds, to reproduce what it shows:\n\n"
            )
            for number, track in edited_tracks:
                f.write(f"Video track {number}: {track['sample_count']} stored frames, {track['media_duration']:.6f} seconds.\n\n")
                f.write("| Edit | Starts at (stored time) | Plays for | Rate |\n|---|---|---|---|\n")
                for index, edit in enumerate(track["edits"], start=1):
                    start = "empty" if edit["media_start"] is None else f"{edit['media_start']:.6f}"
                    f.write(f"| {index} | {start} | {edit['duration']:.6f} | {edit['rate']:g} |\n")
                f.write("\n")
        for note in timecode_notes:
            f.write(f"{note}\n\n")

    """Start long-running processes at the same time in the background."""
    print("\n")
    hashing = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    calculating_md5_source_file = None
    if source_md5_path.exists():
        # calculate MD5 of source file if a comparison file exists
        print("⏳ calculating source file MD5 in the background")
        calculating_md5_source_file = hashing.submit(md5_of_file, p)
    # determine if first subtitle stream has content
    if len(subtitle_streams) > 1:
        print("🔇 multiple subtitle streams detected; skipping subtitle transcoding")
        # set up option to skip transcoding subtitle streams
        skip_subtitle_streams = True
    elif len(subtitle_streams) > 0:
        print("⏳ checking for subtitle streams with content in the background")
        captured_subtitle_stream = subprocess.run(
            [
                FFMPEG_CMD,
                "-f",
                "lavfi",
                "-i",
                f"movie={p.as_posix()}[out+subcc]",
                "-map",
                "0:s:0",
                "-c:s",
                "srt",
                "-f",
                "srt",
                "-",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if captured_subtitle_stream.stdout == "":
            print("🔇 subtitle stream has no content")
            # set up option to skip transcoding subtitle streams
            skip_subtitle_streams = True
        else:
            print("🔇 subtitle stream has content")
            skip_subtitle_streams = False
    else:
        # set up option to skip transcoding subtitle streams
        skip_subtitle_streams = True
    # calculate MD5 of source audio/video streams
    print("⏳ calculating source streamhash as MD5 in the background")
    source_streamhash_cmd = streamhash_command(p, source_input_options)
    calculating_md5_source_streams = subprocess.Popen(
        source_streamhash_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    # transcode source to FFV1 MKV
    print(f"⏳ transcoding source to {output_mkv_path.name} in the background")
    transcode_cmd = [
        FFMPEG_CMD,
        "-hide_banner",
        "-nostats",
        *source_input_options,
        "-i",
        p.as_posix(),
        "-map",
        "0",
        "-c:v",
        "ffv1",
        "-level",
        "3",
        "-g",
        "1",
        "-slicecrc",
        "1",
        "-slices",
        "4",
        "-c:a",
        "flac",
        "-compression_level",
        "12",
        "-dn",
        *timecode_options,
    ]
    if skip_subtitle_streams:
        transcode_cmd.append("-sn")
    transcode_cmd.append(output_mkv_path.as_posix())
    transcode = subprocess.Popen(
        transcode_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    log_names = {p.as_posix(): p.name, output_mkv_path.as_posix(): output_mkv_path.name}
    if source_md5_path.exists():
        with open(source_md5_path) as f:
            saved_md5_source_file = f.read().split()[0].lower()
        # wait for source file MD5 calculation to complete
        if calculating_md5_source_file is None:
            raise SystemExit("Source file MD5 process did not start")
        spinner = Spinner()
        spinner.start("🤼 WAITING FOR MD5 COMPARISON TO COMPLETE")
        calculated_md5_source_file = calculating_md5_source_file.result()
        spinner.stop()
        # compare calculated MD5 of source file with saved MD5 checksum file
        print(f"{p.name}:        {calculated_md5_source_file}")
        print(f"{p.name}.md5:    {saved_md5_source_file}")
        if calculated_md5_source_file != saved_md5_source_file:
            print("❌ MD5 FILE MISMATCH")
            transcode.terminate()
            transcode.wait()
            calculating_md5_source_streams.terminate()
            calculating_md5_source_streams.wait()
            raise SystemExit("MD5 file mismatch")
        else:
            print("✅ MD5 FILE MATCH")
        with open(transcode_log_path, "a") as f:
            f.write(
                "Calculated the MD5 checksum of the source file and compared it with the saved MD5 checksum.\n\n"
            )
            f.write("Calculated MD5:\n")
            f.write(f"```\n{calculated_md5_source_file}  {p.name}\n```\n\n")
            f.write("Saved MD5:\n")
            f.write(f"```\n$ cat {p.name}.md5\n{saved_md5_source_file}  {p.name}.md5\n```\n\n")
    with open(transcode_log_path, "a") as f:
        f.write("FFmpeg version used to transcode the file.\n")
    print_ffmpeg_version = subprocess.run(
        [
            FFMPEG_CMD,
            "-version",
        ],
        capture_output=True,
        text=True,
    ).stdout.strip()
    with open(transcode_log_path, "a") as f:
        f.write(f"```\n$ {FFMPEG_CMD} -version\n{print_ffmpeg_version}\n```\n\n")
    # wait for transcode to complete; ffmpeg writes its message output to stderr
    spinner = Spinner()
    spinner.start("⏳ WAITING FOR TRANSCODING TO COMPLETE")
    ffmpeg_output = transcode.communicate()[1]
    spinner.stop()
    if transcode.returncode != 0:
        print("\n❌ FFMPEG TRANSCODE FAILED")
        print(ffmpeg_output)
        calculating_md5_source_streams.terminate()
        calculating_md5_source_streams.wait()
        with open(error_log_path, "w") as f:
            f.write(f"# ❌ FFMPEG TRANSCODE FAILED\n\n```\n{ffmpeg_output}\n```\n")
        raise SystemExit("FFmpeg transcode failed")
    with open(transcode_log_path, "a") as f:
        f.write("FFmpeg output.\n")
        f.write(f"```\n$ {display_command(transcode_cmd, log_names)}\n{ffmpeg_output}\n```\n\n")
    # calculate MD5 of transcoded MKV file
    print(f"\n⏳ calculating {output_mkv_path.name} file MD5 in the background")
    calculating_md5_mkv_file = hashing.submit(md5_of_file, output_mkv_path)
    # calculate MD5 hashes of transcoded MKV audio/video streams
    mkv_streamhash_cmd = streamhash_command(output_mkv_path, [])
    calculated_md5_mkv_streams = subprocess.run(
        mkv_streamhash_cmd,
        capture_output=True,
        text=True,
    )
    if calculated_md5_mkv_streams.returncode != 0:
        print("❌ MKV STREAMHASH FAILED")
        print(calculated_md5_mkv_streams.stderr)
        raise SystemExit("MKV streamhash failed")
    calculated_md5_mkv_streams = calculated_md5_mkv_streams.stdout.strip()
    # wait for MKV file MD5 calculation to complete
    spinner = Spinner()
    spinner.start("🤼 WAITING FOR MD5 COMPARISON TO COMPLETE")
    calculated_md5_mkv_file = calculating_md5_mkv_file.result()
    spinner.stop()
    hashing.shutdown()
    with open(transcode_log_path, "a") as f:
        f.write(
            "Calculated the MD5 checksum of the transcoded MKV file.\n\n"
        )
        f.write("Calculated MD5:\n")
        f.write(f"```\n{calculated_md5_mkv_file}  {output_mkv_path.name}\n```\n\n")
    with open(output_mkv_md5_path, "w") as f:
        f.write(calculated_md5_mkv_file)
    # wait for source streamhash MD5 calculation to complete
    spinner = Spinner()
    spinner.start("🤼 WAITING FOR MD5 COMPARISON TO COMPLETE")
    source_streamhash_stdout, source_streamhash_stderr = calculating_md5_source_streams.communicate()
    spinner.stop()
    if calculating_md5_source_streams.returncode != 0:
        print("❌ SOURCE STREAMHASH FAILED")
        print(source_streamhash_stderr)
        raise SystemExit("Source streamhash failed")
    calculated_md5_source_streams = source_streamhash_stdout.strip()
    # compare MD5 hashes of source audio/video streams with MD5 hashes of transcoded MKV audio/video streams
    print(f"{p.name} (streams):\n{calculated_md5_source_streams}")
    print(f"{output_mkv_path.name} (streams):\n{calculated_md5_mkv_streams}")
    source_hashes = parse_streamhash_lines(calculated_md5_source_streams)
    mkv_hashes = parse_streamhash_lines(calculated_md5_mkv_streams)

    if source_hashes["v"] != mkv_hashes["v"]:
        print("❌ VIDEO STREAM MD5 MISMATCH")
        raise SystemExit("Video stream MD5 mismatch")
    print("✅ VIDEO STREAM MD5 MATCH")

    source_audio_codecs = [stream.get("codec_name", "").lower() for stream in audio_streams]
    non_aac_audio_positions = [i for i, codec in enumerate(source_audio_codecs) if codec != "aac"]

    if not source_hashes["a"] and not mkv_hashes["a"]:
        print("✅ NO AUDIO STREAM HASHES TO COMPARE")
    elif not non_aac_audio_positions:
        print("ℹ️ SKIPPING AUDIO STREAM MD5 CHECK (all source audio streams are AAC)")
    else:
        if len(source_hashes["a"]) <= max(non_aac_audio_positions) or len(mkv_hashes["a"]) <= max(non_aac_audio_positions):
            print("❌ AUDIO STREAM HASH EXTRACTION FAILED")
            print(f"Source audio codec count: {len(source_audio_codecs)}")
            print(f"Source non-AAC positions: {non_aac_audio_positions}")
            print(f"Source audio hash count: {len(source_hashes['a'])}")
            print(f"MKV audio hash count: {len(mkv_hashes['a'])}")
            print(f"Source codecs: {source_audio_codecs}")
            print(f"Source hashes (a): {source_hashes['a']}")
            print(f"MKV hashes (a): {mkv_hashes['a']}")
            raise SystemExit("Audio stream hash extraction failed: streamhash output did not include all expected audio streams")

        source_non_aac_hashes = [source_hashes["a"][i] for i in non_aac_audio_positions]
        mkv_non_aac_hashes = [mkv_hashes["a"][i] for i in non_aac_audio_positions]

        if source_non_aac_hashes != mkv_non_aac_hashes:
            print("❌ NON-AAC AUDIO STREAM MD5 MISMATCH")
            raise SystemExit("Non-AAC audio stream MD5 mismatch")
        print("✅ NON-AAC AUDIO STREAM MD5 MATCH")
    with open(transcode_log_path, "a") as f:
        f.write(
            "Compared the calculated MD5 stream hashes from the source file with those from the transcoded MKV file. Future stream hash calculations must use the same or a compatible version of FFmpeg, otherwise the output will differ.\n\n"
        )
        f.write("Source stream hashes:\n")
        f.write(f"```\n$ {display_command(source_streamhash_cmd, log_names)}\n{calculated_md5_source_streams}\n```\n\n")
        f.write("MKV stream hashes:\n")
        f.write(f"```\n$ {display_command(mkv_streamhash_cmd, log_names)}\n{calculated_md5_mkv_streams}\n```\n\n")

    # the stream hashes can't show a frame missing from both reads, so count
    # every stored frame in the source and every frame in the MKV
    print("⏳ COUNTING FRAMES")
    stored_frame_counts = [int(stream["nb_frames"]) for stream in video_streams if stream.get("nb_frames")]
    if len(stored_frame_counts) != len(video_streams):
        stored_frame_counts = count_video_frames(p, source_input_options)
    mkv_frame_counts = count_video_frames(output_mkv_path, [])
    print(f"{p.name} (stored frames): {stored_frame_counts}")
    print(f"{output_mkv_path.name} (frames): {mkv_frame_counts}")
    with open(transcode_log_path, "a") as f:
        f.write("Compared the number of frames stored in each video stream of the source with the number in the MKV.\n\n")
        f.write(f"```\nsource stored frames: {stored_frame_counts}\nMKV frames:           {mkv_frame_counts}\n```\n\n")
    if stored_frame_counts != mkv_frame_counts:
        print("❌ FRAME COUNT MISMATCH")
        raise SystemExit("Frame count mismatch")
    print("✅ FRAME COUNT MATCH")

    rows, changed, labeled = compare_video_properties(source_probe, probe(output_mkv_path))
    with open(transcode_log_path, "a") as f:
        f.write("Compared the significant properties of each video stream.\n\n")
        f.write("| Stream | Property | Source | MKV | Result |\n|---|---|---|---|---|\n")
        f.write("\n".join(rows) + "\n\n")
        if labeled:
            f.write(
                "Where the source leaves a property unspecified, ffmpeg writes its own value into the MKV. "
                "These labels come from ffmpeg, not the source; the stream hashes show the pictures are unchanged.\n\n"
            )
    if changed:
        print(f"❌ VIDEO PROPERTIES CHANGED: {', '.join(changed)}")
        raise SystemExit(f"Video properties changed: {', '.join(changed)}")
    if labeled:
        print(f"ℹ️ UNSPECIFIED IN SOURCE, LABELED BY FFMPEG: {', '.join(sorted(set(labeled)))}")
    print("✅ VIDEO PROPERTIES MATCH")

    print("\n✅ DONE\n")
    return None

def is_video_file(path, video_exts=VIDEO_EXTS):
    return path.suffix.lower() in video_exts

def is_source_video_md5(path, video_exts=VIDEO_EXTS):
    # e.g., IMG_1234.mov.md5
    if path.suffix.lower() != ".md5":
        return False
    stem = Path(path.stem)  # e.g., IMG_1234.mov
    ext = stem.suffix.lower()
    return ext in video_exts

def cli():
    """Run the athanor command."""
    global FFMPEG_CMD, FFPROBE_CMD
    atexit.register(restore_tty)
    # print each line as it happens, even when output goes to a log
    sys.stdout.reconfigure(line_buffering=True)
    parser = argparse.ArgumentParser(
        prog="athanor", description="Convert digitized video to lossless FFV1/MKV and verify the result."
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {athanor_version()}")
    parser.add_argument(
        "--level", choices=["parent", "object"], help="'parent' if src contains many items, 'object' if src is a direct path to one item", required=True
    )
    parser.add_argument(
        "--src", help="path to the source parent (directory) or object (file)", required=True
    )
    parser.add_argument(
        "--dst", help="path to the destination directory", required=True
    )
    parser.add_argument('--ffmpeg', default=FFMPEG_CMD, help='(optional) path to ffmpeg binary')
    parser.add_argument('--ffprobe', default=FFPROBE_CMD, help='(optional) path to ffprobe binary')
    parser.add_argument('--exclude', help='file extension to exclude from processing (e.g. mp4)', default=None)
    args = parser.parse_args(args=None if sys.argv[1:] else ["--help"])
    src_path = Path(args.src)
    dst_path = Path(args.dst)
    ffmpeg_path = Path(args.ffmpeg)
    ffprobe_path = Path(args.ffprobe)
    # VALIDATE ARGUMENTS
    if args.level == "parent" and (not src_path.exists() or not src_path.is_dir()):
        print("❌ INVALID SOURCE PATH")
        sys.exit(1)
    if args.level == "object" and (not src_path.exists() or not src_path.is_file()):
        print("❌ INVALID SOURCE PATH")
        sys.exit(1)
    if not dst_path.exists() or not dst_path.is_dir():
        print("❌ INVALID DESTINATION PATH")
        sys.exit(1)
    if not ffmpeg_path.exists() or not ffmpeg_path.is_file():
        print("❌ INVALID FFMPEG PATH")
        sys.exit(1)
    if not ffprobe_path.exists() or not ffprobe_path.is_file():
        print("❌ INVALID FFPROBE PATH")
        sys.exit(1)
    # SET GLOBAL VARIABLES
    FFMPEG_CMD = args.ffmpeg
    FFPROBE_CMD = args.ffprobe

    # Process source media in place and write derivatives to a timestamped batch directory.
    batches_directory = dst_path.joinpath(
        "BATCHES",
        datetime.datetime.now().isoformat(sep="-", timespec="seconds").replace(":", "")
    )
    batches_directory.mkdir(parents=True)

    if args.level == "parent":
        destination_root = batches_directory
        source_video_root = src_path
    elif args.level == "object":
        destination_root = batches_directory.joinpath(src_path.stem)
        destination_root.mkdir(parents=True)
        source_video_root = src_path.parent
    else:
        print("❌ PROBLEM PREPARING DESTINATION")
        sys.exit(1)

    # Find all video files (common extensions)
    video_exts = VIDEO_EXTS.copy()
    if args.exclude:
        exclude_ext = args.exclude.lower().strip(".")
        video_exts = [ext for ext in video_exts if ext.lstrip(".") != exclude_ext]
    if args.level == "parent":
        video_paths = [p for ext in video_exts for p in source_video_root.glob(f"**/*{ext}")]
    else:
        video_paths = [src_path] if src_path.suffix.lower() in video_exts else []
    failed_files = []
    skipped_files = []
    transcoded_files = []
    if args.level == "parent":
        for p in sorted(video_paths, key=lambda x: x.stat().st_size):
            try:
                skip_reason = main(p, source_video_root, destination_root)
                if skip_reason is None:
                    transcoded_files.append(p)
                else:
                    skipped_files.append((p.name, skip_reason))
            except SystemExit as e:
                failed_files.append((p.name, str(e)))
            except Exception as e:
                failed_files.append((p.name, f"Exception: {e}"))
        if transcoded_files:
            copy_siblings_of_transcoded(transcoded_files, source_video_root, destination_root)
        if skipped_files:
            print("\nSummary of skipped files:")
            for fname, reason in skipped_files:
                print(f"  {fname}: {reason}")
        if failed_files:
            print("\nSummary of failed files:")
            for fname, reason in failed_files:
                print(f"  {fname}: {reason}")
            sys.exit(1)
    elif args.level == "object":
        if not video_paths:
            print("❌ NO VIDEO FILES FOUND TO PROCESS")
            sys.exit(1)
        skip_reason = main(video_paths[0], source_video_root, destination_root)
        if skip_reason is not None:
            print(f"❌ NOT TRANSCODED: {skip_reason}")
            sys.exit(1)
        copy_siblings_of_transcoded(video_paths, source_video_root, destination_root)
    else:
        print("❌ UNEXPECTED ERROR")
        sys.exit(1)


if __name__ == "__main__":
    cli()
