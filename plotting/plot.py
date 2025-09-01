# %%


from collections import defaultdict
import pandas as pd
import argparse
from pathlib import Path
import re
import pickle
from tqdm import tqdm
from threading import Lock
import typing as t
import altair as alt
from multiprocessing.pool import ThreadPool
from dataclasses import dataclass

alt.data_transformers.disable_max_rows()
alt.data_transformers.enable("vegafusion")
pd.set_option("display.max_rows", None)


@dataclass(slots=True, frozen=True, unsafe_hash=True)
class FuzzerStats:
    attributes: t.Dict[str, str]

    @staticmethod
    def from_afl_stats_file(path: Path):
        content = path.read_text()
        attributes = re.findall(
            r"^(?P<key>\S+)\s*:\s*(?P<value>.+)", content, flags=re.MULTILINE
        )
        attributes_kv: t.Dict[str, str] = dict()
        for k, v in attributes:
            attributes_kv[k] = v

        return FuzzerStats(attributes_kv)

    def run_time(self) -> int:
        return int(self.attributes["run_time"])

    def execs_done(self) -> int:
        return int(self.attributes["execs_done"])

    def execs_per_s(self) -> float:
        return self.execs_done() / self.run_time()

    def __getattr__(self, name: str) -> str:
        v = self.attributes.get(name, None)
        assert (
            v
        ), f"{name} is not a valid attribute. Valid attributes: {self.attributes.keys()}"
        return v


class ResultArtifact:
    __slots__ = ("_path", "_runner_name", "_attributes", "_fuzzers_stats")

    def __init__(self, path: Path) -> None:
        self._path = path
        name = path.name
        # First parse the folder name
        # DefaultVmRunner:target_name=readelf,job_cnt=47,timeout_s=600
        if ":" not in name:
            # new schema to make Docker --mount happy  :)
            runner_attrs = re.match(r"(?P<runner>\S+?),(?P<attrs>\S+)", name)
        else:
            runner_attrs = re.match(r"(?P<runner>\S+):(?P<attrs>\S+)", name)
        assert runner_attrs, name
        self._runner_name: str = runner_attrs.group("runner")
        attrs_str = runner_attrs.group("attrs")
        attrs = dict()
        for attr_kv in attrs_str.split(","):
            k, v = attr_kv.split("=")
            assert k not in attrs
            attrs[k] = v
        self._attributes: t.Dict[str, str] = attrs
        for needle in ["target_name", "job_cnt", "timeout_s"]:
            assert needle in attrs, f"{needle} not in {self._attributes}, {name}"

        # Parse the stats files
        fuzzers_stats = []
        for p in (self._path / "stats_files").glob("*"):
            stats = FuzzerStats.from_afl_stats_file(p)
            fuzzers_stats.append(stats)
        self._fuzzers_stats: t.List[FuzzerStats] = fuzzers_stats
        assert (
            len(self._fuzzers_stats) == self.job_cnt
        ), f"{self._path}, {len(self._fuzzers_stats)} != {self.job_cnt}"

    @staticmethod
    def parse_or_from_cache(path: Path):
        if obj := artifact_cache.get(path):
            return obj
        obj = ResultArtifact(path)
        artifact_cache.store(path, obj)
        return obj

    @property
    def runner_name(self) -> str:
        return self._runner_name

    @property
    def job_cnt(self) -> int:
        return int(self.attrs["job_cnt"])

    @property
    def timeout_s(self) -> int:
        return int(self.attrs["timeout_s"])

    @property
    def target_name(self) -> str:
        return self.attrs["target_name"]

    @property
    def cpu(self) -> str:
        return self.attrs["cpu"]

    @property
    def attrs(self) -> t.Dict[str, str]:
        return self._attributes

    @property
    def fuzzer_stats(self) -> t.List[FuzzerStats]:
        return self._fuzzers_stats

    @property
    def execs_per_s(self) -> float:
        stats = self.fuzzer_stats
        return sum([e.execs_per_s() for e in stats])

    @property
    def execs(self) -> float:
        stats = self.fuzzer_stats
        return sum([e.execs_done() for e in stats])

    @property
    def execs_per_core_per_s(self) -> float:
        return self.execs_per_s / self.job_cnt


class ResultArtifactCache:

    def __init__(self, path: Path):
        self._path = path
        self._data = {}
        self._lock = Lock()

        if path.exists():
            self._data = pickle.load(path.open("rb"))

    def sync_to_disk(self):
        with self._path.open("wb") as f:
            pickle.dump(self._data, f)

    def get(self, path: Path) -> t.Optional["ResultArtifact"]:
        with self._lock:
            return self._data.get(path, None)

    def store(self, path: Path, o: "ResultArtifact"):
        with self._lock:
            self._data[path] = o


artifact_cache_path = Path("cache.bin")
artifact_cache = ResultArtifactCache(artifact_cache_path)


def load_artifacts(storage: Path) -> t.List[ResultArtifact]:
    print("Parsing result artifacts...")
    all_artifacts = []

    paths = [path for path in storage.glob("*") if not path.is_file()]
    with ThreadPool() as pool:
        for artifact in tqdm(
            pool.imap_unordered(ResultArtifact.parse_or_from_cache, paths),
            total=len(paths),
        ):
            if artifact:
                all_artifacts.append(artifact)
    print(f"Loaded {len(all_artifacts)} artifact")
    artifact_cache.sync_to_disk()

    return all_artifacts


storage = Path("../storage/")
graphs_dir = Path("graphs")
graphs_dir.mkdir(exist_ok=True)


if not storage.exists():
    print(f"[!] Storage folder {storage} does not seem to exist")

all_artifacts = load_artifacts(storage)
all_runner_names = [r.runner_name for r in all_artifacts]

# %%

# selected_artifacts = [
#     a
#     for a in all_artifacts
#     if a.runner_name
#     in [
#         "DefaultAflRunner",
#         "DefaultDockerRunner",
#         "DockerRunnerWithoutOverlayfs",
#         "AflRunnerWithoutPin",
#         "DefaultVmRunnerV2",
#         "VmRunner8coresV2",
#         "VmRunner2cores",
#         "VmRummer8cores",
#         "DockerRunner8cores",
#     ]
# ]

data_frame_dict = defaultdict(list)
for artifact in all_artifacts:
    data_frame_dict["runner_name"].append(artifact.runner_name)
    data_frame_dict["target_name"].append(artifact.target_name)
    data_frame_dict["job_cnt"].append(artifact.job_cnt)
    data_frame_dict["cpu"].append(artifact.cpu)
    data_frame_dict["execs"].append(int(artifact.execs))
    data_frame_dict["execs_per_s"].append(int(artifact.execs_per_s))
    data_frame_dict["execs_per_core_per_s"].append(int(artifact.execs_per_core_per_s))

data_frame = pd.DataFrame(data_frame_dict)
# %%


def plot_for_runner(
    data_frame: pd.DataFrame, save_name: str, runners: t.Optional[t.List[str]] = None
) -> alt.LayerChart:
    max_job_cnt = 96 * 4
    selection = alt.selection_point(fields=["runner_name"], bind="legend")

    lines = (
        alt.Chart(data_frame)
        .transform_filter(
            alt.datum.job_cnt
            <= max_job_cnt
            # ).transform_filter(
            #     alt.datum.job_cnt % (2) == 0
        )
        .mark_line(point=True)
        .encode(
            x=alt.X("job_cnt:Q", title="Number of Fuzzers"),
            y=alt.Y("execs_per_s:Q", title="Total Execs/s"),
            color=alt.Color("runner_name:N", title="Runner"),
            shape=alt.Shape("cpu:N"),
            strokeDash="cpu:N",
            opacity=alt.condition(selection, alt.value(1.0), alt.value(0.06)),
            tooltip=alt.Tooltip(["runner_name:N", "execs_per_s:Q"]),
        )
        .properties(
            title="AFL++ v4.21c",
        )
    ).add_params(selection)

    assert runners is None or len(runners) > 0
    if runners:
        lines = lines.transform_filter(
            alt.FieldOneOfPredicate(field="runner_name", oneOf=runners)
        )

    num_cores_ruler_52 = (
        alt.Chart(pd.DataFrame({"x": [52]})).mark_rule(color="green").encode(x="x:Q")
    )
    num_threads_ruler_104 = (
        alt.Chart(pd.DataFrame({"x": [52 * 2]})).mark_rule(color="red").encode(x="x:Q")
    )

    num_cores_ruler_192 = (
        alt.Chart(pd.DataFrame({"x": [192]})).mark_rule(color="green").encode(x="x:Q")
    )
    num_threads_ruler_384 = (
        alt.Chart(pd.DataFrame({"x": [192 * 2]})).mark_rule(color="red").encode(x="x:Q")
    )

    chart = (
        lines
        + num_cores_ruler_52
        + num_threads_ruler_104
        + num_cores_ruler_192
        + num_threads_ruler_384
    )
    chart = chart.configure_legend(
        titleFontSize=14, labelFontSize=9, labelLimit=0, orient="right"
    )

    chart.save(graphs_dir / f"{save_name}.png", scale_factor=3.0)
    chart.save(graphs_dir / f"{save_name}.html", scale_factor=5.0)
    return chart


# print("[+] All runners plot")
# all_chart = plot_for_runner(data_frame, "all")
# all_chart.show()

print("[+] Afl runners plot")
# runner_names = [r for r in all_runner_names if "Afl" in r]
data_frame = data_frame[data_frame["target_name"] == "readelf-persistante"]
# runner_names = ["AflRunner", "AflRunnerTurbo", "AflRunnerWithoutPin"]
runner_names = [
    "AflRunner",
    "DockerRunner",
    "DockerRunnerPriv",
]
chart = plot_for_runner(data_frame, "afl", runners=runner_names)
chart.show()

# print("[+] Docker runners plot")
# runner_names = [r for r in all_runner_names if "Docker" in r] + ["AflRunner"]
# chart = plot_for_runner(data_frame, "docker", runners=runner_names)
# chart.show()

# print("[+] VM runners plot")
# runner_names = [r for r in all_runner_names if "Vm" in r] + ["AflRunner"]
# chart = plot_for_runner(data_frame, "vm", runners=runner_names)
# chart.show()


# %%
