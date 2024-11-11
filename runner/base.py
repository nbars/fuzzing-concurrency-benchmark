from abc import ABC, abstractmethod
from pathlib import Path
import shutil
import typing as t

from builder import BuildArtifact
from builder.aflpp import AflConfig
from logger import get_logger

log = get_logger()


class EvaluationRunner(ABC):

    def __init__(
        self,
        target: BuildArtifact,
        afl_config: AflConfig,
        job_cnt: int,
        timeout_s: int,
        custom_attrs: t.Optional[t.Dict[str, str]] = None,
    ) -> None:
        self._target = target
        self._afl_config = afl_config
        self._job_cnt = job_cnt
        self._timeout_s = timeout_s
        self._custom_attrs = custom_attrs
        self._work_dir = EvaluationRunner.get_runner_work_dir(Path(str(self)))
        self._purge_on_next_prepare_flags_file = (
            self.work_dir() / "purge_on_next_prepare_flag"
        )

    @staticmethod
    def get_runner_work_dir(suffix: Path) -> Path:
        return Path(__file__).parent / "work-dirs" / suffix

    def work_dir(self) -> Path:
        return self._work_dir

    def target(self) -> BuildArtifact:
        return self._target

    def afl_config(self) -> AflConfig:
        return self._afl_config

    def job_cnt(self) -> int:
        return self._job_cnt

    def set_purge_on_next_prepare_flag(self):
        if self._purge_on_next_prepare_flags_file.parent.exists():
            self._purge_on_next_prepare_flags_file.write_text("ON")

    def is_purge_on_next_prepare_flag_set(self):
        return self._purge_on_next_prepare_flags_file.exists()

    @abstractmethod
    def prepare(self, purge: bool = False) -> bool:
        log.info(f"Preparing {self} in directory {self.work_dir()}")
        if self.work_dir().exists() and purge:
            log.info(f"Purging directory {self.work_dir()} because of purge beeing set")
            self.purge()
        elif self.work_dir().exists() and self.is_purge_on_next_prepare_flag_set():
            log.info(
                f"Purging directory {self.work_dir()} because of purge_on_next_prepare flag"
            )
            self.purge()
        elif self.work_dir().exists():
            return False
        self.work_dir().mkdir(parents=True)
        return True

    def __str__(self) -> str:
        custom_attrs_str = ""
        if self._custom_attrs:
            for k, v in self._custom_attrs.items():
                custom_attrs_str += f",{k}={v}"

        return f"{self.__class__.__name__},target_name={self._target.name},job_cnt={self._job_cnt},timeout_s={self._timeout_s}{custom_attrs_str}"

    def purge(self) -> None:
        if self.work_dir().exists():
            log.info(f"Purging {self.work_dir()}")
            shutil.rmtree(self.work_dir())

    @abstractmethod
    def start(self) -> None:
        raise NotImplementedError()

    @abstractmethod
    def stats_files_paths(self) -> t.List[Path]:
        raise NotImplementedError()
