# DataStagingArea-MediaPreserve-FFV1.py
# Version 0.3.0

import argparse
import os
import shutil
import subprocess

parser = argparse.ArgumentParser()
parser.add_argument("source", help="path to the directory containing the Archival Object directories")
parser.add_argument("destination", help="path to the directory into which the Archival Object directories will be moved")
args = parser.parse_args()

for entry in os.scandir(args.source):
    if entry.is_dir():
        print(f"🙃 {entry.name}")
        for item in os.scandir(entry.path):
            if item.name != f"{entry.name}_prsv.mov":
                continue
            """Start 3 of 4 long-running processes at the same time in the background."""
            # calculate MD5 of *_prsv.mov file
            print(f"⏳ calculating *_prsv.mov file MD5 in the background")
            calculating_md5_mov_file = subprocess.Popen(["md5sum", item.path], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            # calculate MD5 of *_prsv.mov audio/video stream
            print(f"⏳ calculating *_prsv.mov stream MD5 in the background")
            calculating_md5_mov_stream = subprocess.Popen(["ffmpeg", "-i", item.path, "-f", "hash", "-hash", "md5", "-"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            # transcode *_prsv.mov to *_prsv.mkv
            print(f"⏳ transcoding *_prsv.mov to *_prsv.mkv in the background")
            transcode = subprocess.Popen(["ffmpeg", "-hide_banner", "-nostats", "-i", item.path, "-map", "0", "-dn", "-c:v", "ffv1", "-level", "3", "-g", "1", "-slicecrc", "1", "-slices", "4", "-c:a", "copy", f"{item.path.replace('.mov', '.mkv')}"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
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
                calculating_md5_mov_stream.terminate()
                exit(1)
            else:
                print("\n✅ MD5 FILE MATCH")
                print(f"{item.name}:        {calculated_md5_mov_file}")
                print(f"{item.name}.md5:    {saved_md5_mov_file}")
            # wait for transcode to complete; ffmpeg writes its message output to stderr
            ffmpeg_log = transcode.communicate()[1]
            if transcode.returncode == 0:
                with open(f"{item.path.replace('.mov', '.ffmpeg.log')}", "w") as f:
                    f.write(ffmpeg_log)
            else:
                print("\n❌ FFMPEG TRANSCODE FAILED")
                print(ffmpeg_log)
                calculating_md5_mov_stream.terminate()
                exit(1)
            # calculate MD5 of *_prsv.mkv audio/video stream
            calculated_md5_mkv_stream = subprocess.run(["ffmpeg", "-i", f"{item.path.replace('.mov', '.mkv')}", "-f", "hash", "-hash", "md5", "-"], capture_output=True, text=True).stdout.strip()
            # wait for *_prsv.mov stream MD5 calculation to complete
            calculated_md5_mov_stream = calculating_md5_mov_stream.communicate()[0].strip()
            # compare MD5 of *_prsv.mov audio/video stream with MD5 of *_prsv.mkv audio/video stream
            if calculated_md5_mov_stream != calculated_md5_mkv_stream:
                print("\n❌ MD5 STREAM MISMATCH")
                print(f"{item.name}:        {calculated_md5_mov_stream}")
                print(f"{item.name.replace('.mov', '.stream.md5')}:    {calculated_md5_mkv_stream}")
                exit(1)
            else:
                print("\n✅ MD5 STREAM MATCH")
                print(f"{item.name}:        {calculated_md5_mov_stream}")
                print(f"{item.name.replace('.mov', '.stream.md5')}:    {calculated_md5_mkv_stream}")
                with open(f"{item.path.replace('.mov', '.stream.md5')}", "w") as f:
                    f.write(calculated_md5_mkv_stream)
            # copy everything except *_prsv.mov and *_prsv.mov.md5 to destination
            print(f"\n⏳ moving files to destination")
            shutil.copytree(entry.path, os.path.join(args.destination, entry.name), ignore=shutil.ignore_patterns("*_prsv.mov*"))
            os.remove(item.path.replace('.mov', '.mkv'))
            os.remove(item.path.replace('.mov', '.ffmpeg.log'))
            os.remove(item.path.replace('.mov', '.stream.md5'))
            print("\n✅ DONE")
