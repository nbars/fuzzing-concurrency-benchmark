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
from runner.base import EvaluationRunner, AFLPP_VERSION
from logger import get_logger

log = get_logger()

@dataclass
class BuildArtifactInfo:
    bin_path: Path
    seed_dir: Path
    target_args: t.List[str]

class PlainRunner(EvaluationRunner):

    def __init__(self, job_cnt: int, timeout_s: int) -> None:
        super().__init__(job_cnt, timeout_s)
        self._target_dir = Path(__file__).parent / "binutils"
        self._build_dir = self._target_dir / "build"
        self._aflpp_src_dir = self._target_dir / "aflpp"
        self._fuzz_dir = self._target_dir / "fuzzing" / f"{job_cnt}jobs_{timeout_s}s"

    def get_build_artifact_info(self) -> BuildArtifactInfo:
        return BuildArtifactInfo(self._build_artifact_path(), self._seed_dir(), self._target_args())

    def _build_artifact_path(self) -> Path:
        return self._build_dir / "binutils/readelf"

    def _target_args(self) -> t.List[str]:
        return ["-a", "@@"]

    def _seed_dir(self) -> Path:
        return self._target_dir / "seeds"

    def build(self, purge: bool = False) -> None:
        log.info(f"Build dir is {self._build_dir}")
        if self._build_dir.exists() and not purge:
            log.info("Build dir already exists, and purge was not set. We are done.")
            return
        if self._build_dir.exists() and purge:
            log.info("Build dir already exists and is purged.")
            shutil.rmtree(self._build_dir.as_posix())

        self._build_dir.mkdir(parents=True)
        workdir = self._target_dir

        if not self._aflpp_src_dir.exists():
            subprocess.check_call(f"git clone https://github.com/AFLplusplus/AFLplusplus {self._aflpp_src_dir}", shell=True)
            subprocess.check_call(f"sudo apt install -y clang llvm lld && git checkout {AFLPP_VERSION} && make -j ", shell=True, cwd=self._aflpp_src_dir)


        binutils_src_dir = workdir / "binutils-gdb"
        if not binutils_src_dir.exists():
            subprocess.check_call(f"git clone git://sourceware.org/git/binutils-gdb.git {binutils_src_dir}", shell=True)

        subprocess.check_call("git checkout binutils-2_43", shell=True, cwd=binutils_src_dir)

        try:
            subprocess.check_call("sudo apt update && sudo apt-get build-dep -y binutils", shell=True)
        except subprocess.SubprocessError:
            log.error(f"Check the output above, likely deb-src entries are missing (sed -i \"s/^# deb-src/deb-src/g\" /etc/apt/sources.list).", exc_info=True)
            log.error("Your config might also be located in /etc/apt/sources.list.d/<...>")
            raise

        # Configure with as few deps as possible, such that we do not get sad if running on a different system.
        # This should produce redelf with only these deps:
        # linux-vdso.so.1 (0x00007ffcb6b9e000)
	    # libc.so.6 => /lib/x86_64-linux-gnu/libc.so.6 (0x00007f5819cf8000)
	    # /lib64/ld-linux-x86-64.so.2 (0x00007f581a325000)
        configure_cmd = f"""
        ../binutils-gdb/configure --prefix=/usr       \
                    --enable-ld=default \
                    --disable-plugins    \
                    --disable-shared     \
                    --disable-werror    \
                    --disable-gdbserver   \
                    --disable-gdb   \
                    --without-zstd \
                    --disable-lto \
                    --enable-compressed-debug-sections=none \
        """
        configure_cmd = textwrap.dedent(configure_cmd)
        env = os.environ.copy()
        #env["PATH"] = f"{aflpp_src_dir}:" + env["PATH"]
        env.update ({
            "CC": f"{self._aflpp_src_dir}/afl-clang-fast",
            "CXX": f"{self._aflpp_src_dir}/afl-clang-fast++",
            "CFLAGS": "-fuse-ld=lld",
            "CXXFLAGS": "-fuse-ld=lld",
            "LD": "/usr/bin/lld"
        })

        subprocess.check_call(f"cd {self._build_dir} && {configure_cmd} && make -j", shell=True, cwd=self._build_dir, env=env)
        assert self._build_artifact_path().exists(), self._build_artifact_path()

    def purge_previous_run(self):
         if self._fuzz_dir.exists():
            shutil.rmtree(self._fuzz_dir.as_posix())

    def start(self, purge: bool) -> None:
        subprocess.run("pkill afl-fuzz", shell=True, check=False)

        log.info(f"Results are going to be stored in {self._fuzz_dir}")
        if purge:
            self.purge_previous_run()
        elif self._fuzz_dir.exists():
            log.info(f"Skipping {self} since {self._fuzz_dir} already exists")
            return

        self._fuzz_dir.mkdir(parents=True)

        env = {
            "AFL_NO_UI": "1",
            # aflpp's affinity code is racy
            "AFL_NO_AFFINITY": "1",
        }

        free_cpus = list(range(multiprocessing.cpu_count()))
        jobs: t.List[subprocess.Popen] = [] # type: ignore
        for i in  range(self._job_cnt):
            workdir = self._fuzz_dir / f"job_{i}"
            workdir.mkdir(parents=True)
            output_log = (workdir / "output.txt").open("w")

            args = " ".join(self._target_args())
            cmd = ""
            # If we ran out of CPUs, we do not constrain the task
            if free_cpus:
                cpu_idx = free_cpus.pop(0)
                cmd = f"taskset -c {cpu_idx} "
            cmd += f"{self._aflpp_src_dir}/afl-fuzz -i {self._seed_dir()} -o {workdir} -- {self._build_artifact_path()} {args}"
            log.info(f"{cmd=}")

            job = subprocess.Popen(cmd, env=env, shell=True, cwd=workdir, stdin=subprocess.DEVNULL, stdout=output_log, stderr=subprocess.STDOUT)
            jobs.append(job)

        deadline = time.monotonic() + self._timeout_s
        while True:
            time.sleep(1)
            status = [j.poll() != None for j in jobs]
            if any(status):
                log.error(f"{len(status)} job(s) terminated prematurely.")
                for j in jobs:
                    log.error(f"Sending SIGINT to {j.pid}")
                    j.send_signal(signal.SIGTERM)
                raise RuntimeError("Some jobs seem to have terminated prematurely. OOM?")

            if time.monotonic() > deadline:
                log.info("Timeout exceeded, terminating jobs")
                for j in jobs:
                    log.info(f"Sending SIGINT to {j.pid}")
                    j.send_signal(signal.SIGTERM)
                    log.info(f"Waiting for {j.pid}")
                    j.wait()
                    log.info(f"Target {j.pid} terminate")
                break

    def stats_files_paths(self) -> t.List[Path]:
        return list(self._fuzz_dir.glob("*/*/fuzzer_stats"))
