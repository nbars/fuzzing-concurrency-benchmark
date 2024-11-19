#!/usr/bin/env python3

import subprocess
import time
import typing as t
import argparse
import shutil
import re
import random
import enum
from abc import ABC, abstractmethod
from builder import BuildArtifact
from builder.aflpp import AflConfig, AflppBuilder
from builder.readelf import ReadelfBuilder
from logger import get_logger, setup_root_logger
from pathlib import Path

from runner.base import EvaluationRunner
from runner.plain import AflRunnerBase

setup_root_logger()
log = get_logger()


def range_limited_int(lower_limit: int = 1) -> t.Callable[[str], int]:
    def check(val: str):
        r = int(val)
        assert r >= lower_limit
        return r

    return check


def parse_time_as_s(val: str) -> int:
    m = re.match(r"^(?P<fac>[1-9][0-9]*)(?P<suffix>s|m|d)$", val)
    assert m
    suffix_to_scalar = {
        "s": 1,
        "m": 60,
        "h": 3600,
    }
    return suffix_to_scalar[m.group("suffix")] * int(m.group("fac"))  # type: ignore


def build_aflpp() -> AflConfig:
    # Build AFL and its compiler
    afl_builder = AflppBuilder()
    afl_builder.build(False)
    return afl_builder.afl_config()


def build_targets(afl_config: AflConfig) -> list[BuildArtifact]:
    # Build all targets locally
    target_builder = [
        ReadelfBuilder(afl_config.afl_cc(), afl_config.afl_cxx()),
    ]

    targets_artifacts: t.List[BuildArtifact] = []
    for builder in target_builder:
        try:
            builder.build(False)
            targets_artifacts.append(builder.build_artifact())
        except:
            builder.set_purge_on_next_build_flag()
            raise RuntimeError(
                f"Error while building {builder.target_name()}. The work directory if the target will be automatically purged on next build."
            )
    return targets_artifacts


def compute_total_runtime(
    timeout_s: int,
    job_cnt_configurations: t.List[int],
    enabled_runner_types: t.List[t.Type[EvaluationRunner]],
    targets_artifacts: t.List[BuildArtifact],
) -> str:
    total_runtime = (
        timeout_s
        * len(job_cnt_configurations)
        * len(enabled_runner_types)
        * len(targets_artifacts)
    )
    if total_runtime > 3600:
        total_runtime = f"{total_runtime/3600:.1f} hours"
    else:
        total_runtime = f"{total_runtime/60} minutes"
    return total_runtime


def prepare_runners(
    timeout_s: int,
    job_cnt_configurations: t.List[int],
    enabled_runner_types: t.List[t.Type[EvaluationRunner]],
    afl_config: AflConfig,
    targets_artifacts: t.List[BuildArtifact],
) -> list[EvaluationRunner]:
    runners: t.List[EvaluationRunner] = []
    for runner_type in enabled_runner_types:
        for target in targets_artifacts:
            for job_cnt in job_cnt_configurations:
                runner = runner_type(target, afl_config, job_cnt, timeout_s)
                log.info(f"Preparing runner {runner}")
                try:
                    runner.prepare(purge=True)
                except:
                    runner.set_purge_on_next_prepare_flag()
                    raise RuntimeError(
                        f"Failed to prepare runner {runner}. Retry on next prepare."
                    )
                runners.append(runner)
    return runners


def order_for_fast_exploration(data: t.List[int]) -> t.List[int]:
    assert len(data) >= 3
    data = sorted(data.copy())
    result = [data[0], data[-1], data[len(data) // 2]]
    queue = [(0, len(data) // 2), (len(data) // 2, len(data) - 1)]

    while queue:
        start, end = queue.pop(0)
        if end - start > 1:
            mid = (start + end) // 2
            result.append(data[mid])
            queue.append((start, mid))
            queue.append((mid, end))

    return result


def main():

    main_parser = argparse.ArgumentParser(
        prog="eval",
        description="Benchmark suit to evaluate the influence of different abstraction mechanisms on fuzzing throughput.",
    )

    main_parser.add_argument(
        "--min-concurrent-jobs",
        type=range_limited_int(),
        default=0,
        help="The minimum number of jobs the test series is started with.",
    )
    main_parser.add_argument(
        "--concurrent-jobs-step",
        type=range_limited_int(),
        default=8,
        help="Steps in that the value is increment start at min-concurrent-jobs to max-concurrent-jobs.",
    )
    main_parser.add_argument(
        "--additional-jobs-step",
        type=set,
        default=set([52, 52 * 2]),
        help="",  # 52, 104
    )
    main_parser.add_argument(
        "--max-concurrent-jobs",
        type=range_limited_int(),
        default=120,
        help="The maximum number of jobs the test series is executed for.",
    )
    main_parser.add_argument(
        "--storage",
        type=Path,
        default=Path("storage"),
        help="The Folder to store results in.",
    )
    main_parser.add_argument(
        "--step-timeout",
        type=parse_time_as_s,
        default="30m",
        help="The minimum number of jobs the test series is started with.",
    )
    main_parser.add_argument(
        "--build-only",
        type=bool,
        default=False,
        help="Only build the target without performing any experiment.",
    )

    from runner import docker, plain, vm

    class Runners(enum.Enum):
        DEFAULT_AFL_RUNNER = AflRunnerBase

        def __str__(self) -> str:
            return str(self.name)

    main_parser.add_argument(
        "--runners",
        type=lambda e: Runners(e).value,
        choices=list(Runners),
        nargs="+",
        default=[r.value for r in Runners],
        help="The runner that should be executed.",
    )

    args = main_parser.parse_args()
    build_only: bool = args.build_only
    timeout_s = args.step_timeout
    storage_path = Path(args.storage)
    assert args.min_concurrent_jobs <= args.max_concurrent_jobs
    storage_path.mkdir(parents=True, exist_ok=True)

    # Right bound inclusive
    additional_jobs_step: t.Set[int] = args.additional_jobs_step
    job_cnt_configurations_set = set(
        range(
            args.min_concurrent_jobs,
            args.max_concurrent_jobs + 1,
            args.concurrent_jobs_step,
        )
    )

    # args.min_concurrent_jobs can be set to zero to enabled generation number that
    # are always even (in regards to concurrent_jobs_step).
    if 0 in job_cnt_configurations_set:
        job_cnt_configurations_set.remove(0)
        job_cnt_configurations_set.add(1)

    job_cnt_configurations_set = additional_jobs_step | job_cnt_configurations_set
    job_cnt_configurations = list(job_cnt_configurations_set)
    if len(job_cnt_configurations) >= 3:
        job_cnt_configurations = order_for_fast_exploration(job_cnt_configurations)

    if job_cnt_configurations[-1] != args.max_concurrent_jobs:
        log.warning(
            f"Current steps value will cause the final concurrent value not being the specified max-concurrent-jobs value. Last value evaluated is {job_cnt_configurations[-1]}"
        )
    log.info(
        f"We are going to perform {len(job_cnt_configurations)} different configurations with a timeout of {timeout_s} seconds"
    )

    subprocess.check_call(
        "echo performance | sudo tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor",
        shell=True,
    )
    subprocess.check_call(
        "echo 1 | sudo tee /sys/devices/system/cpu/intel_pstate/no_turbo",
        shell=True,
    )

    # enabled_runner_types: list[type[EvaluationRunner]] = [rty for rty in args.runners]
    enabled_runner_types: list[type[EvaluationRunner]] = [
        docker.DockerRunnerBase,
        docker.DockerRunnerNoOverlay,
        docker.DockerRunnerNoOverlayPriv,
        docker.DockerRunnerNoOverlayNoPidNs,
        docker.DockerRunnerNoOverlayNoCgroups,
        docker.DockerRunnerNoOverlayPrivNoSeccompNoApparmoreAllCaps,
        docker.DockerRunnerNoOverlayPrivNoSeccompNoApparmoreAllCapsNoNs,
        docker.DockerRunnerNoOverlayPrivNoSeccompNoApparmoreAllCapsNoNsNoCgroup,
        docker.DockerRunnerSingleContainer,
        docker.DockerRunnerSingleContainerNoOverlay,
    ]

    afl_config = build_aflpp()
    targets_artifacts = build_targets(afl_config)

    total_runtime = compute_total_runtime(
        timeout_s, job_cnt_configurations, enabled_runner_types, targets_artifacts
    )
    log.info(f"This campaign will take {total_runtime} in total")
    time.sleep(1)

    runners = prepare_runners(
        timeout_s,
        job_cnt_configurations,
        enabled_runner_types,
        afl_config,
        targets_artifacts,
    )

    if build_only:
        return

    for runner in runners:
        log.info(f"Next runner is {runner}")

        runner_name = str(runner)
        job_storage = storage_path / f"{runner_name}"
        stats_storage = job_storage / "stats_files"
        if job_storage.exists():
            log.info(
                f"Results for current configuration are already at {job_storage}, skipping..."
            )
            continue

        log.info(f"Final results will be located at {job_storage}")
        try:
            runner.start()
        except KeyboardInterrupt:
            runner.purge()
            raise

        # Sync results stats files into storage folder
        job_storage.mkdir(parents=True)
        stats_storage.mkdir()
        stats_files = runner.stats_files_paths()
        assert (
            len(stats_files) == runner.job_cnt()
        ), f"{len(stats_files)} != {runner.job_cnt()}"
        for i, stats_path in enumerate(stats_files):
            shutil.copy(stats_path, stats_storage / f"{i}_stats_file.txt")

        log.info("Removing runner results")
        runner.purge()


if __name__ == "__main__":
    main()
