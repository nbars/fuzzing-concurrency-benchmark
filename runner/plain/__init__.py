import os
import shutil
import signal
import subprocess
import textwrap
import time
import multiprocessing
import typing as t
from dataclasses import dataclass
from pathlib import Path
from builder import BuildArtifact
from builder.aflpp import AflConfig
from runner.base import EvaluationRunner
from logger import get_logger

log = get_logger()


class DefaultAflRunner(EvaluationRunner):

    def __init__(
        self, target: BuildArtifact, afl_config: AflConfig, job_cnt: int, timeout_s: int
    ) -> None:
        super().__init__(target, afl_config, job_cnt, timeout_s)

    def prepare(self, purge: bool = False) -> bool:
        cmd = "echo core | sudo tee /proc/sys/kernel/core_pattern"
        log.info(f"Running {cmd}")
        subprocess.check_call(cmd, shell=True)
        if not super().prepare(purge):
            return True
        return True

    def start(self) -> None:
        log.info(f"Results are going to be stored in {self.work_dir()}")

        out = subprocess.check_output(
            "pgrep afl-fuzz || true", shell=True, encoding="utf8"
        ).strip()
        if out:
            raise RuntimeError(
                f"Looks like other afl-fuzz processes are running: {out}"
            )

        env = {
            "AFL_NO_UI": "1",
            # aflpp's affinity code is racy
            "AFL_NO_AFFINITY": "1",
        }

        free_cpus = list(range(multiprocessing.cpu_count()))
        jobs: t.List[subprocess.Popen] = []  # type: ignore
        for i in range(self._job_cnt):
            instance_dir = self.work_dir() / f"job_{i}"
            instance_dir.mkdir(parents=True)
            output_log = (instance_dir / "output.txt").open("w")

            args = " ".join(self.target().args)
            cmd = ""
            # If we ran out of CPUs, we do not constrain the task
            if free_cpus:
                cpu_idx = free_cpus.pop(0)
                cmd = f"taskset -c {cpu_idx} "
            cmd += f"{self.afl_config().afl_fuzz()} -i {self.target().seed_dir} -o {instance_dir} -- {self.target().bin_path} {args}"
            log.info(f"{cmd=}")

            job = subprocess.Popen(
                cmd,
                env=env,
                shell=True,
                cwd=instance_dir,
                stdin=subprocess.DEVNULL,
                stdout=output_log,
                stderr=subprocess.STDOUT,
            )
            jobs.append(job)

        deadline = time.monotonic() + self._timeout_s
        while True:
            time.sleep(1)
            status = [j.poll() != None for j in jobs]
            if any(status):
                log.error(f"{len(status)} job(s) terminated prematurely.")
                for j in jobs:
                    log.error(f"Sending SIGTERM to {j.pid}")
                    j.send_signal(signal.SIGTERM)
                    log.error(f"Waiting for {j.pid}")
                    j.wait()
                # afl++ sometimes fails to kill their childs :)
                subprocess.run("pkill -9 afl-fuzz", shell=True, check=False)
                raise RuntimeError(
                    f"Some jobs seem to have terminated prematurely. OOM? Check the logs at {self.work_dir()} for details"
                )

            if time.monotonic() > deadline:
                log.info("Timeout exceeded, terminating jobs")
                for j in jobs:
                    log.info(f"Sending SIGTERM to {j.pid}")
                    j.send_signal(signal.SIGTERM)
                    log.info(f"Waiting for {j.pid}")
                    j.wait()
                    log.info(f"Target {j.pid} terminate")
                # afl++ sometimes fails to kill their childs :)
                subprocess.run("pkill -9 afl-fuzz", shell=True, check=False)
                break

    def stats_files_paths(self) -> t.List[Path]:
        return list(self.work_dir().glob("*/*/fuzzer_stats"))
