# DataStagingArea-MediaPreserve-FFV1.py
# Version 0.4.0

import argparse
import os
import shutil
import subprocess

parser = argparse.ArgumentParser()
parser.add_argument(
    "source", help="path to the directory containing the Archival Object directories"
)
parser.add_argument(
    "destination",
    help="path to the directory into which the Archival Object directories will be moved",
)
args = parser.parse_args()

FFMPEG_CMD = "/home/linuxbrew/.linuxbrew/bin/ffmpeg"

for entry in os.scandir(args.source):
    if not entry.is_dir():
        continue
    print(f"🙃 {entry.name}")
    for item in os.scandir(entry.path):
        if item.name != f"{entry.name}_prsv.mov":
            continue
        with open(f"{item.path.replace('.mov', '.mkv.md')}", "w") as f:
            f.write(
                "# TRANSCODING LOG\n\nThese were the steps used to convert the original v210/MOV file to a lossless FFV1/MKV file.\n\n"
            )
        """Start 3 of 4 long-running processes at the same time in the background."""
        # calculate MD5 of *_prsv.mov file
        print(f"⏳ calculating *_prsv.mov file MD5 in the background")
        calculating_md5_mov_file = subprocess.Popen(
            ["md5sum", item.path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        # calculate MD5 of *_prsv.mov audio/video streams
        print(f"⏳ calculating *_prsv.mov streamhash as MD5 in the background")
        calculating_md5_mov_streams = subprocess.Popen(
            [FFMPEG_CMD, "-i", item.path, "-f", "streamhash", "-hash", "md5", "-"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        # transcode *_prsv.mov to *_prsv.mkv
        print(f"⏳ transcoding *_prsv.mov to *_prsv.mkv in the background")
        transcode = subprocess.Popen(
            [
                FFMPEG_CMD,
                "-hide_banner",
                "-nostats",
                "-i",
                item.path,
                "-map",
                "0",
                "-dn",
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
                f"{item.path.replace('.mov', '.mkv')}",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        with open(f"{item.path}.md5") as f:
            saved_md5_mov_file = f.read().split()[0].lower()
        # wait for *_prsv.mov file MD5 calculation to complete
        calculated_md5_mov_file = calculating_md5_mov_file.communicate()[0].split()[0]
        # compare calculated MD5 of *_prsv.mov file with saved MD5 checksum file
        if calculated_md5_mov_file != saved_md5_mov_file:
            print("\n❌ MD5 FILE MISMATCH")
            print(f"{item.name}:        {calculated_md5_mov_file}")
            print(f"{item.name}.md5:    {saved_md5_mov_file}")
            transcode.terminate()
            calculating_md5_mov_streams.terminate()
            exit(1)
        else:
            print("\n✅ MD5 FILE MATCH")
            print(f"{item.name}:        {calculated_md5_mov_file}")
            print(f"{item.name}.md5:    {saved_md5_mov_file}")
        with open(f"{item.path.replace('.mov', '.mkv.md')}", "a") as f:
            f.write(
                "Calculated the MD5 checksum of the original MOV file and compared it with the saved MD5 checksum.\n\n"
            )
            f.write("Calculated MD5:\n")
            f.write(
                f"```\n$ md5sum {item.name}\n{calculated_md5_mov_file}  {item.name}\n```\n\n"
            )
            f.write("Saved MD5:\n")
            f.write(
                f"```\n$ cat {item.name}.md5\n{saved_md5_mov_file}  {item.name}.md5\n```\n\n"
            )
            f.write("FFmpeg version used to transcode the file.\n")
        print_ffmpeg_version = subprocess.run(
            [
                FFMPEG_CMD,
                "-version",
            ],
            capture_output=True,
            text=True,
        ).stdout.strip()
        with open(f"{item.path.replace('.mov', '.mkv.md')}", "a") as f:
            f.write(f"```\n$ {FFMPEG_CMD} -version\n{print_ffmpeg_version}\n```\n\n")
        # wait for transcode to complete; ffmpeg writes its message output to stderr
        ffmpeg_output = transcode.communicate()[1]
        if transcode.returncode != 0:
            print("\n❌ FFMPEG TRANSCODE FAILED")
            print(ffmpeg_output)
            calculating_md5_mov_streams.terminate()
            exit(1)
        with open(f"{item.path.replace('.mov', '.mkv.md')}", "a") as f:
            f.write("FFmpeg output.\n")
            f.write(
                f"```\n$ {FFMPEG_CMD} -hide_banner -nostats -i {item.name} -map 0 -dn -c:v ffv1 -level 3 -g 1 -slicecrc 1 -slices 4 -c:a copy {item.name.replace('.mov', '.mkv')}\n{ffmpeg_output}\n```\n\n"
            )
        # calculate MD5 hashes of *_prsv.mkv audio/video streams
        calculated_md5_mkv_streams = subprocess.run(
            [
                FFMPEG_CMD,
                "-i",
                f"{item.path.replace('.mov', '.mkv')}",
                "-f",
                "streamhash",
                "-hash",
                "md5",
                "-",
            ],
            capture_output=True,
            text=True,
        ).stdout.strip()
        # wait for *_prsv.mov streamhash MD5 calculation to complete
        calculated_md5_mov_streams = calculating_md5_mov_streams.communicate()[
            0
        ].strip()
        # compare MD5 hashes of *_prsv.mov audio/video streams with MD5 hashes of *_prsv.mkv audio/video streams
        if calculated_md5_mov_streams != calculated_md5_mkv_streams:
            print("\n❌ MD5 STREAM MISMATCH")
            print(f"{item.name}:\n{calculated_md5_mov_streams}")
            print(
                f"{item.name.replace('.mov', '.streamhashmd5')}:\n{calculated_md5_mkv_streams}"
            )
            exit(1)
        else:
            print("\n✅ MD5 STREAM MATCH")
            print(f"{item.name} (streams):\n{calculated_md5_mov_streams}")
            print(
                f"{item.name.replace('.mov', '.mkv')} (streams):\n{calculated_md5_mkv_streams}"
            )
            with open(f"{item.path.replace('.mov', '.streamhashmd5')}", "w") as f:
                f.write(calculated_md5_mkv_streams)
        with open(f"{item.path.replace('.mov', '.mkv.md')}", "a") as f:
            f.write(
                "Compared the calculated MD5 stream hashes from the original MOV file with those from the transcoded MKV file. Future stream hash calculations must use the same or a compatible version of FFmpeg, otherwise the output will differ.\n\n"
            )
            f.write("MOV stream hashes:\n")
            f.write(
                f"```\n$ {FFMPEG_CMD} -i {item.name} -f streamhash -hash md5 -\n{calculated_md5_mov_streams}\n```\n\n"
            )
            f.write("MKV stream hashes:\n")
            f.write(
                f"```\n$ {FFMPEG_CMD} -i {item.name.replace('.mov', '.mkv')} -f streamhash -hash md5 -\n{calculated_md5_mkv_streams}\n```\n\n"
            )
        # copy everything except *_prsv.mov and *_prsv.mov.md5 to destination
        print(f"\n⏳ moving files to destination")
        shutil.copytree(
            entry.path,
            os.path.join(args.destination, entry.name),
            ignore=shutil.ignore_patterns("*_prsv.mov*"),
        )
        os.remove(item.path.replace(".mov", ".mkv"))
        os.remove(item.path.replace(".mov", ".mkv.md"))
        os.remove(item.path.replace(".mov", ".streamhashmd5"))
        print("\n✅ DONE")
