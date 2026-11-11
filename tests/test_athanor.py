import struct

import pytest

from athanor import athanor


def atom(kind: bytes, body: bytes = b"") -> bytes:
    return struct.pack(">I4s", 8 + len(body), kind) + body


def timescale_header(timescale: int, duration: int) -> bytes:
    """Body of a version 0 mvhd or mdhd atom, as far as athanor reads it."""
    return struct.pack(">IIIII", 0, 0, 0, timescale, duration) + bytes(80)


def video_track(sample_count: int, media_timescale: int, media_duration: int, edits=None, handler=b"vide") -> bytes:
    """A trak atom; edits are (duration in movie time, media time, rate) tuples."""
    children = [
        atom(b"mdia", b"".join([
            atom(b"mdhd", timescale_header(media_timescale, media_duration)),
            atom(b"hdlr", struct.pack(">II4s", 0, 0, handler) + bytes(12)),
            atom(b"minf", atom(b"stbl", atom(b"stsz", struct.pack(">III", 0, 0, sample_count)))),
        ])),
    ]
    if edits is not None:
        entries = b"".join(struct.pack(">IiI", duration, media_time, int(rate * 65536)) for duration, media_time, rate in edits)
        children.insert(0, atom(b"edts", atom(b"elst", struct.pack(">II", 0, len(edits)) + entries)))
    return atom(b"trak", b"".join(children))


def write_mov(path, tracks, movie_timescale=600, moov_first=True):
    moov = atom(b"moov", atom(b"mvhd", timescale_header(movie_timescale, 0)) + b"".join(tracks))
    mdat = atom(b"mdat", bytes(64))
    path.write_bytes(atom(b"ftyp", b"qt  " + bytes(4)) + (moov + mdat if moov_first else mdat + moov))
    return path


@pytest.mark.parametrize("moov_first", [True, False])
def test_edit_list_that_skips_leader(tmp_path, moov_first):
    # 11053 frames at 30000/1001; the edit starts 105316/30000 seconds in
    track = video_track(11053, 30000, 11053 * 1001, edits=[(10847421, 105316, 1)])
    path = write_mov(tmp_path / "leader.mov", [track], movie_timescale=30000, moov_first=moov_first)

    [result] = athanor.read_mov_video_edit_lists(path)

    assert result["sample_count"] == 11053
    assert result["edits"][0]["media_start"] == pytest.approx(105316 / 30000)
    assert result["edits"][0]["duration"] == pytest.approx(10847421 / 30000)
    assert not athanor.edit_list_hides_nothing(result)


def test_edit_list_that_plays_every_frame(tmp_path):
    track = video_track(300, 30000, 300 * 1001, edits=[(300 * 1001 // 50, 0, 1)])
    path = write_mov(tmp_path / "whole.mov", [track], movie_timescale=600)

    [result] = athanor.read_mov_video_edit_lists(path)

    assert athanor.edit_list_hides_nothing(result)


def test_track_without_edit_list_hides_nothing(tmp_path):
    path = write_mov(tmp_path / "plain.mov", [video_track(10, 2997, 10 * 100)])

    [result] = athanor.read_mov_video_edit_lists(path)

    assert result["edits"] == []
    assert athanor.edit_list_hides_nothing(result)


def test_segmented_and_empty_edits(tmp_path):
    track = video_track(6000, 29970, 6000 * 1000, edits=[(600, -1, 1), (43244, 0, 1), (45225, 2161035, 1)])
    path = write_mov(tmp_path / "segments.mov", [track])

    [result] = athanor.read_mov_video_edit_lists(path)

    assert [edit["media_start"] for edit in result["edits"]] == [None, 0, pytest.approx(2161035 / 29970)]
    assert not athanor.edit_list_hides_nothing(result)


def test_only_video_tracks_are_read(tmp_path):
    tracks = [video_track(1, 600, 600, handler=b"tmcd"), video_track(5, 2997, 500)]
    path = write_mov(tmp_path / "tracks.mov", tracks)

    assert [track["sample_count"] for track in athanor.read_mov_video_edit_lists(path)] == [5]


def test_file_without_moov(tmp_path):
    path = tmp_path / "empty.mov"
    path.write_bytes(atom(b"ftyp", b"qt  " + bytes(4)) + atom(b"mdat", bytes(16)))

    assert athanor.read_mov_video_edit_lists(path) == []


def timecode_stream(index=1, entries="1", start="00:00:00;00", reel=None):
    tags = {"timecode": start}
    if reel:
        tags["reel_name"] = reel
    return {"index": index, "codec_type": "data", "codec_tag_string": "tmcd", "nb_frames": entries, "tags": tags}


def test_empty_timecode_is_dropped():
    problems, notes, options = athanor.analyze_data_streams({"streams": [timecode_stream()]})

    assert problems == []
    assert "Dropped" in notes[0]
    assert options == ["-metadata:s:v", "timecode="]


def test_timecode_with_a_start_is_kept():
    problems, notes, options = athanor.analyze_data_streams({"streams": [timecode_stream(start="01:00:00:00")]})

    assert problems == []
    assert "Kept as a TIMECODE tag" in notes[0]
    assert options == []


def test_timecode_reel_name_is_kept():
    problems, _, options = athanor.analyze_data_streams({"streams": [timecode_stream(reel="Tape 7")]})

    assert problems == []
    assert options == ["-metadata:s:v", "reel_name=Tape 7"]


def test_timecode_with_breaks_needs_review():
    problems, _, _ = athanor.analyze_data_streams({"streams": [timecode_stream(entries="3")]})

    assert "review" in problems[0]


def test_other_data_stream_needs_review():
    stream = {"index": 2, "codec_type": "data", "codec_tag_string": "gpmd"}

    problems, _, _ = athanor.analyze_data_streams({"streams": [stream]})

    assert "gpmd" in problems[0]


@pytest.mark.parametrize(
    "codec, channels, expected_problems",
    [("pcm_s24le", 2, 0), ("flac", 8, 0), ("pcm_f32le", 2, 1), ("pcm_s32le", 2, 1), ("pcm_s24le", 10, 1)],
)
def test_audio_flac_cannot_hold(codec, channels, expected_problems):
    stream = {"index": 1, "codec_type": "audio", "codec_name": codec, "channels": channels}

    assert len(athanor.analyze_audio_streams({"streams": [stream]})) == expected_problems


def test_video_property_comparison():
    source = {"streams": [{"index": 0, "codec_type": "video", "width": 720, "field_order": None, "color_space": "bt709"}]}
    output = {"streams": [{"index": 0, "codec_type": "video", "width": 720, "field_order": "progressive", "color_space": "bt601"}]}

    rows, changed, labeled = athanor.compare_video_properties(source, output)

    assert changed == ["color_space"]
    assert labeled == ["field_order"]
    assert len(rows) == len(athanor.SIGNIFICANT_VIDEO_PROPERTIES)


def test_attached_pictures_are_not_video_streams():
    probed = {"streams": [
        {"index": 0, "codec_type": "video", "codec_name": "v210"},
        {"index": 1, "codec_type": "video", "codec_name": "mjpeg", "disposition": {"attached_pic": 1}},
    ]}

    assert [stream["index"] for stream in athanor.streams_of_type(probed, "video")] == [0]


def test_mov_family_detection():
    assert athanor.is_mov_family({"format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2"}})
    assert not athanor.is_mov_family({"format": {"format_name": "matroska,webm"}})


@pytest.mark.parametrize(
    "stem, expected",
    [("TVK_158_4_Lucille_Price_prsv", "TVK_158_4_Lucille_Price_FFV1"), ("capsca_00008_r01_prsv", "capsca_00008_r01_FFV1"), ("reel", "reel_FFV1")],
)
def test_output_names(stem, expected):
    assert athanor.replace_last_stem_segment(stem, "FFV1") == expected


def test_spinner_prints_once_without_a_terminal(capsys):
    spinner = athanor.Spinner()
    spinner.start("waiting")
    spinner.stop()

    assert capsys.readouterr().out == "waiting\n"
