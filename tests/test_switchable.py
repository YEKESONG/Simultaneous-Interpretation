"""运行中换音源（电脑播放的声音 ⇄ 麦克风）的测试。"""

import numpy as np
import pytest

from simul_interp.audio.base import CHUNK_SAMPLES, SAMPLE_RATE, AudioSource
from simul_interp.audio.switchable import SwitchableSource


class Source(AudioSource):
    """测试用的音源：start / stop 只记次数，音频由测试自己送；fail=True 时启动失败。"""

    def __init__(self, name, fail=False):
        super().__init__()
        self.name, self.fail = name, fail
        self.starts = self.stops = 0

    def start(self):
        if self.fail:
            raise RuntimeError(f"打不开{self.name}")
        self.starts += 1

    def stop(self):
        self.stops += 1
        self._finish()

    def say(self, level, seconds=0.5):
        self._feed(np.full(int(seconds * SAMPLE_RATE), level, np.float32))


def take(source, count):
    it = source.chunks()
    return [next(it) for _ in range(count)]


def test_switching_keeps_the_stream_time_and_the_recording_continuous():
    system, mic = Source("电脑播放的声音"), Source("麦克风")
    switchable = SwitchableSource(system)
    recorded = []
    switchable.on_chunk = recorded.append
    switchable.start()
    system.say(0.1)
    first = take(switchable, int(0.5 * SAMPLE_RATE) // CHUNK_SAMPLES)
    switchable.switch(mic, restore=lambda: Source("电脑播放的声音"))
    assert system.stops == 1 and mic.starts == 1 and switchable.name == "麦克风"
    system.say(0.9)  # 换下来的音源最后又送来几块：不要了
    mic.say(0.2)
    second = take(switchable, int(0.5 * SAMPLE_RATE) // CHUNK_SAMPLES)
    assert all(np.allclose(c.samples, 0.1) for c in first) and all(np.allclose(c.samples, 0.2) for c in second)
    assert second[0].start == first[-1].end  # 时间接着往下走：录音和记录里的位置不会错开
    assert len(recorded) == len(first) + len(second)  # 录音还是同一个文件，一块不少
    switchable.stop()
    assert mic.stops == 1


def test_input_that_cannot_be_opened_falls_back_to_the_previous_kind():
    system = Source("电脑播放的声音")
    switchable = SwitchableSource(system)
    switchable.start()
    restored = Source("电脑播放的声音（重新打开）")
    with pytest.raises(RuntimeError, match="打不开麦克风"):
        switchable.switch(Source("麦克风", fail=True), restore=lambda: restored)
    assert restored.starts == 1 and switchable.name == "电脑播放的声音（重新打开）"  # 同传不中断
    restored.say(0.3)
    assert np.allclose(take(switchable, 1)[0].samples, 0.3)
    switchable.stop()


def test_switching_before_the_start_just_replaces_the_input():
    system, mic = Source("电脑播放的声音"), Source("麦克风")
    switchable = SwitchableSource(system)
    switchable.switch(mic, restore=lambda: system)  # 模型还在加载，音源还没启动
    assert system.starts == system.stops == 0 and mic.starts == 0
    switchable.start()
    assert mic.starts == 1 and system.starts == 0


def test_input_that_ends_by_itself_ends_the_stream():
    system = Source("电脑播放的声音")
    switchable = SwitchableSource(system)
    switchable.start()
    system.say(0.1, seconds=CHUNK_SAMPLES / SAMPLE_RATE)
    system._finish()  # 比如内录小程序反复崩溃、放弃了
    assert len(list(switchable.chunks())) == 1  # 流水线的循环能结束


def test_pause_applies_to_whatever_input_is_in_use():
    system, mic = Source("电脑播放的声音"), Source("麦克风")
    switchable = SwitchableSource(system)
    switchable.start()
    switchable.pause(flush_s=0)
    switchable.switch(mic, restore=lambda: system)
    mic.say(0.5)
    assert switchable._queue.empty() and switchable.position == 0  # 暂停中：换了音源也不收
    switchable.resume()
    mic.say(0.5, seconds=CHUNK_SAMPLES / SAMPLE_RATE)
    assert np.allclose(take(switchable, 1)[0].samples, 0.5)
    switchable.stop()
