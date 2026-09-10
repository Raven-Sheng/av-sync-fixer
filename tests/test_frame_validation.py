import sys

import pytest

from app.analyzer import parse_stream
from app.color import select_color_plan
from app.frame_validation import InputFrameValidator
from app.ffmpeg_utils import MediaError, run_ffmpeg

PREFIX = "[showinfo@avsync_input @ 0123] "
FRAME = PREFIX + "n:   0 pts:0 pts_time:0 fmt:yuv420p s:160x90"
COLOR = PREFIX + "color_range:tv color_space:bt709 color_primaries:bt709 color_trc:bt709"


@pytest.fixture
def validator():
    video = parse_stream({"pix_fmt": "yuv420p", "color_range": "tv", "color_space": "bt709",
        "color_primaries": "bt709", "color_transfer": "bt709"})
    return InputFrameValidator(video, select_color_plan(video))


@pytest.mark.parametrize("changed", [COLOR.replace("tv", "pc"), COLOR.replace("bt709", "bt2020nc", 1),
    COLOR.replace("color_trc:bt709", "color_trc:smpte2084"), COLOR.replace("color_range:tv", "color_range:unknown")])
def test_conflicting_decoded_color_is_rejected(validator, changed):
    validator.consume(FRAME)
    validator.consume(COLOR)
    validator.consume(FRAME)
    with pytest.raises(ValueError):
        validator.consume(changed)


@pytest.mark.parametrize("lines", [[], [FRAME], [FRAME, FRAME], [COLOR], [FRAME, COLOR.replace("color_space:bt709", "")]])
def test_missing_or_unparseable_records_never_count_as_verified(validator, lines):
    with pytest.raises(ValueError):
        for line in lines:
            validator.consume(line)
        validator.finish()


def test_reader_handles_filter_reinitialization_without_losing_baseline(validator):
    validator.consume(FRAME)
    validator.consume(COLOR)
    # Filter-local n resets on reinit. The Python baseline must survive it.
    with pytest.raises(ValueError, match="中途"):
        validator.consume(FRAME.replace("160x90", "320x180"))


def test_hdr_side_data_cannot_hide_behind_sdr_tags(validator):
    validator.consume(FRAME)
    with pytest.raises(ValueError, match="HDR"):
        validator.consume(PREFIX + "side data - Mastering Display Metadata:")


def test_constant_space_for_long_log_stream(validator):
    for _ in range(10000):
        validator.consume(FRAME)
        validator.consume(COLOR)
    validator.finish()
    assert validator.frames == validator.colors == 10000
    assert validator.baseline == validator.expected


def test_validator_failure_terminates_real_child_and_is_not_lost_in_reader_thread(validator):
    bad = COLOR.replace("tv", "pc")
    script = f"import sys,time; print({FRAME!r},file=sys.stderr,flush=True); print({bad!r},file=sys.stderr,flush=True); time.sleep(60)"
    with pytest.raises(MediaError, match="输入帧颜色与 RepairPlan 不一致"):
        run_ffmpeg([sys.executable, "-u", "-c", script], input_validator=validator)


def test_successful_child_without_diagnostics_is_not_verified(validator):
    with pytest.raises(MediaError, match="未获得完整"):
        run_ffmpeg([sys.executable, "-c", "pass"], input_validator=validator)


def test_frame_log_volume_does_not_evict_decoder_failure(validator):
    script = ("import sys; print('corrupt input packet',file=sys.stderr); "
              f"[(print({FRAME!r},file=sys.stderr),print({COLOR!r},file=sys.stderr)) for _ in range(200)]; sys.exit(7)")
    with pytest.raises(MediaError, match="corrupt input packet"):
        run_ffmpeg([sys.executable, "-u", "-c", script], input_validator=validator)
