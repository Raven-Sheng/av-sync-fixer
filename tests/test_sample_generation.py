"""样本工具失败时不覆盖原文件、不留下损坏的正式样本。"""

import pytest

from app.ffmpeg_utils import MediaError, MissingToolError
from tests.generate_samples import generate_samples, main


def test_existing_sample_is_preserved_before_any_generation(tmp_path, monkeypatch):
    existing = tmp_path / "audio_shorter.mp4"
    existing.write_bytes(b"keep")
    monkeypatch.setattr("tests.generate_samples.check_tools", lambda: pytest.fail("应先拒绝已有目标"))
    with pytest.raises(MediaError, match="样本已存在"):
        generate_samples(tmp_path)
    assert existing.read_bytes() == b"keep"
    assert list(tmp_path.iterdir()) == [existing]


@pytest.mark.parametrize("stage", ["encode", "probe"])
def test_generation_failure_cleans_staging_without_publishing(tmp_path, monkeypatch, stage):
    monkeypatch.setattr("tests.generate_samples.check_tools", lambda: {"ffmpeg": "ffmpeg", "ffprobe": "ffprobe"})
    calls = []
    def encode(ffmpeg, path, options):
        path.write_bytes(b"partial")
        calls.append(path)
        if stage == "encode" and len(calls) == 2:
            raise MediaError("模拟生成失败")
    def probe(path, ffprobe):
        if stage == "probe" and len(calls) == 2:
            raise MediaError("模拟探测失败")
        return {}
    monkeypatch.setattr("tests.generate_samples.generate_media", encode)
    monkeypatch.setattr("tests.generate_samples.probe_media", probe)
    with pytest.raises(MediaError, match="模拟"):
        generate_samples(tmp_path)
    assert len(calls) == 2
    assert list(tmp_path.iterdir()) == []


def test_sample_tool_missing_dependency_is_readable(tmp_path, monkeypatch, capsys):
    def missing():
        raise MissingToolError("ffmpeg")
    monkeypatch.setattr("tests.generate_samples.check_tools", missing)
    directory = tmp_path / "新样本"
    assert main(["--output-dir", str(directory)]) == 1
    error = capsys.readouterr().err
    assert "未找到 ffmpeg" in error and "Traceback" not in error
    assert not directory.exists()
