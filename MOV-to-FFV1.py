# MOV-to-FFV1.py
# Version 1.2.0

import argparse
import datetime
import itertools
import shutil
import subprocess
import sys
import time
import threading

from pathlib import Path

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

        print("\r", end="")


def main(p: Path):
    print(f"\n📁 {p.parent.name}")
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
    with open(f"{p.parent}/{p.parent.name}_prsv.mkv.md", "w") as f:
        f.write(
            "# TRANSCODING LOG\n\nThese were the steps used to convert the source MOV file to a lossless FFV1/MKV file.\n\n"
        )
    """Start 3 of 4 long-running processes at the same time in the background."""
    # calculate MD5 of *.mov file
    print("\n")
    print(f"⏳ calculating source *.mov file MD5 in the background")
    calculating_md5_mov_file = subprocess.Popen(
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
    # calculate MD5 of *.mov audio/video streams
    print(f"⏳ calculating source *.mov streamhash as MD5 in the background")
    calculating_md5_mov_streams = subprocess.Popen(
        [FFMPEG_CMD, "-i", p.as_posix(), "-f", "streamhash", "-hash", "md5", "-"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    # transcode *.mov to *_prsv.mkv
    print(f"⏳ transcoding source *.mov to *_prsv.mkv in the background")
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
        "copy",
        "-dn",  # Only audio, video, and subtitles are supported for Matroska.
    ]
    if skip_subtitle_streams:
        transcode_cmd.append("-sn")
    transcode_cmd.append(f"{p.parent}/{p.parent.name}_prsv.mkv")
    transcode = subprocess.Popen(
        transcode_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    with open(f"{p.as_posix()}.md5") as f:
        saved_md5_mov_file = f.read().split()[0].lower()
    # wait for *.mov file MD5 calculation to complete
    print("\n")
    spinner = Spinner()
    spinner.start("🤼 WAITING FOR MD5 COMPARISON TO COMPLETE")
    calculated_md5_mov_file = calculating_md5_mov_file.communicate()[0].split()[0]
    spinner.stop()
    # compare calculated MD5 of *.mov file with saved MD5 checksum file
    print(f"{p.name}:        {calculated_md5_mov_file}")
    print(f"{p.name}.md5:    {saved_md5_mov_file}")
    if calculated_md5_mov_file != saved_md5_mov_file:
        print("❌ MD5 FILE MISMATCH")
        transcode.terminate()
        calculating_md5_mov_streams.terminate()
        exit(1)
    else:
        print("✅ MD5 FILE MATCH")
    with open(f"{p.parent}/{p.parent.name}_prsv.mkv.md", "a") as f:
        f.write(
            "Calculated the MD5 checksum of the source MOV file and compared it with the saved MD5 checksum.\n\n"
        )
        f.write("Calculated MD5:\n")
        f.write(f"```\n$ md5sum {p.name}\n{calculated_md5_mov_file}  {p.name}\n```\n\n")
        f.write("Saved MD5:\n")
        f.write(f"```\n$ cat {p.name}.md5\n{saved_md5_mov_file}  {p.name}.md5\n```\n\n")
        f.write("FFmpeg version used to transcode the file.\n")
    print_ffmpeg_version = subprocess.run(
        [
            FFMPEG_CMD,
            "-version",
        ],
        capture_output=True,
        text=True,
    ).stdout.strip()
    with open(f"{p.parent}/{p.parent.name}_prsv.mkv.md", "a") as f:
        f.write(f"```\n$ {FFMPEG_CMD} -version\n{print_ffmpeg_version}\n```\n\n")
    # wait for transcode to complete; ffmpeg writes its message output to stderr
    print("\n")
    spinner = Spinner()
    spinner.start("⏳ WAITING FOR TRANSCODING TO COMPLETE")
    ffmpeg_output = transcode.communicate()[1]
    spinner.stop()
    if transcode.returncode != 0:
        print("\n❌ FFMPEG TRANSCODE FAILED")
        print(ffmpeg_output)
        calculating_md5_mov_streams.terminate()
        with open(Path(args.dst).joinpath(f"{p.parent.name}--ERROR.md"), "w") as f:
            f.write(f"# ❌ FFMPEG TRANSCODE FAILED\n\n```\n{ffmpeg_output}\n```\n")
        return
    with open(f"{p.parent}/{p.parent.name}_prsv.mkv.md", "a") as f:
        f.write("FFmpeg output.\n")
        f.write(
            f"```\n$ {FFMPEG_CMD} -hide_banner -nostats -i {p.name} -map 0 -dn -c:v ffv1 -level 3 -g 1 -slicecrc 1 -slices 4 -c:a copy {p.parent.name}_prsv.mkv\n{ffmpeg_output}\n```\n\n"
        )
    # calculate MD5 of *_prsv.mkv file
    print(f"\n⏳ calculating *_prsv.mkv file MD5 in the background")
    calculating_md5_mkv_file = subprocess.Popen(
        ["md5sum", f"{p.parent}/{p.parent.name}_prsv.mkv"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    # calculate MD5 hashes of *_prsv.mkv audio/video streams
    calculated_md5_mkv_streams = subprocess.run(
        [
            FFMPEG_CMD,
            "-i",
            f"{p.parent}/{p.parent.name}_prsv.mkv",
            "-f",
            "streamhash",
            "-hash",
            "md5",
            "-",
        ],
        capture_output=True,
        text=True,
    ).stdout.strip()
    # wait for *_prsv.mkv file MD5 calculation to complete
    print("\n")
    spinner = Spinner()
    spinner.start("🤼 WAITING FOR MD5 COMPARISON TO COMPLETE")
    calculated_md5_mkv_file = calculating_md5_mkv_file.communicate()[0].split()[0]
    spinner.stop()
    with open(f"{p.parent}/{p.parent.name}_prsv.mkv.md", "a") as f:
        f.write(
            "Calculated the MD5 checksum of the transcoded MKV file.\n\n"
        )
        f.write("Calculated MD5:\n")
        f.write(f"```\n$ md5sum {p.parent}/{p.parent.name}_prsv.mkv.md\n{calculated_md5_mkv_file}  {p.parent}/{p.parent.name}_prsv.mkv.md\n```\n\n")
    with open(f"{p.parent}/{p.parent.name}_prsv.mkv.md5", "w") as f:
        f.write(calculated_md5_mkv_file)
    # wait for *.mov streamhash MD5 calculation to complete
    print("\n")
    spinner = Spinner()
    spinner.start("🤼 WAITING FOR MD5 COMPARISON TO COMPLETE")
    calculated_md5_mov_streams = calculating_md5_mov_streams.communicate()[0].strip()
    spinner.stop()
    # compare MD5 hashes of *.mov audio/video streams with MD5 hashes of *_prsv.mkv audio/video streams
    print(f"{p.name} (streams):\n{calculated_md5_mov_streams}")
    print(f"{p.parent.name}_prsv.mkv (streams):\n{calculated_md5_mkv_streams}")
    if calculated_md5_mov_streams != calculated_md5_mkv_streams:
        print("❌ MD5 STREAM MISMATCH")
        exit(1)
    else:
        print("✅ MD5 STREAM MATCH")
    with open(f"{p.parent}/{p.parent.name}_prsv.mkv.md", "a") as f:
        f.write(
            "Compared the calculated MD5 stream hashes from the source MOV file with those from the transcoded MKV file. Future stream hash calculations must use the same or a compatible version of FFmpeg, otherwise the output will differ.\n\n"
        )
        f.write("MOV stream hashes:\n")
        f.write(
            f"```\n$ {FFMPEG_CMD} -i {p.name} -f streamhash -hash md5 -\n{calculated_md5_mov_streams}\n```\n\n"
        )
        f.write("MKV stream hashes:\n")
        f.write(
            f"```\n$ {FFMPEG_CMD} -i {p.parent.name}_prsv.mkv -f streamhash -hash md5 -\n{calculated_md5_mkv_streams}\n```\n\n"
        )
    # copy everything to destination with exceptions
    print("\n")
    spinner = Spinner()
    spinner.start("⏳ WAITING FOR FILE MOVE TO COMPLETE")
    # using copytree in case we cross file system boundaries
    shutil.copytree(
        p.parent.as_posix(),
        Path(args.dst).joinpath(p.parent.name).as_posix(),
        ignore=shutil.ignore_patterns("*.mov*", "*.mp4*"),
    )
    p.parent.joinpath(f"{p.parent.name}_prsv.mkv").unlink()
    p.parent.joinpath(f"{p.parent.name}_prsv.mkv.md").unlink()
    p.parent.joinpath(f"{p.parent.name}_prsv.mkv.md5").unlink()
    spinner.stop()
    print("\n✅ DONE\n")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Preservation Transcoder")
    parser.add_argument(
        "--level", choices=["parent", "object"], help="'parent' if src contains many items, 'object' if src is a direct path to one item", required=True
    )
    parser.add_argument(
        "--src", help="path to the source directory", required=True
    )
    parser.add_argument(
        "--dst", help="path to the destination directory", required=True
    )
    parser.add_argument('--ffmpeg', default='/home/linuxbrew/.linuxbrew/bin/ffmpeg', help='(optional) path to ffmpeg binary')
    parser.add_argument('--ffprobe', default='/home/linuxbrew/.linuxbrew/bin/ffprobe', help='(optional) path to ffprobe binary')
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
            if src_item.is_dir():
                shutil.move(src_item.as_posix(), BATCHES_DIRECTORY.as_posix())
    elif args.level == "object":
        shutil.move(args.src, BATCHES_DIRECTORY.as_posix())
    else:
        print("❌ PROBLEM MOVING SOURCE FILES")
        exit(1)
    # EXECUTE MAIN FUNCTION
    # ASSUMPTION: the relevant src files have MOV extensions
    mov_paths = list(BATCHES_DIRECTORY.glob("**/*.mov"))
    if args.level == "parent":
        for p in sorted(mov_paths, key=lambda x: x.stat().st_size):
            main(p)
    elif args.level == "object":
        main(mov_paths[0])
    else:
        print("❌ UNEXPECTED ERROR")
        exit(1)
