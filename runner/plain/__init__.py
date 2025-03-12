import signal
import subprocess
import time
import multiprocessing
import typing as t
from pathlib import Path
from builder import BuildArtifact
from builder.aflpp import AflConfig
from runner.base import EvaluationRunner
from logger import get_logger
import util

log = get_logger()


class AflRunnerBase(EvaluationRunner):

    def __init__(
        self,
        target: BuildArtifact,
        afl_config: AflConfig,
        job_cnt: int,
        timeout_s: int,
        custom_attrs: t.Optional[t.Dict[str, str]],
        without_pinning: bool = False,
        with_turbo: bool = False,
    ) -> None:
        self._without_pinning = without_pinning
        self._with_turbo = with_turbo
        super().__init__(target, afl_config, job_cnt, timeout_s, custom_attrs)

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

        if self._with_turbo:
            util.set_turbo(True)

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
                if not self._without_pinning:
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
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    log.info("Waiting for afl processes to terminate")
                    time.sleep(5)
                    out = subprocess.check_output(
                        "pgrep afl-fuzz || true", shell=True, encoding="utf8"
                    ).strip()
                    if not out:
                        break
                else:
                    raise RuntimeError(
                        "Failed to terminate remaining afl-fuzz processes"
                    )
                break

    def stats_files_paths(self) -> t.List[Path]:
        return list(self.work_dir().glob("*/*/fuzzer_stats"))


class AflRunner(AflRunnerBase):

    def __init__(
        self,
        target: BuildArtifact,
        afl_config: AflConfig,
        job_cnt: int,
        timeout_s: int,
        custom_attrs: t.Optional[t.Dict[str, str]],
    ) -> None:
        super().__init__(
            target, afl_config, job_cnt, timeout_s, custom_attrs, without_pinning=False
        )


class AflRunnerWithoutPin(AflRunnerBase):

    def __init__(
        self,
        target: BuildArtifact,
        afl_config: AflConfig,
        job_cnt: int,
        timeout_s: int,
        custom_attrs: t.Optional[t.Dict[str, str]],
    ) -> None:
        super().__init__(
            target, afl_config, job_cnt, timeout_s, custom_attrs, without_pinning=True
        )


class AflRunnerTurbo(AflRunnerBase):

    def __init__(
        self,
        target: BuildArtifact,
        afl_config: AflConfig,
        job_cnt: int,
        timeout_s: int,
        custom_attrs: t.Optional[t.Dict[str, str]],
    ) -> None:
        super().__init__(
            target,
            afl_config,
            job_cnt,
            timeout_s,
            custom_attrs,
            without_pinning=False,
            with_turbo=True,
        )


class AflRunnerWithoutPinTurbo(AflRunnerBase):

    def __init__(
        self,
        target: BuildArtifact,
        afl_config: AflConfig,
        job_cnt: int,
        timeout_s: int,
        custom_attrs: t.Optional[t.Dict[str, str]],
    ) -> None:
        super().__init__(
            target,
            afl_config,
            job_cnt,
            timeout_s,
            custom_attrs,
            without_pinning=True,
            with_turbo=True,
        )


def all() -> t.List[t.Type[AflRunnerBase]]:
    ret = []
    for _k, v in globals().items():
        if isinstance(v, type) and issubclass(v, AflRunnerBase) and v != AflRunnerBase:
            ret.append(v)
    return ret
