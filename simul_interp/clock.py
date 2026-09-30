"""整个程序只用这一个时钟：各处的时间戳要互相相减，必须来自同一个时钟。

用 time.perf_counter 而不是 time.monotonic：两者都不会倒退，但 Python 3.12 及以前在 Windows 上
monotonic 的精度只有 15.6 毫秒（CI 上测出两个本该有先后的时间戳完全相等），
perf_counter 在 Windows 上用 QueryPerformanceCounter，精度远高于 1 毫秒。
"""

import time

now = time.perf_counter
