import subprocess
import sys
import threading
from typing import List, Optional


class TeePopen:
    def __init__(
        self,
        args,
        *,
        stdin=None,
        stdout=sys.stdout,
        stderr=sys.stderr,
        cwd=None,
        env=None,
        text=True,
        bufsize=1,
        output_prefix="",
        log_file=None,
    ):
        self.args = args
        self.stdin = stdin
        self.cwd = cwd
        self.env = env
        self.text = text
        self.bufsize = bufsize
        self.output_prefix = output_prefix

        self._proc: Optional[subprocess.Popen] = None
        self._stdout_buf: List[str] = []
        self._stderr_buf: List[str] = []
        self._threads: List[threading.Thread] = []
        self._stdout = stdout
        self._stderr = stderr
        self._log_file = log_file

    def _reader(self, pipe, buf, target_stream):
        try:
            for line in pipe:
                if self.output_prefix:
                    line = f"{self.output_prefix}{line}"
                if target_stream:
                    target_stream.write(line)
                    target_stream.flush()
                if self._log_file:
                    self._log_file.write(line)
                buf.append(line)
        finally:
            if self._log_file:
                self._log_file.close()
            pipe.close()

    def __del__(self):
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
        if self._log_file:
            self._log_file.close()

    def start(self):
        self._proc = subprocess.Popen(
            self.args,
            stdin=self.stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.cwd,
            env=self.env,
            text=self.text,
            bufsize=self.bufsize,
        )

        t_out = threading.Thread(
            target=self._reader,
            args=(self._proc.stdout, self._stdout_buf, self._stdout),
            daemon=True,
        )
        t_err = threading.Thread(
            target=self._reader,
            args=(self._proc.stderr, self._stderr_buf, self._stderr),
            daemon=True,
        )

        t_out.start()
        t_err.start()

        self._threads.extend([t_out, t_err])

        return self

    def wait(self):
        if self._proc is None:
            raise RuntimeError("Process not started")

        returncode = self._proc.wait()

        for t in self._threads:
            t.join()
        if self._log_file:
            self._log_file.close()
        return returncode

    def run(self):
        self.start()
        return self.wait()

    @property
    def stdout(self) -> str:
        return "".join(self._stdout_buf)

    @property
    def stderr(self) -> str:
        return "".join(self._stderr_buf)

    @property
    def returncode(self) -> Optional[int]:
        return self._proc.returncode if self._proc else None
