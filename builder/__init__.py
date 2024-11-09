from abc import ABC, abstractmethod
import os
import shutil
import typing as t
from dataclasses import dataclass
from pathlib import Path
from logger import get_logger

log = get_logger()


@dataclass(frozen=True)
class BuildArtifact:
    """
    The build result of a target application.
    """

    name: str
    bin_path: Path
    seed_dir: Path
    args: t.List[str]

    def with_new_root(self, new_root: Path) -> "BuildArtifact":
        return BuildArtifact(
            self.name,
            new_root / self.bin_path.name,
            new_root / self.seed_dir.name,
            self.args,
        )

    def validate(self) -> bool:
        for path in [self.bin_path, self.seed_dir]:
            assert path.exists(), f"{path} does not exists"
        return True


class Builder(ABC):
    """
    Interface for something that builds a software.
    """

    @staticmethod
    def get_build_directory(suffix: Path) -> Path:
        return Path(__file__).parent / "build-dirs" / suffix

    @staticmethod
    def get_build_env() -> t.Dict[str, str]:
        env = os.environ.copy()
        env["DEBIAN_FRONTEND"] = "noninteractive"
        return env

    def __init__(self) -> None:
        self._build_dir = Builder.get_build_directory(Path(self.target_name()))
        self._build_dir.mkdir(parents=True, exist_ok=True)
        self._purge_on_next_build_flags_file = self.work_dir() / "purge_next_build"

    @abstractmethod
    def target_name(self) -> str:
        raise NotImplementedError()

    def work_dir(self) -> Path:
        return self._build_dir

    def workdir_exists_and_is_not_empty(self) -> bool:
        if not self.work_dir().exists():
            return False
        return any(self.work_dir().glob("*"))

    def set_purge_on_next_build_flag(self):
        self._purge_on_next_build_flags_file.write_text("ON")

    def is_purge_on_next_build_flag_set(self):
        return self._purge_on_next_build_flags_file.exists()

    @abstractmethod
    def build(self, purge: bool = False) -> bool:
        log.info(f"Build dir is {self.work_dir()}")
        if self.workdir_exists_and_is_not_empty() and purge:
            log.info("Build dir already exists and purge is set.")
            shutil.rmtree(self.work_dir().as_posix())
        elif (
            self.workdir_exists_and_is_not_empty()
            and self.is_purge_on_next_build_flag_set()
        ):
            log.info("Build dir already exists and purge_on_next_build flag is set.")
            shutil.rmtree(self.work_dir().as_posix())
        elif self.workdir_exists_and_is_not_empty() and not purge:
            log.info("Build dir already exists, and purge was not set. We are done.")
            return False

        self.work_dir().mkdir(parents=True, exist_ok=True)
        return True


class AflTargetBuildere(Builder):

    def __init__(self, afl_cc: Path, afl_cxx: Path) -> None:
        super().__init__()
        self._afl_cc = afl_cc
        self._afl_cxx = afl_cxx

    def __str__(self) -> str:
        return f"{self.__class__.__name__}()"

    def afl_cc(self) -> Path:
        return self._afl_cc

    def afl_cxx(self) -> Path:
        return self._afl_cxx

    @abstractmethod
    def build(self, purge: bool = False) -> bool:
        return super().build(purge)

    @abstractmethod
    def build_artifact(self) -> BuildArtifact:
        raise NotImplementedError()
