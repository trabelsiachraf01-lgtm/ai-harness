import pytest

from harness.attachments import MAX_IMAGE_BYTES, AttachmentError, find_images


def png(tmp_path, name="shot.png", data=b"\x89PNG-fake"):
    (tmp_path / name).write_bytes(data)


def test_attaches_a_referenced_image(tmp_path):
    png(tmp_path)
    [image] = find_images("what is in @shot.png?", tmp_path)
    assert image.name == "shot.png"
    assert image.data_url.startswith("data:image/png;base64,")
    assert image.size == len(b"\x89PNG-fake")


def test_trailing_punctuation_and_duplicates(tmp_path):
    png(tmp_path)
    assert len(find_images("see @shot.png. and again @shot.png, ok", tmp_path)) == 1


def test_quoted_and_escaped_paths_with_spaces(tmp_path):
    png(tmp_path, "my shot.png")
    assert find_images('look @"my shot.png"', tmp_path)[0].name == "my shot.png"
    assert find_images(r"look @my\ shot.png", tmp_path)[0].name == "my shot.png"


def test_jpeg_mime_and_absolute_path(tmp_path):
    png(tmp_path, "a.JPG")
    [image] = find_images(f"@{tmp_path / 'a.JPG'}", tmp_path / "elsewhere")
    assert image.data_url.startswith("data:image/jpeg;base64,")


def test_non_image_mentions_are_plain_text(tmp_path):
    assert find_images("ping @alice about @notes.md", tmp_path) == []


def test_missing_image_is_an_error_not_a_silent_drop(tmp_path):
    with pytest.raises(AttachmentError, match="not found"):
        find_images("@nope.png", tmp_path)


def test_oversized_image_is_refused(tmp_path):
    png(tmp_path, "big.png", b"0" * (MAX_IMAGE_BYTES + 1))
    with pytest.raises(AttachmentError, match="limit"):
        find_images("@big.png", tmp_path)


def test_dropped_path_attaches_without_at_sign(tmp_path):
    png(tmp_path, "my shot.png")
    assert find_images(f"'{tmp_path / 'my shot.png'}' what is this?", tmp_path)[0].name == "my shot.png"
    assert find_images(str(tmp_path / "my shot.png").replace(" ", "\\ "), tmp_path)[0].name == "my shot.png"


def test_bare_paths_that_do_not_exist_are_just_text(tmp_path):
    assert find_images("see /usr/share/nope.png or foo/bar.png", tmp_path) == []
