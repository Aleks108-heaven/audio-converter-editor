from __future__ import annotations

import threading
import time

import numpy as np
import pytest
import sounddevice as sd

from tonewright.audio.devices import AudioDeviceError
from tonewright.audio.playback import Player
from tonewright.audio.recorder import Recorder
from tonewright.core.document import Document
from tonewright.core.source import Source

SR = 8000


class FakeOutput:
    """Pulls audio through the callback like PortAudio would, and keeps what it 'played'."""
    instances: list["FakeOutput"] = []

    def __init__(self, samplerate, channels, dtype, callback, finished_callback=None, device=None, **kw):
        self.samplerate, self.channels, self.cb, self.fin = samplerate, channels, callback, finished_callback
        self.latency = 0.0
        self.played: list[np.ndarray] = []
        self.active = False
        self.closed = False
        self._thread = None
        FakeOutput.instances.append(self)

    def start(self):
        self.active = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        while self.active:
            out = np.zeros((256, self.channels), np.float32)
            try:
                self.cb(out, 256, None, None)
            except sd.CallbackStop:
                self.played.append(out.copy())
                self.active = False
                if self.fin:
                    self.fin()
                return
            self.played.append(out.copy())
            time.sleep(0.0004)

    def abort(self):
        self.active = False

    stop = abort

    def close(self):
        self.active = False
        self.closed = True

    def audio(self) -> np.ndarray:
        return np.concatenate(self.played) if self.played else np.zeros((0, self.channels), np.float32)


def ramp_doc(n: int = 4000, channels: int = 2) -> Document:
    """Strictly positive, strictly increasing samples so every frame is unique and non-silent."""
    data = np.repeat((np.arange(1, n + 1, dtype=np.float32) / (n + 1))[:, None], channels, axis=1)
    return Document.from_source(Source.from_array(data, SR))


def wait_for(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.005)
    return False


@pytest.fixture
def player():
    FakeOutput.instances.clear()
    p = Player(stream_factory=FakeOutput)
    yield p
    p.stop()


def played_frames(stream: FakeOutput) -> np.ndarray:
    audio = stream.audio()
    return audio[np.abs(audio).max(axis=1) > 0]


def test_plays_the_whole_document_then_reports_finished(player):
    doc = ramp_doc()
    player.play(doc.snapshot())
    assert wait_for(player.consume_finished)
    assert player.state == "stopped"
    np.testing.assert_array_equal(played_frames(FakeOutput.instances[-1]), doc.read(0, doc.frames))
    assert player.consume_finished() is False


def test_mono_documents_are_played_on_both_channels(player):
    doc = ramp_doc(1000, channels=1)
    player.play(doc.snapshot())
    assert wait_for(player.consume_finished)
    out = played_frames(FakeOutput.instances[-1])
    np.testing.assert_array_equal(out[:, 0], doc.read(0, 1000)[:, 0])
    np.testing.assert_array_equal(out[:, 0], out[:, 1])


def test_play_range_and_position(player):
    doc = ramp_doc()
    player.play(doc.snapshot(), start=1000, end=1600)
    assert wait_for(player.consume_finished)
    np.testing.assert_array_equal(played_frames(FakeOutput.instances[-1]), doc.read(1000, 1600))
    assert 1500 <= player.position <= 1600


def test_pause_resume_loses_nothing(player):
    doc = ramp_doc(6000)
    player.play(doc.snapshot())
    assert wait_for(lambda: player.position > 500)
    player.pause()
    assert player.state == "paused"
    time.sleep(0.05)
    frozen = player.position
    time.sleep(0.15)
    assert player.position == frozen
    player.resume()
    assert wait_for(player.consume_finished)
    np.testing.assert_array_equal(played_frames(FakeOutput.instances[-1]), doc.read(0, doc.frames))


def test_seek_jumps_without_replaying_old_audio(player):
    doc = ramp_doc(20000)
    player.play(doc.snapshot())
    assert wait_for(lambda: player.position > 300)
    player.seek(15000)
    assert wait_for(player.consume_finished)
    out = played_frames(FakeOutput.instances[-1])
    first_after = int(np.argmax(out[:, 0] >= doc.read(15000, 15001)[0, 0] - 1e-9))
    np.testing.assert_array_equal(out[first_after:], doc.read(15000, 20000))
    before = out[:first_after, 0]
    assert (np.diff(before) > 0).all()                      # what played before the jump was in order


def test_loop_repeats_the_range_and_position_stays_inside(player):
    doc = ramp_doc(4000)
    player.play(doc.snapshot(), start=500, end=4000, loop=True, loop_range=(1000, 1500))
    assert wait_for(lambda: sum(len(a) for a in FakeOutput.instances[-1].played) > 5000)
    for _ in range(20):
        assert 500 <= player.position <= 1500
        time.sleep(0.005)
    pos = player.stop()
    assert 500 <= pos <= 1500
    out = played_frames(FakeOutput.instances[-1])[:, 0]
    rewinds = np.flatnonzero(np.diff(out) < 0)
    assert len(rewinds) >= 2
    first, second = out[rewinds[0] + 1: rewinds[1] + 1], out[rewinds[1] + 1: rewinds[2] + 1] if len(rewinds) > 2 else None
    expected = doc.read(1000, 1500)[:, 0]
    np.testing.assert_array_equal(first, expected)
    if second is not None:
        np.testing.assert_array_equal(second, expected)


def test_stop_returns_position_closes_stream_and_is_idempotent(player):
    doc = ramp_doc(40000)
    player.play(doc.snapshot(), start=100)
    assert wait_for(lambda: player.position > 1000)
    pos = player.stop()
    assert 1000 <= pos < 40000 and player.state == "stopped"
    assert FakeOutput.instances[-1].closed
    player.stop()
    assert player.state == "stopped"


def test_volume_and_peak_meter(player):
    doc = Document.from_source(Source.from_array(np.full((2000, 2), 0.5, np.float32), SR))
    player.volume = 0.5
    player.play(doc.snapshot())
    assert wait_for(player.consume_finished)
    out = played_frames(FakeOutput.instances[-1])
    assert float(out.max()) == pytest.approx(0.25)
    # the peak accumulated during playback was available; once consumed it resets
    player.consume_peak()
    assert player.consume_peak() == (0.0, 0.0)


def test_empty_selection_is_a_noop_that_reports_finished(player):
    doc = ramp_doc(100)
    player.play(doc.snapshot(), start=100, end=100)
    assert player.state == "stopped" and player.consume_finished()
    assert not FakeOutput.instances


def test_device_failure_becomes_audio_device_error():
    def broken(**kw):
        raise OSError("no device")

    p = Player(stream_factory=broken)
    with pytest.raises(AudioDeviceError):
        p.play(ramp_doc(100).snapshot())
    assert p.state == "stopped"


def test_editing_the_document_does_not_disturb_playback(player):
    doc = ramp_doc(8000)
    snap = doc.snapshot()
    player.play(snap)
    assert wait_for(lambda: player.position > 200)
    doc.delete(0, 4000)
    assert wait_for(player.consume_finished)
    original = ramp_doc(8000).read(0, 8000)
    np.testing.assert_array_equal(played_frames(FakeOutput.instances[-1]), original)


class ManualOutput(FakeOutput):
    """A card that only plays when the test pulls a block, so what comes out is exact and never timing-dependent."""

    def start(self):
        self.active = True

    def pull(self, frames: int) -> np.ndarray:
        out = np.zeros((frames, self.channels), np.float32)
        self.cb(out, frames, None, None)
        return out


@pytest.fixture
def manual():
    ManualOutput.instances.clear()
    p = Player(stream_factory=ManualOutput)
    yield p
    p.stop()


def test_swap_carries_on_from_where_the_card_is_with_no_gap(manual):
    doc = ramp_doc(20000)
    manual.play(doc.snapshot())
    card = ManualOutput.instances[-1]
    heard = card.pull(1000)
    np.testing.assert_array_equal(heard, doc.read(0, 1000))
    doc.delete(2000, 6000)                                   # an edit ahead of the playhead
    assert manual.swap(doc.snapshot())
    after = card.pull(3000)
    assert (np.abs(after).max(axis=1) > 0).all()             # not one silent frame while the new audio took over
    np.testing.assert_array_equal(after, doc.read(1000, 4000))


def test_swap_can_jump_and_shortens_the_end(manual):
    doc = ramp_doc(20000)
    manual.play(doc.snapshot())
    card = ManualOutput.instances[-1]
    card.pull(500)
    short = doc.snapshot()
    doc.delete(10000, 20000)
    assert manual.swap(doc.snapshot(), frame=9000)
    np.testing.assert_array_equal(card.pull(1000), doc.read(9000, 10000))
    assert manual.end == 10000
    assert short.frames == 20000                             # the snapshot being replaced was not touched


def test_swap_does_nothing_when_nothing_is_playing(manual):
    assert manual.swap(ramp_doc(100).snapshot()) is False
    assert manual.sequence is None and manual.state == "stopped"


def test_swap_while_paused_stays_paused_and_resumes_with_the_new_audio(manual):
    doc = ramp_doc(20000)
    manual.play(doc.snapshot())
    card = ManualOutput.instances[-1]
    card.pull(500)
    manual.pause()
    doc.delete(0, 5000)
    assert manual.swap(doc.snapshot(), frame=0)
    assert not card.pull(300).any() and manual.state == "paused"
    manual.resume()
    np.testing.assert_array_equal(card.pull(300), doc.read(0, 300))


def test_sequence_is_what_is_playing(manual):
    doc = ramp_doc(5000)
    a, b = doc.snapshot(), doc.snapshot()
    manual.play(a)
    assert manual.sequence is a
    manual.swap(b)
    assert manual.sequence is b
    manual.stop()
    assert manual.sequence is None


def test_seek_is_heard_at_once_even_with_audio_already_queued(manual):
    doc = ramp_doc(40000)
    manual.play(doc.snapshot())
    card = ManualOutput.instances[-1]
    card.pull(700)
    assert wait_for(lambda: manual._feedq[1].qsize() >= 8)    # a good stretch of the old position is waiting in the queue
    manual.seek(30000)
    np.testing.assert_array_equal(card.pull(500), doc.read(30000, 30500))


def test_the_position_follows_a_seek_back_before_where_playback_began(manual):
    manual.play(ramp_doc(40000).snapshot(), start=20000)
    card = ManualOutput.instances[-1]
    card.pull(300)
    manual.seek(5000)
    assert manual.position == 5000                           # not held at 20000, where this run started
    card.pull(300)
    assert manual.position == 5300


def test_swap_while_looping_keeps_looping_inside_the_new_range(manual):
    doc = ramp_doc(6000)
    manual.play(doc.snapshot(), start=0, end=6000, loop=True, loop_range=(1000, 2000))
    card = ManualOutput.instances[-1]
    card.pull(100)
    doc.delete(0, 500)
    assert manual.swap(doc.snapshot(), frame=600, loop_range=(500, 1500))
    assert manual.loop_range == (500, 1500)
    heard = card.pull(1200)
    np.testing.assert_array_equal(heard[:900], doc.read(600, 1500))
    np.testing.assert_array_equal(heard[900:], doc.read(500, 800))


@pytest.mark.skipif(not sd.query_devices(), reason="no audio devices")
def test_real_device_opens_and_plays_silence():
    doc = Document.from_source(Source.silence(int(0.25 * 44100), 44100, 2))
    p = Player()
    try:
        p.play(doc.snapshot())
    except AudioDeviceError as exc:
        pytest.skip(str(exc))
    assert wait_for(p.consume_finished, timeout=5)
    p.stop()


# ------------------------------------------------------------------ recorder
class FakeInput:
    def __init__(self, samplerate, channels, dtype, callback, device=None, **kw):
        self.cb, self.channels = callback, channels
        self.active = False
        self.sent = 0
        self.closed = False

    def start(self):
        self.active = True
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while self.active:
            block = np.full((400, self.channels), 0.25, np.float32)
            block[:, 0] = np.arange(self.sent, self.sent + 400) / 100000
            self.cb(block, 400, None, None)
            self.sent += 400
            time.sleep(0.0005)

    def stop(self):
        self.active = False

    def close(self):
        self.closed = True


def test_recorder_collects_audio_and_meters():
    rec = Recorder(sr=SR, channels=2, stream_factory=FakeInput)
    rec.start()
    assert rec.state == "recording"
    assert wait_for(lambda: rec.seconds > 0.4)
    left, right = rec.consume_peak()
    assert right == pytest.approx(0.25) and left > 0
    src = rec.stop()
    assert rec.state == "idle" and src.channels == 2 and src.sr == SR
    data = src.read(0, src.frames)
    assert src.frames >= 3200
    np.testing.assert_allclose(data[:, 0], np.arange(src.frames) / 100000, rtol=1e-5, atol=1e-7)
    assert np.allclose(data[:, 1], 0.25)


def test_recorder_pause_skips_audio():
    rec = Recorder(sr=SR, channels=1, stream_factory=FakeInput)
    rec.start()
    assert wait_for(lambda: rec.seconds > 0.1)
    rec.pause()
    time.sleep(0.05)
    frozen = rec.seconds
    time.sleep(0.15)
    assert rec.seconds == frozen and rec.state == "paused"
    rec.resume()
    assert wait_for(lambda: rec.seconds > frozen + 0.1)
    src = rec.stop()
    assert src.channels == 1
    left, right = rec.consume_peak()
    assert left == right                                    # mono reports the same level on both meters


def test_recorder_device_failure():
    def broken(**kw):
        raise OSError("busy")

    with pytest.raises(AudioDeviceError):
        Recorder(stream_factory=broken).start()
