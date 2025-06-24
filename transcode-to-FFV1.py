# transcode-to-FFV1.py
# Version 1.5.1

import argparse
import atexit
import datetime
import itertools
import shutil
import signal
import subprocess
import sys
import time
import threading

from pathlib import Path

# Define video extensions globally for DRY usage
VIDEO_EXTS = [
    ".mov", ".mp4", ".mkv", ".avi", ".mxf", ".webm", ".flv", ".wmv", ".mpg", ".mpeg", ".3gp", ".ogg", ".ogv"
]

def restore_tty():
    subprocess.run(['stty','sane'])

atexit.register(restore_tty)

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
    # https://stackoverflow.com/a/57974583

    __default_spinner_symbols_list = ['.    ', '..   ', '...  ', '.... ', '.....', ' ....', '  ...', '   ..', '    .', '     ']

    # needed for Python < 3.9
    from typing import List

    def __init__(self, spinner_symbols_list: List[str] = None):
        spinner_symbols_list = spinner_symbols_list if spinner_symbols_list else Spinner.__default_spinner_symbols_list
        self.__screen_lock = threading.Event()
        self.__spinner = itertools.cycle(spinner_symbols_list)
        self.__stop_event = False
        self.__thread = None

    def get_spin(self):
        return self.__spinner

    def start(self, spinner_message: str):
        self.__stop_event = False
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
        if self.__screen_lock.is_set():
            self.__screen_lock.wait()
            self.__screen_lock.clear()
            print("\r", end="")

        print()


def has_aac_audio_stream(filepath, ffprobe_cmd):
    result = subprocess.run(
        [
            ffprobe_cmd,
            "-v", "error",
            "-select_streams", "a",
            "-show_entries", "stream=codec_name",
            "-of", "csv=p=0",
            filepath
        ],
        capture_output=True,
        text=True,
    )
    codecs = set(result.stdout.strip().split())
    return "aac" in codecs

@auto_cleanup_processes
def main(p: Path):
    print(f"\n📂 {p.parent.name}")
    video_stream_count = len(
        subprocess.run(
            [
                FFPROBE_CMD,
                "-v",
                "error",
                "-select_streams",
                "v",
                "-show_entries",
                "stream=index",
                "-of",
                "csv=p=0",
                p.as_posix(),
            ],
            capture_output=True,
            text=True,
        ).stdout.split()
    )
    print(f"🎥 {p.name} contains {video_stream_count} video streams")
    audio_stream_count = len(
        subprocess.run(
            [
                FFPROBE_CMD,
                "-v",
                "error",
                "-select_streams",
                "a",
                "-show_entries",
                "stream=index",
                "-of",
                "csv=p=0",
                p.as_posix(),
            ],
            capture_output=True,
            text=True,
        ).stdout.split()
    )
    print(f"🎧 {p.name} contains {audio_stream_count} audio streams")
    subtitle_stream_count = len(
        subprocess.run(
            [
                FFPROBE_CMD,
                "-v",
                "error",
                "-select_streams",
                "s",
                "-show_entries",
                "stream=index",
                "-of",
                "csv=p=0",
                p.as_posix(),
            ],
            capture_output=True,
            text=True,
        ).stdout.split()
    )
    print(f"🔇 {p.name} contains {subtitle_stream_count} subtitle streams")
    with open(f"{p.parent}/{p.stem}--FFV1.mkv.md", "w") as f:
        f.write(
            "# TRANSCODING LOG\n\nThese were the steps used to convert the source file to a lossless FFV1/MKV file.\n\n"
        )
    """Start long-running processes at the same time in the background."""
    print("\n")
    if Path(f"{p.as_posix()}.md5").exists():
        # calculate MD5 of source file if a comparison file exists
        print(f"⏳ calculating source file MD5 in the background")
        calculating_md5_source_file = subprocess.Popen(
            ["md5sum", p.as_posix()],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    # determine if first subtitle stream has content
    if subtitle_stream_count > 0:
        print(f"⏳ checking for subtitle streams with content in the background")
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
    elif subtitle_stream_count > 1:
        print("🔇 multiple subtitle streams detected WE ONLY CHECKED THE FIRST ONE")
        # set up option to skip transcoding subtitle streams
        skip_subtitle_streams = True
    else:
        # set up option to skip transcoding subtitle streams
        skip_subtitle_streams = True
    # calculate MD5 of source audio/video streams
    print(f"⏳ calculating source streamhash as MD5 in the background")
    calculating_md5_source_streams = subprocess.Popen(
        [FFMPEG_CMD, "-i", p.as_posix(), "-f", "streamhash", "-hash", "md5", "-"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    # transcode source to *--FFV1.mkv
    print(f"⏳ transcoding source to *--FFV1.mkv in the background")
    transcode_cmd = [
        FFMPEG_CMD,
        "-hide_banner",
        "-nostats",
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
    ]
    if skip_subtitle_streams:
        transcode_cmd.append("-sn")
    transcode_cmd.append(f"{p.parent}/{p.stem}--FFV1.mkv")
    transcode = subprocess.Popen(
        transcode_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if Path(f"{p.as_posix()}.md5").exists():
        with open(f"{p.as_posix()}.md5") as f:
            saved_md5_source_file = f.read().split()[0].lower()
        # wait for source file MD5 calculation to complete
        spinner = Spinner()
        spinner.start("🤼 WAITING FOR MD5 COMPARISON TO COMPLETE")
        calculated_md5_source_file = calculating_md5_source_file.communicate()[0].split()[0]
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
            exit(1)
        else:
            print("✅ MD5 FILE MATCH")
        with open(f"{p.parent}/{p.stem}--FFV1.mkv.md", "a") as f:
            f.write(
                "Calculated the MD5 checksum of the source file and compared it with the saved MD5 checksum.\n\n"
            )
            f.write("Calculated MD5:\n")
            f.write(f"```\n$ md5sum {p.name}\n{calculated_md5_source_file}  {p.name}\n```\n\n")
            f.write("Saved MD5:\n")
            f.write(f"```\n$ cat {p.name}.md5\n{saved_md5_source_file}  {p.name}.md5\n```\n\n")
    with open(f"{p.parent}/{p.stem}--FFV1.mkv.md", "a") as f:
        f.write("FFmpeg version used to transcode the file.\n")
    print_ffmpeg_version = subprocess.run(
        [
            FFMPEG_CMD,
            "-version",
        ],
        capture_output=True,
        text=True,
    ).stdout.strip()
    with open(f"{p.parent}/{p.stem}--FFV1.mkv.md", "a") as f:
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
        with open(Path(args.dst).joinpath(f"{p.stem}--ERROR.md"), "w") as f:
            f.write(f"# ❌ FFMPEG TRANSCODE FAILED\n\n```\n{ffmpeg_output}\n```\n")
        return
    with open(f"{p.parent}/{p.stem}--FFV1.mkv.md", "a") as f:
        f.write("FFmpeg output.\n")
        f.write(
            f"```\n$ {FFMPEG_CMD} -hide_banner -nostats -i {p.name} -map 0 -dn -c:v ffv1 -level 3 -g 1 -slicecrc 1 -slices 4 -c:a flac -compression_level 12 {p.stem}--FFV1.mkv\n{ffmpeg_output}\n```\n\n"
        )
    # calculate MD5 of *--FFV1.mkv file
    print(f"\n⏳ calculating *--FFV1.mkv file MD5 in the background")
    calculating_md5_mkv_file = subprocess.Popen(
        ["md5sum", f"{p.parent}/{p.stem}--FFV1.mkv"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    # calculate MD5 hashes of *--FFV1.mkv audio/video streams
    calculated_md5_mkv_streams = subprocess.run(
        [
            FFMPEG_CMD,
            "-i",
            f"{p.parent}/{p.stem}--FFV1.mkv",
            "-f",
            "streamhash",
            "-hash",
            "md5",
            "-",
        ],
        capture_output=True,
        text=True,
    ).stdout.strip()
    # wait for *--FFV1.mkv file MD5 calculation to complete
    spinner = Spinner()
    spinner.start("🤼 WAITING FOR MD5 COMPARISON TO COMPLETE")
    calculated_md5_mkv_file = calculating_md5_mkv_file.communicate()[0].split()[0]
    spinner.stop()
    with open(f"{p.parent}/{p.stem}--FFV1.mkv.md", "a") as f:
        f.write(
            "Calculated the MD5 checksum of the transcoded MKV file.\n\n"
        )
        f.write("Calculated MD5:\n")
        f.write(f"```\n$ md5sum {p.parent}/{p.stem}--FFV1.mkv.md\n{calculated_md5_mkv_file}  {p.parent}/{p.stem}--FFV1.mkv.md\n```\n\n")
    with open(f"{p.parent}/{p.stem}--FFV1.mkv.md5", "w") as f:
        f.write(calculated_md5_mkv_file)
    # wait for source streamhash MD5 calculation to complete
    spinner = Spinner()
    spinner.start("🤼 WAITING FOR MD5 COMPARISON TO COMPLETE")
    calculated_md5_source_streams = calculating_md5_source_streams.communicate()[0].strip()
    spinner.stop()
    # compare MD5 hashes of source audio/video streams with MD5 hashes of *--FFV1.mkv audio/video streams
    print(f"{p.name} (streams):\n{calculated_md5_source_streams}")
    print(f"{p.stem}--FFV1.mkv (streams):\n{calculated_md5_mkv_streams}")
    aac_audio = has_aac_audio_stream(p.as_posix(), FFPROBE_CMD)
    if aac_audio:
        # Only compare video stream hashes
        def filter_video_lines(md5_output):
            return "\n".join(
                line for line in md5_output.splitlines()
                if line.strip().startswith("v:")  # v: for video streams
            )
        source_video_hashes = filter_video_lines(calculated_md5_source_streams)
        mkv_video_hashes = filter_video_lines(calculated_md5_mkv_streams)
        if source_video_hashes != mkv_video_hashes:
            print("❌ VIDEO STREAM MD5 MISMATCH (audio skipped due to AAC)")
            exit(1)
        else:
            print("✅ VIDEO STREAM MD5 MATCH (audio skipped due to AAC)")
    else:
        if calculated_md5_source_streams != calculated_md5_mkv_streams:
            print("❌ MD5 STREAM MISMATCH")
            exit(1)
        else:
            print("✅ MD5 STREAM MATCH")
    with open(f"{p.parent}/{p.stem}--FFV1.mkv.md", "a") as f:
        f.write(
            "Compared the calculated MD5 stream hashes from the source file with those from the transcoded MKV file. Future stream hash calculations must use the same or a compatible version of FFmpeg, otherwise the output will differ.\n\n"
        )
        f.write("Source stream hashes:\n")
        f.write(
            f"```\n$ {FFMPEG_CMD} -i {p.name} -f streamhash -hash md5 -\n{calculated_md5_source_streams}\n```\n\n"
        )
        f.write("MKV stream hashes:\n")
        f.write(
            f"```\n$ {FFMPEG_CMD} -i {p.stem}--FFV1.mkv -f streamhash -hash md5 -\n{calculated_md5_mkv_streams}\n```\n\n"
        )
    # copy everything to destination with exceptions
    spinner = Spinner()
    spinner.start("⏳ WAITING FOR FILE MOVE TO COMPLETE")
    copy_outputs_and_nonvideos(
        p.parent,
        Path(args.dst).joinpath(p.stem),
        VIDEO_EXTS
    )
    p.parent.joinpath(f"{p.stem}--FFV1.mkv").unlink()
    p.parent.joinpath(f"{p.stem}--FFV1.mkv.md").unlink()
    p.parent.joinpath(f"{p.stem}--FFV1.mkv.md5").unlink()
    spinner.stop()
    print("\n✅ DONE\n")
    return

def is_video_file(path, video_exts=VIDEO_EXTS):
    return path.suffix.lower() in video_exts

def is_source_video_md5(path, video_exts=VIDEO_EXTS):
    # e.g., IMG_1234.mov.md5
    if path.suffix.lower() != ".md5":
        return False
    stem = Path(path.stem)  # e.g., IMG_1234.mov
    ext = stem.suffix.lower()
    return ext in video_exts

def copy_outputs_and_nonvideos(src_dir, dst_dir, video_exts=VIDEO_EXTS):
    src_dir = Path(src_dir)
    dst_dir = Path(dst_dir)
    for file in src_dir.rglob("*"):
        rel_path = file.relative_to(src_dir)
        dest_path = dst_dir / rel_path
        # Exclude source video files and their md5s
        if is_video_file(file, video_exts) or is_source_video_md5(file, video_exts):
            continue
        # Only copy files (not directories)
        if file.is_file():
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file, dest_path)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Preservation Transcoder")
    parser.add_argument(
        "--level", choices=["parent", "object"], help="'parent' if src contains many items, 'object' if src is a direct path to one item", required=True
    )
    parser.add_argument(
        "--src", help="path to the source parent (directory) or object (file)", required=True
    )
    parser.add_argument(
        "--dst", help="path to the destination directory", required=True
    )
    parser.add_argument('--ffmpeg', default='/home/linuxbrew/.linuxbrew/bin/ffmpeg', help='(optional) path to ffmpeg binary')
    parser.add_argument('--ffprobe', default='/home/linuxbrew/.linuxbrew/bin/ffprobe', help='(optional) path to ffprobe binary')
    parser.add_argument('--exclude', help='file extension to exclude from processing (e.g. mp4)', default=None)
    args = parser.parse_args(args=None if sys.argv[1:] else ["--help"])
    # VALIDATE ARGUMENTS
    if not Path(args.src).exists() and not Path(args.src).is_dir():
        print("❌ INVALID SOURCE PATH")
        exit(1)
    if not Path(args.dst).exists() and not Path(args.dst).is_dir():
        print("❌ INVALID DESTINATION PATH")
        exit(1)
    if not Path(args.ffmpeg).exists() and not Path(args.ffmpeg).is_file():
        print("❌ INVALID FFMPEG PATH")
        exit(1)
    if not Path(args.ffprobe).exists() and not Path(args.ffprobe).is_file():
        print("❌ INVALID FFPROBE PATH")
        exit(1)
    # SET GLOBAL VARIABLES
    FFMPEG_CMD = args.ffmpeg
    FFPROBE_CMD = args.ffprobe
    BATCHES_DIRECTORY = Path(args.dst).joinpath(
        ".BATCHES",
        datetime.datetime.now()
        .isoformat(sep="-", timespec="seconds")
        .replace(":", "")
    )
    BATCHES_DIRECTORY.mkdir(parents=True)
    # MOVE SOURCE FILES TO BATCHES DIRECTORY
    if args.level == "parent":
        for src_item in Path(args.src).iterdir():
            shutil.move(src_item.as_posix(), BATCHES_DIRECTORY.as_posix())
    elif args.level == "object":
        shutil.move(args.src, BATCHES_DIRECTORY.as_posix())
    else:
        print("❌ PROBLEM MOVING SOURCE FILES")
        exit(1)
    # Find all video files (common extensions)
    video_exts = VIDEO_EXTS.copy()
    if args.exclude:
        exclude_ext = args.exclude.lower().strip(".")
        video_exts = [ext for ext in video_exts if ext.lstrip(".") != exclude_ext]
    video_paths = [p for ext in video_exts for p in BATCHES_DIRECTORY.glob(f"**/*{ext}")]
    if args.level == "parent":
        for p in sorted(video_paths, key=lambda x: x.stat().st_size):
            main(p)
    elif args.level == "object":
        main(video_paths[0])
    else:
        print("❌ UNEXPECTED ERROR")
        exit(1)
