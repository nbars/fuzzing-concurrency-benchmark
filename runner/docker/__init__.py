from collections import defaultdict
import os
import shutil
import itertools
import signal
import subprocess
import textwrap
import time
import multiprocessing
import math
import typing as t
from dataclasses import dataclass
from pathlib import Path
from builder import BuildArtifact
from builder.aflpp import AflConfig
from runner.base import EvaluationRunner
from logger import get_logger

log = get_logger()


class DockerRunner(EvaluationRunner):

    def __init__(
        self,
        target: BuildArtifact,
        afl_config: AflConfig,
        job_cnt: int,
        timeout_s: int,
        with_overlayfs: bool = True,
        num_proccesses_containers: int = 1,
    ) -> None:
        super().__init__(target, afl_config, job_cnt, timeout_s)
        self._image_name = (
            str(self)
            .replace(":", "_")
            .replace(",", "_")
            .replace("=", "_")
            .replace(".", "_")
            .lower()
        )
        self._with_overlayfs = with_overlayfs
        self._num_processes_per_container = num_proccesses_containers
        self._spawned_container_ids: t.Optional[t.List[str]] = None
        self._contrainer_root = Path("/work")
        self._container_afl_config = AflConfig(self._contrainer_root / "aflpp")
        self._container_target = self.target().with_new_root(self._contrainer_root)
        assert with_overlayfs
        assert num_proccesses_containers == 1

    def prepare(self, purge: bool = False) -> bool:
        cmd = "echo core | sudo tee /proc/sys/kernel/core_pattern"
        log.info(f"Running {cmd}")
        subprocess.check_call(cmd, shell=True)
        if not super().prepare(purge):
            return True

        # Build the docke image
        work_dir = self.work_dir()

        # Copy afl++
        shutil.copytree(self.afl_config().root(), work_dir / "aflpp")

        # Copy seeds + binary
        shutil.copy(self.target().bin_path, work_dir)
        shutil.copytree(self.target().seed_dir, work_dir / self.target().seed_dir.name)

        docker_file = f"""
        FROM ubuntu:22.04
        ENV DEBIAN_FRONTEND "noninteractive"

        RUN apt update -y
        RUN apt install -y clang llvm lld

        RUN mkdir {self._contrainer_root}
        COPY ./ {self._contrainer_root}

        """
        docker_file = textwrap.dedent(docker_file)
        (work_dir / "Dockerfile").write_text(docker_file)

        subprocess.check_call(
            f"docker build -t {self._image_name} .", shell=True, cwd=self.work_dir()
        )

        return True

    def start(self) -> None:
        log.info(f"Results are going to be stored in {self.work_dir()}")

        out = subprocess.run(
            f"docker ps | grep {self._image_name}",
            shell=True,
            encoding="utf8",
            check=False,
        )
        if out.returncode != 1:
            raise RuntimeError(
                f"Looks like there are Docker containers from another run: {out}"
            )

        out = subprocess.check_output(
            "pgrep afl-fuzz || true", shell=True, encoding="utf8"
        ).strip()
        if out:
            raise RuntimeError(
                f"Looks like other afl-fuzz processes are running: {out}"
            )

        # First, start all docker containers we are going to need
        max_container_cnt = math.ceil(self._job_cnt / self._num_processes_per_container)

        free_cpus = list(range(multiprocessing.cpu_count()))
        self._spawned_container_ids = []
        for i in range(max_container_cnt):
            # TODO: Overlayfs
            cpus = []
            try:
                cpus = [
                    str(free_cpus.pop(0))
                    for _ in range(self._num_processes_per_container)  # inclusive
                ]
            except:
                # We ran out of cpus
                assert not free_cpus

            cpus_flag = ""
            if cpus:
                cpus_flag = ",".join(cpus)
                cpus_flag = f"--cpuset-cpus={cpus_flag}"

            cmd = f"docker run -d {cpus_flag} -t {self._image_name} bash"
            log.info(f"Spawning container: {cmd}")
            output = subprocess.check_output(
                cmd,
                shell=True,
                encoding="utf8",
            ).strip()
            self._spawned_container_ids.append(output)

        def stop_all_container():
            assert self._spawned_container_ids
            for c in self._spawned_container_ids:
                subprocess.run(f"docker kill {c}", shell=True, check=False)

        env = {
            "AFL_NO_UI": "1",
            # aflpp's affinity code is racy and we are in contains
            "AFL_NO_AFFINITY": "1",
        }
        env_args = [["-e", f"{arg}={value}"] for arg, value in env.items()]
        env_args = " ".join(itertools.chain.from_iterable(env_args))

        container_id_to_job_ids = defaultdict(list)
        jobs: t.List[subprocess.Popen] = []  # type: ignore
        for job_idx in range(self._job_cnt):
            container = self._spawned_container_ids[
                job_idx % len(self._spawned_container_ids)
            ]
            local_instance_dir = self.work_dir() / f"{job_idx}"
            local_instance_dir.mkdir(parents=True, exist_ok=True)
            local_log_file = local_instance_dir / "log.txt"
            local_log_file_fd = local_log_file.open("w")
            container_instance_dir = f"{self._contrainer_root.as_posix()}/{job_idx}"
            target_args = " ".join(self._container_target.args)
            afl_cmd = f"{self._container_afl_config.afl_fuzz()} -i {self._container_target.seed_dir} -o {container_instance_dir} -- {self._container_target.bin_path} {target_args}"
            cmd = f"docker exec {env_args} -t {container} {afl_cmd}"
            log.info(f"Spawning process in container: {cmd}")
            j = subprocess.Popen(
                cmd, shell=True, stdout=local_log_file_fd, stderr=subprocess.STDOUT
            )
            container_id_to_job_ids[container].append(job_idx)
            jobs.append(j)

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
                stop_all_container()
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
                stop_all_container()
                break

        for c in self._spawned_container_ids:
            for job_id in container_id_to_job_ids[c]:
                subprocess.run(
                    f"docker cp {c}:{self._contrainer_root}/{job_id}/default/fuzzer_stats {self._work_dir}/{job_id}_fuzzer_stats",
                    shell=True,
                )
                subprocess.check_call(f"docker rm -f {c}", shell=True)

    def stats_files_paths(self) -> t.List[Path]:
        return list(self.work_dir().glob("*fuzzer_stats"))

    def purge(self):
        super().purge()
        if self._spawned_container_ids:
            for c in self._spawned_container_ids:
                subprocess.run(
                    f"docker rm -f {c}",
                    shell=True,
                    check=False,
                    stderr=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                )


class DefaultDockerRunner(DockerRunner):
    """
    Runner without any special setting such as disabled overlayfs etc.
    """

    pass
