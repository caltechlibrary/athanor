#!/bin/bash

# DataStagingArea-MediaPreserve-FFV1.sh
# Version 0.2.0

if [ -z "$2" ]; then
    echo -e "\nUSAGE: /bin/bash $0 /path/to/source /path/to/destination\n"
    echo -e "/path/to/source\t\tuse the directory containing the Archival Object directories"
    echo -e "/path/to/destination\tuse the directory where the Archival Object directories will be moved to"
    exit 1
fi

cd "$1" || exit 1
for directory in *; do
    [ -d "$directory" ] && echo "🙃 $directory"
    # check MD5 of *_prsv.mov file
    if [ -f "$directory/${directory}_prsv.mov" ]; then
        mov_calculated_md5=$(md5sum "$directory/${directory}_prsv.mov" | cut -d ' ' -f 1)
        echo "🐞 mov_calculated_md5: $mov_calculated_md5"
        mov_file_md5=$(cut -d ' ' -f 1 < "$directory/${directory}_prsv.mov.md5" | tr '[:upper:]' '[:lower:]')
        echo "🐞 mov_file_md5: $mov_file_md5"
        if ! [ "$mov_calculated_md5" == "$mov_file_md5" ]; then
            echo -e "\n❌ MD5 MISMATCH"
            echo -e "$directory/${directory}_prsv.mov:\t$mov_calculated_md5"
            echo -e "$directory/${directory}_prsv.mov.md5:\t$mov_file_md5"
            exit 1
        else
            echo -e "\n✅ MD5 MATCH"
            echo -e "$directory/${directory}_prsv.mov:\t$mov_calculated_md5"
            echo -e "$directory/${directory}_prsv.mov.md5:\t$mov_file_md5"
        fi
    fi
    # transcode *_prsv.mov to *_prsv.mkv
    if [ -f "$directory/${directory}_prsv.mov" ]; then
        if ! ffmpeg -hide_banner -nostats -i "$directory/${directory}_prsv.mov" -map 0 -dn -c:v ffv1 -level 3 -g 1 -slicecrc 1 -slices 4 -c:a copy "$directory/${directory}_prsv.mkv" 2>&1 | tee "$directory/${directory}_prsv.ffmpeg.log"; then
            echo -e "\n❌ FFMPEG TRANSCODE FAILED"
            exit 1
        else
            # create raw audio & video stream MD5 checksum of *_prsv.mkv
            if [ -f "$directory/${directory}_prsv.mkv" ]; then
                ffmpeg -i "$directory/${directory}_prsv.mkv" -f hash -hash md5 "$directory/${directory}_prsv.raw.md5"
            fi
        fi
    fi
    # compare raw audio & video stream MD5 checksum of *_prsv.mov and *_prsv.mkv
    raw_mov_calculated_md5=$(ffmpeg -i "$directory/${directory}_prsv.mov" -f hash -hash md5 -)
    echo "🐞 raw_mov_calculated_md5: $raw_mov_calculated_md5"
    raw_mkv_file_md5=$(cat "$directory/${directory}_prsv.raw.md5")
    echo "🐞 raw_mkv_file_md5: $raw_mkv_file_md5"
    if ! [ "$raw_mov_calculated_md5" == "$raw_mkv_file_md5" ]; then
        echo -e "\n❌ RAW MD5 MISMATCH"
        echo -e "$directory/${directory}_prsv.mov:\t$raw_mov_calculated_md5"
        echo -e "$directory/${directory}_prsv.raw.md5:\t$raw_mkv_file_md5"
        exit 1
    else
        echo -e "\n✅ RAW MD5 MATCH"
        echo -e "$directory/${directory}_prsv.mov:\t$raw_mov_calculated_md5"
        echo -e "$directory/${directory}_prsv.raw.md5:\t$raw_mkv_file_md5"
    fi
    # make destination subdirectory
    mkdir -p "$2/$directory"
    # move *_prsv.mkv, *_prsv.ffmpeg.log, *_prsv.raw.md5 to destination subdirectory
    mv "$directory/${directory}_prsv.mkv" "$2/$directory"
    mv "$directory/${directory}_prsv.ffmpeg.log" "$2/$directory"
    mv "$directory/${directory}_prsv.raw.md5" "$2/$directory"
done
