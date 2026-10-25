"""What vision models get for still and animated images."""

import io

import pytest
from PIL import Image

from yggdrasil.agent.media import model_image


def _gif(frames: list[Image.Image], durations: list[int]) -> bytes:
    out = io.BytesIO()
    frames[0].save(out, "GIF", save_all=True, append_images=frames[1:], duration=durations, loop=0)
    return out.getvalue()


def _solid(color: str, size: tuple[int, int] = (120, 120)) -> Image.Image:
    return Image.new("RGB", size, color)


def test_still_image_is_passed_through_without_a_note() -> None:
    out = io.BytesIO()
    _solid("red").save(out, "PNG")
    image = model_image(out.getvalue())
    assert (image.data, image.times, image.note) == (out.getvalue(), (), "")


def test_animation_becomes_one_sheet_with_a_frame_per_change() -> None:
    # A long hold, a 0.1 s flash, another hold: three frames, timed by playback, not by frame count.
    blob = _gif([_solid("red"), _solid("blue"), _solid("green")], [1000, 100, 1000])
    image = model_image(blob)
    assert image.times == pytest.approx((0.0, 1.0, 1.1))
    assert image.duration == pytest.approx(2.1)
    assert "3 帧" in image.note
    assert "2.1 秒" in image.note
    with Image.open(io.BytesIO(image.data)) as sheet:
        assert not getattr(sheet, "is_animated", False)
        assert sheet.width > 120  # several frames side by side


def test_a_small_local_change_counts_as_a_new_frame() -> None:
    blink = _solid("white", (240, 240))
    blink.paste(_solid("black", (30, 30)), (100, 60))
    image = model_image(_gif([_solid("white", (240, 240)), blink], [500, 500]))
    assert image.times == pytest.approx((0.0, 0.5))


def test_animation_that_never_changes_is_treated_as_a_still() -> None:
    blob = _gif([_solid("red"), _solid("red"), _solid("red")], [100, 100, 100])
    image = model_image(blob)
    assert (image.times, image.note) == ((), "")


def test_long_animation_keeps_at_most_sixteen_frames_from_start_to_end() -> None:
    frames = [_solid(("red", "green", "blue")[i % 3], (400, 300)) for i in range(60)]
    image = model_image(_gif(frames, [100] * 60))
    assert len(image.times) == 16
    assert (image.times[0], image.times[-1]) == pytest.approx((0.0, 5.9))
    with Image.open(io.BytesIO(image.data)) as sheet:
        assert max(sheet.size) <= 1568


def test_zero_delay_frames_play_at_a_tenth_of_a_second() -> None:
    blob = _gif([_solid(c) for c in ("red", "blue", "green", "yellow")], [0, 0, 0, 0])
    image = model_image(blob)
    assert image.times == pytest.approx((0.0, 0.1, 0.2, 0.3))
    assert image.duration == pytest.approx(0.4)
