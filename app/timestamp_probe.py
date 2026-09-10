"""Bounded ffprobe streaming: no per-frame JSON tree or unbounded stderr buffer."""

from collections import deque
from collections.abc import Callable
from queue import Empty, Full, Queue
import subprocess
from threading import Event, Thread
from time import monotonic

from app.ffmpeg_utils import MediaError, SUBPROCESS_CREATION_FLAGS, _check_executable
from app.models import TimestampScan


MAX_LINE_CHARS = 16384
QUEUE_LINES = 64


def scan_timestamp_lines(command: list[str], consume: Callable[[str], None], *, purpose: str,
                         timeout: float, max_records: int) -> TimestampScan:
    """Stop/reap even when the child hangs, the callback raises, or Ctrl+C arrives."""
    if timeout <= 0 or max_records <= 0:
        return TimestampScan(purpose, 0, False, 0, "分析预算已耗尽")
    _check_executable(command[0])
    started = monotonic()
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
            creationflags=SUBPROCESS_CREATION_FLAGS)
    except OSError as exc:
        raise MediaError(f"无法启动深度 ffprobe：{exc}") from exc
    stop = Event()
    lines: Queue[str | None] = Queue(maxsize=QUEUE_LINES)
    errors: deque[str] = deque(maxlen=16)
    reader_errors: deque[str] = deque(maxlen=2)

    def enqueue(line):
        while not stop.is_set():
            try:
                lines.put(line, timeout=0.1)
                return
            except Full:
                pass

    def read_stdout():
        try:
            while not stop.is_set():
                line = process.stdout.readline(MAX_LINE_CHARS + 1)
                if not line:
                    break
                enqueue(line)
        except (OSError, ValueError) as exc:
            reader_errors.append(f"stdout 读取失败：{exc}")
        finally:
            enqueue(None)

    def read_stderr():
        try:
            while not stop.is_set():
                line = process.stderr.readline(MAX_LINE_CHARS + 1)
                if not line:
                    break
                errors.append(line[-1000:].strip())
        except (OSError, ValueError) as exc:
            reader_errors.append(f"stderr 读取失败：{exc}")

    threads = [Thread(target=read_stdout, daemon=True), Thread(target=read_stderr, daemon=True)]
    count, reason, complete = 0, "", False
    try:
        try:
            for thread in threads:
                thread.start()
        except RuntimeError as exc:
            raise MediaError(f"无法启动深度分析读取线程：{exc}") from exc
        while True:
            remaining = timeout - (monotonic() - started)
            if remaining <= 0:
                reason = "达到分析时限，结果仅覆盖已扫描部分"
                break
            try:
                line = lines.get(timeout=min(0.1, remaining))
            except Empty:
                continue
            if line is None:
                try:
                    code = process.wait(timeout=max(0.001, timeout - (monotonic() - started)))
                except subprocess.TimeoutExpired:
                    reason = "ffprobe 结束等待超时"
                    break
                # A successful exit with decoding errors must not authorize compensation.
                threads[1].join(timeout=max(0, min(1, timeout - (monotonic() - started))))
                if threads[1].is_alive():
                    reason = "stderr 未完整读取，不能确认分析完成"
                    break
                complete = code == 0 and not errors and not reader_errors
                reason = "" if complete else f"ffprobe 错误（{code}）：" + "\n".join((*errors, *reader_errors))[-2000:]
                break
            # Read one item beyond the budget to distinguish EOF from truncation;
            # never send that extra record to the consumer.
            if count >= max_records:
                reason = "达到记录上限，结果仅覆盖已扫描部分"
                break
            if len(line) > MAX_LINE_CHARS:
                reason = "ffprobe 行长度超过安全上限"
                break
            count += 1
            consume(line)
    finally:
        stop.set()
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        for thread in threads:
            if thread.ident is not None:
                thread.join(timeout=5)
        process.stdout.close()
        process.stderr.close()
    return TimestampScan(purpose, count, complete, monotonic() - started, reason)
