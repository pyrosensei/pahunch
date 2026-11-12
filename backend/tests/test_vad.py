from dataclasses import replace

import pytest

from app.config import Settings
from app.realtime import RealtimeConfig, UtteranceBuffer, build_realtime_config
from scripts.vad_probe import FRAME_MS, RATE, frame


def feed(buffer, rms, ms, *, noise=False):
    starts = []
    for index in range(ms // FRAME_MS):
        if buffer.add(frame(rms, noise=noise, index=index)):
            starts.append((index + 1) * FRAME_MS)
    return starts


def feed_processing(buffer, rms, ms, *, noise=False, offset=0):
    starts = []
    for index in range(ms // FRAME_MS):
        if buffer.add(
            frame(rms, noise=noise, index=index),
            calibrate=False,
            start_ms=160,
            minimum_threshold=500,
            quiet_start_ms=400,
        ):
            starts.append(offset + (index + 1) * FRAME_MS)
    return starts


def calibrated(noise_rms=20, **kwargs):
    buffer = UtteranceBuffer(RealtimeConfig(**kwargs))
    assert not feed(buffer, noise_rms, 500, noise=True)
    return buffer


def test_quiet_speech_is_detected_after_noise_calibration():
    buffer = calibrated()
    assert feed(buffer, 220, 800) == [100]
    assert buffer.has_utterance
    assert 100 <= buffer.threshold < 220
    assert feed(buffer, 20, 600, noise=True) == []
    assert buffer.complete


@pytest.mark.parametrize("noise_rms", [20, 200, 800, 3000])
def test_steady_noise_does_not_start_utterance(noise_rms):
    buffer = UtteranceBuffer(RealtimeConfig())
    assert feed(buffer, noise_rms, 5000, noise=True) == []
    assert not buffer.has_utterance
    assert len(buffer.pcm) == 0


def test_extreme_noise_fails_closed_at_threshold_bound():
    buffer = UtteranceBuffer(RealtimeConfig())
    assert feed(buffer, 10000, 3000) == []
    assert buffer.threshold <= 12000
    assert buffer.noise_limited


def test_noise_adapts_to_slow_room_change_and_freezes_during_speech():
    buffer = calibrated(50)
    initial = buffer.noise_floor
    for level in range(50, 251, 10):
        assert feed(buffer, level, 200, noise=True) == []
    assert buffer.noise_floor > initial * 2
    floor = buffer.noise_floor
    feed(buffer, 3000, 2000)
    assert buffer.has_utterance
    assert buffer.noise_floor == floor


def test_playback_frames_do_not_calibrate_or_adapt_noise_floor():
    buffer = calibrated()
    floor = buffer.noise_floor
    for _ in range(20):
        buffer.add(frame(30), calibrate=False, start_ms=160)
    assert buffer.noise_floor == floor


def test_short_spike_does_not_start_speech():
    buffer = calibrated()
    assert feed(buffer, 3000, 20) == []
    assert feed(buffer, 20, 800, noise=True) == []
    assert not buffer.has_utterance
    assert len(buffer.pcm) == 0


def test_processing_onset_ignores_a_short_loud_burst():
    buffer = calibrated()
    assert feed_processing(buffer, 3000, 100) == []
    assert not buffer.has_utterance
    assert feed_processing(buffer, 20, 200, noise=True) == []
    assert not buffer.has_utterance


def test_processing_onset_confirms_quiet_speech_after_longer_window():
    buffer = calibrated()
    assert feed_processing(buffer, 220, 380) == []
    assert feed_processing(buffer, 220, 20, offset=380) == [400]
    assert buffer.has_utterance
    assert buffer.onset_mode == "quiet"
    assert buffer._speech_threshold == buffer.threshold


def test_processing_onset_still_accepts_strong_speech_quickly():
    buffer = calibrated()
    assert feed_processing(buffer, 3000, 140) == []
    assert feed_processing(buffer, 3000, 20, offset=140) == [160]
    assert buffer._saw_speech
    assert buffer.speech_ms == 160
    assert buffer.onset_mode == "strong"


def test_pre_roll_preserves_first_syllable_and_is_bounded():
    buffer = calibrated()
    expected = b"".join(frame(1600, index=i) for i in range(5))
    for i in range(5):
        started = buffer.add(frame(1600, index=i))
    assert started
    assert bytes(buffer.pcm).endswith(expected)
    assert len(buffer.pcm) == (200 + 100) * RATE * 2 // 1000
    buffer.reset()
    feed(buffer, 20, 30000, noise=True)
    assert len(buffer.pcm) == 0
    assert len(buffer.pre_roll) <= 360 * RATE * 2 // 1000


def test_continuous_speech_beyond_fifteen_seconds_waits_for_silence():
    buffer = calibrated(max_utterance_ms=1000)
    feed(buffer, 1600, 20000)
    assert not buffer.complete
    assert buffer.speech_ms == 20000
    assert len(buffer.pcm) > 20 * RATE * 2
    assert buffer.pcm.file._rolled  # PCM leaves memory, not the conversation.
    feed(buffer, 20, 600)
    assert buffer.end_reason == "SILENCE"
    assert bytes(buffer.pcm).endswith(frame(20) * 30)
    buffer.pcm.close()


def test_large_frame_is_preserved_without_forcing_an_endpoint():
    buffer = calibrated(max_utterance_ms=1000)
    feed(buffer, 1600, 300)
    before = bytes(buffer.pcm)
    extra = frame(1600) * 100
    buffer.add(extra)
    assert not buffer.complete
    assert bytes(buffer.pcm) == before + extra
    assert buffer.pcm.file._rolled
    buffer.reset()
    assert len(buffer.pcm) == 0
    buffer.pcm.close()


def test_reset_keeps_calibration_but_discards_old_pre_roll():
    buffer = calibrated(800)
    floor = buffer.noise_floor
    feed(buffer, 4000, 300)
    buffer.reset()
    assert not buffer.calibrating
    assert buffer.noise_floor == floor
    assert buffer.pre_roll == b""
    assert not buffer.has_utterance


def test_fixed_threshold_mode_remains_explicitly_testable():
    buffer = UtteranceBuffer(RealtimeConfig(adaptive_vad=False, pre_roll_ms=0))
    assert not feed(buffer, 220, 500)
    assert feed(buffer, 1600, 300) == [100]
    assert buffer.threshold == 500


@pytest.mark.parametrize(
    "field,value",
    [
        ("vad_floor", 0),
        ("vad_ceiling", 100),
        ("noise_ratio", 1),
        ("speech_start_ms", 0),
        ("barge_in_ms", 0),
        ("processing_confirm_ms", 0),
        ("processing_confirm_ms", 100),
        ("pre_roll_ms", -1),
        ("no_speech_ms", 0),
    ],
)
def test_invalid_detector_configuration_is_rejected(field, value):
    with pytest.raises(ValueError):
        replace(RealtimeConfig(), **{field: value})


def test_environment_settings_reach_realtime_detector():
    settings = Settings(
        _env_file=None,
        realtime_adaptive_vad=False,
        realtime_vad_floor=150,
        realtime_vad_ceiling=9000,
        realtime_noise_ratio=2.5,
        realtime_calibration_ms=400,
        realtime_speech_start_ms=120,
        realtime_barge_in_ms=200,
        realtime_processing_confirm_ms=500,
        realtime_pre_roll_ms=240,
        realtime_no_speech_ms=6000,
    )
    config = build_realtime_config(settings)
    assert not config.adaptive_vad
    assert (config.vad_floor, config.vad_ceiling, config.noise_ratio) == (150, 9000, 2.5)
    assert (config.calibration_ms, config.speech_start_ms, config.barge_in_ms) == (400, 120, 200)
    assert (config.processing_confirm_ms, config.pre_roll_ms, config.no_speech_ms) == (500, 240, 6000)


def test_audio_spool_copy_never_uses_unbounded_reads():
    from app.realtime import _AudioSpool

    source, target = _AudioSpool(16), _AudioSpool(16)
    content = b"abcd" * 100_000
    source.extend(content)
    original_read = source.file.read
    sizes = []

    def bounded_read(size=-1):
        assert 0 <= size <= 64 * 1024
        sizes.append(size)
        return original_read(size)

    source.file.read = bounded_read
    try:
        source.copy_to(target)
        assert bytes(target) == content
        assert len(sizes) > 1
    finally:
        source.close()
        target.close()


def test_audio_spool_oversized_materialization_fails_before_read(monkeypatch):
    from app import realtime
    from app.errors import DomainError

    spool = realtime._AudioSpool(16)
    spool.extend(b"abcd" * 100)
    monkeypatch.setattr(realtime, "MAX_AUDIO_BYTES", 100)
    def forbidden_read(*args):
        pytest.fail("Oversized recording must not be read or silently truncated")
    spool.file.read = forbidden_read
    try:
        with pytest.raises(DomainError) as error:
            bytes(spool)
        assert error.value.status_code == 413
    finally:
        spool.close()
