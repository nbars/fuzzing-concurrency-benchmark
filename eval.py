#!/usr/bin/env python3

import typing as t
import argparse
import shutil
import re
import random
from abc import ABC, abstractmethod
from builder import BuildArtifact
from builder.aflpp import AflConfig, AflppBuilder
from builder.readelf import ReadelfBuilder
from logger import get_logger, setup_root_logger
from pathlib import Path

from runner.base import EvaluationRunner
from runner.plain import PlainAflRunner

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


def main():

    main_parser = argparse.ArgumentParser(
        prog="eval",
        description="Benchmark suit to evaluate the influence of different abstraction mechanisms on fuzzing throughput.",
    )

    main_parser.add_argument(
        "--min-concurrent-jobs",
        type=range_limited_int(),
        default=1,
        help="The minimum number of jobs the test series is started with.",
    )
    main_parser.add_argument(
        "--concurrent-jobs-step",
        type=range_limited_int(),
        default=1,
        help="Steps in that the value is increment start at min-concurrent-jobs to max-concurrent-jobs.",
    )
    main_parser.add_argument(
        "--additional-jobs-step", type=set, default=set([]), help=""
    )
    main_parser.add_argument(
        "--max-concurrent-jobs",
        type=range_limited_int(),
        default=4,
        help="The maximum number of jobs the test series is executed for with.",
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
        default="1m",
        help="The minimum number of jobs the test series is started with.",
    )
    main_parser.add_argument(
        "--build-only",
        type=bool,
        default=False,
        help="Only build the target without performing any experiment.",
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
    job_cnt_configurations_set = additional_jobs_step | job_cnt_configurations_set
    job_cnt_configurations = list(job_cnt_configurations_set)
    random.shuffle(job_cnt_configurations)

    if job_cnt_configurations[-1] != args.max_concurrent_jobs:
        log.warning(
            f"Current steps value will cause the final concurrent value not being the specified max-concurrent-jobs value. Last value evaluated is {job_cnt_configurations[-1]}"
        )
    log.info(
        f"We are going to perform {len(job_cnt_configurations)} different configurations with a timeout of {timeout_s} seconds"
    )

    from runner import PlainAflRunner, DefaultDockerRunner

    enabled_runner_types: list[type[EvaluationRunner]] = [
        PlainAflRunner,
        DefaultDockerRunner,
    ]

    afl_config = build_aflpp()
    targets_artifacts = build_targets(afl_config)

    total_runtime = compute_total_runtime(
        timeout_s, job_cnt_configurations, enabled_runner_types, targets_artifacts
    )
    log.info(f"This campaign will take {total_runtime} in total")

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
        if job_storage.exists():
            log.info(
                f"Results for current configuration are already at {job_storage}, skipping..."
            )
            continue

        log.info(f"Final results will be located at {job_storage}")
        runner.start()

        stats_files = runner.stats_files_paths()
        job_storage.mkdir(parents=True)
        for i, stats_path in enumerate(stats_files):
            shutil.copy(stats_path, job_storage / f"{i}_stats_file.txt")

        log.info("Removing runner results")
        runner.purge()


if __name__ == "__main__":
    main()
