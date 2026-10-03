import subprocess

import av
import pytest
from PIL import ImageChops, ImageStat

from easy_cutter.video import VideoReader, export_clip, ffmpeg_exe

FPS = 25


@pytest.fixture(scope='module')
def video(tmp_path_factory):
    # 20 s Testvideo mit Ton und nur zwei Keyframes (0 s und 10 s), damit ein Schnitt
    # auf Keyframes sofort auffallen würde
    path = str(tmp_path_factory.mktemp('video') / 'test.mp4')
    subprocess.run([
        ffmpeg_exe(), '-y', '-loglevel', 'error',
        '-f', 'lavfi', '-i', f'testsrc2=size=320x180:rate={FPS}:duration=20',
        '-f', 'lavfi', '-i', 'sine=frequency=440:duration=20',
        '-c:v', 'libx264', '-g', '250', '-c:a', 'aac', '-shortest', path,
    ], check=True)
    return path


def diff(a, b):
    return sum(ImageStat.Stat(ImageChops.difference(a.convert('RGB'), b.convert('RGB'))).mean) / 3


def frame_times(path):
    with av.open(path) as c:
        return [float(f.time) for f in c.decode(video=0)]


def test_duration(video):
    reader = VideoReader(video)
    assert reader.duration == pytest.approx(20, abs=0.05)
    reader.close()


@pytest.mark.parametrize('start', [0, 3, 3.01, 9.99, 10, 14.5])
def test_preview_frames(video, start):
    reader = VideoReader(video)
    first, last = reader.resolve_range(start, start + 5)
    reader.close()
    expected_first = -(-start * FPS // 1) / FPS  # erster Frame >= start
    assert first == pytest.approx(expected_first, abs=1e-6)
    assert last < start + 5
    assert last + 1 / FPS >= start + 5 - 1e-6


@pytest.mark.parametrize('start', [3, 3.01, 7.5])
def test_export_matches_preview(video, tmp_path, start):
    out = str(tmp_path / 'clip.mp4')
    first, last = export_clip(video, out, start, start + 5)
    times = frame_times(out)
    assert len(times) == round((last - first) * FPS) + 1
    with av.open(out) as c:
        assert len(c.streams.audio) == 1
        clip = [f.to_image() for f in c.decode(video=0)]

    src = VideoReader(video)
    src_first = src.first_frame_at(start).to_image()
    src_last = src.last_frame_before(start + 5).to_image()
    neighbour = src.first_frame_at(start + 5).to_image()
    src.close()
    # Kodierverlust ist klein, ein Nachbarframe unterscheidet sich deutlich (testsrc2 bewegt sich)
    assert diff(clip[0], src_first) < 3
    assert diff(clip[-1], src_last) < 3
    assert diff(clip[-1], neighbour) > diff(clip[-1], src_last) + 1


def test_end_of_video(video):
    reader = VideoReader(video)
    first, last = reader.resolve_range(15, 20)
    reader.close()
    assert first == pytest.approx(15)
    assert last == pytest.approx(20 - 1 / FPS)
