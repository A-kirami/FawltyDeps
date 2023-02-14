"""Verify behavior of FawltyDeps on real Python projects.

These are bigger integration tests that are not meant to be run on every commit.
We download/extract pinned releases several 3rd-party Python projects, and run
FawltyDeps on them, with hardcoded expectations per project on what FawltyDeps
should find/report.
"""
import json
import hashlib
import logging
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any, Dict, Iterator, List, NamedTuple, Optional, Tuple
from urllib.parse import urlparse
from urllib.request import urlretrieve

import pytest

from fawltydeps.extract_declared_dependencies import TomlData

if sys.version_info >= (3, 11):
    import tomllib  # pylint: disable=E1101
else:
    import tomli as tomllib

logger = logging.getLogger(__name__)

# Each of these tests will download and unpack a 3rd-party project before analyzing it;
# therefore, they're slow and are skipped by default.
pytestmark = pytest.mark.integration

# Directory with .toml files that define test cases for selected tarballs from
# 3rd-party/real-world projects.
REAL_PROJECTS_DIR = Path(__file__).with_name("real_projects")


def run_fawltydeps(
    *args: str,
    cwd: Optional[Path] = None,
) -> Dict[str, Any]:
    proc = subprocess.run(
        ["fawltydeps"] + list(args) + ["--json"],
        stdout=subprocess.PIPE,
        check=False,
        cwd=cwd,
    )
    assert proc.returncode in {0, 3, 4}
    return json.loads(proc.stdout)  # type: ignore


def sha256sum(path: Path):
    """Calculate the SHA256 checksum of the given file.

    Read the file in 64kB blocks while calculating the checksum, and return
    the hex-encoded digest.
    """
    sha256 = hashlib.sha256()
    BLOCK_SIZE = 64 * 1024
    with path.open("rb") as f:
        for block in iter(lambda: f.read(BLOCK_SIZE), b""):
            sha256.update(block)
    return sha256.hexdigest()


class Experiment(NamedTuple):
    """A single experiment on a real world project

    Input to the experiment(`args`) is the set of
    command line options to run `fawltydeps` command line tool.

    The expected results of the experiment are `Analysis` results, namely:
    `imports`, `declared_deps`, `undeclared_deps`, `unused_deps`
    """

    name: str
    args: List[str]
    description: Optional[str] = None
    imports: Optional[List[str]] = None
    declared_deps: Optional[List[str]] = None
    undeclared_deps: Optional[List[str]] = None
    unused_deps: Optional[List[str]] = None

    @classmethod
    def parse_from_toml(cls, name: str, data: TomlData) -> "Experiment":
        return cls(
            name=name,
            args=data["args"],
            description=data.get("description"),
            imports=data.get("imports"),
            declared_deps=data.get("declared_deps"),
            undeclared_deps=data.get("undeclared_deps"),
            unused_deps=data.get("unused_deps"),
        )

    def verify_analysis_json(self, analysis: Dict[str, Any]) -> None:
        """Compare Experiment with json results of `Analysis`."""
        if self.imports:
            print(f"Checking imports for experiment: {self.name}")
            assert set(self.imports) == {imp["name"] for imp in analysis["imports"]}
        if self.declared_deps:
            print(f"Checking declared dependencies for experiment: {self.name}")
            assert set(self.declared_deps) == {
                dep["name"] for dep in analysis["declared_deps"]
            }
        if self.undeclared_deps:
            print(f"Checking undeclared dependencies for experiment: {self.name}")
            assert set(self.undeclared_deps) == {
                imp["name"] for imp in analysis["undeclared_deps"]
            }
        if self.unused_deps:
            print(f"Checking unused dependencies for experiment: {self.name}")
            assert set(self.unused_deps) == {
                dep["name"] for dep in analysis["unused_deps"]
            }


class ThirdPartyProject(NamedTuple):
    """Encapsulate a 3rd-party project to be tested with FawltyDeps.

    This ultimately identifies a tarball containing a 3rd-party Python project,
    and the things we expect FawltyDeps to find when run on that unpacked
    tarball.

    The actual data populating these objects is read from TOML files in
    REAL_PROJECTS_DIR, and the tarballs are downloaded, unpacked, and cached
    by the cached_tarball() fixture above.
    """

    # TODO: Use TOML array of tables (https://toml.io/en/v1.0.0#array-of-tables)
    # to allow the definition of more than one sets of tests per .toml file.
    # The idea is to allow multiple runs of fawltydeps on the project (with
    # different --code and --deps options, as well as other options in the
    # future). This would split this class into two parts, one with the
    # project metadata, and then a list tests, each of which define the
    # necessary fawltydeps options to use, along with the expected
    # .imports, .declared_deps, .undeclared_deps, and .unused_deps.

    toml_path: Path
    name: str
    url: str
    sha256: str
    experiments: List[Experiment]
    description: Optional[str] = None

    @classmethod
    def parse_from_toml(cls, path: Path) -> "ThirdPartyProject":
        try:
            with path.open("rb") as f:
                data = tomllib.load(f)
        except tomllib.TOMLDecodeError:
            print(f"Error occurred while parsing file: {path}")
            raise

        # We ultimately _trust_ the .toml files read here, so we can skip all
        # the usual error checking associated with validating external data.
        return cls(
            toml_path=path,
            name=data["project"]["name"],
            description=data["project"].get("description"),
            url=data["project"]["url"],
            sha256=data["project"]["sha256"],
            experiments=[
                Experiment.parse_from_toml(name, values)
                for name, values in data["experiments"].items()
            ],
        )

    @classmethod
    def collect(cls) -> Iterator[Tuple["ThirdPartyProject", Experiment]]:
        for path in REAL_PROJECTS_DIR.iterdir():
            if path.suffix == ".toml":
                project = cls.parse_from_toml(path)
                for experiment in project.experiments:
                    yield (project, experiment)

    def tarball_name(self) -> str:
        """The filename used for the tarball in the local cache."""
        # We cache tarballs using the filename part of the given URL.
        # However, tarballs produced from tags at GitHub typically only use the
        # version number in the filename. Prefix the project name in that case:
        filename = Path(urlparse(self.url).path).name
        if self.name not in filename:
            filename = f"{self.name}-{filename}"
        return filename

    def tarball_is_cached(self, path: Optional[Path]) -> bool:
        """Return True iff the given path contains this project's tarball."""
        return path is not None and path.is_file() and sha256sum(path) == self.sha256

    def get_tarball(self, cache: pytest.Cache) -> Path:
        """Get this project's tarball. Download if not already cached.

        The cached tarball is keyed by its filename and integrity checked with
        SHA256. Thus a changed URL with the same filename and sha256 checksum
        will still be able to reuse a previously downloaded tarball.

        """
        filename = self.tarball_name()
        # Cannot store Path objects in the pytest cache, only str.
        cached_str = cache.get(f"fawltydeps/{filename}", None)
        if self.tarball_is_cached(cached_str and Path(cached_str)):
            return Path(cached_str)  # already cached

        # Must (re)download
        tarball = Path(cache.mkdir("fawltydeps")) / filename
        logger.info(f"Downloading {self.url!r} to {tarball}...")
        urlretrieve(self.url, tarball)
        if not self.tarball_is_cached(tarball):
            logger.error(f"Failed integrity check after downloading {self.url!r}!")
            logger.error(f"    Downloaded file: {tarball}")
            logger.error(f"    Retrieved SHA256 {sha256sum(tarball)}")
            logger.error(f"     Expected SHA256 {self.sha256}")
            assert False
        cache.set(f"fawltydeps/{filename}", str(tarball))
        return tarball

    def get_unpack_dir(self, tarball: Path, cache: pytest.Cache) -> Path:
        """Get this project's unpack dir. Unpack the given tarball if necessary.

        The unpack dir is where we unpack the project's tarball. It is keyed by
        the sha256 checksum of the tarball, so that we don't risk reusing a
        previously cached unpack dir for a different tarball.
        """
        # We cache unpacked tarballs using the given sha256 sum
        cached_str = cache.get(f"fawltydeps/{self.sha256}", None)
        if cached_str is not None and Path(cached_str).is_dir():
            return Path(cached_str)  # already cached

        # Must unpack
        unpack_dir = Path(cache.mkdir(f"fawltydeps_{self.sha256}"))
        logger.info(f"Unpacking {tarball} to {unpack_dir}...")
        with tarfile.open(tarball) as f:
            f.extractall(unpack_dir)
        assert unpack_dir.is_dir()
        cache.set(f"fawltydeps/{self.sha256}", str(unpack_dir))
        return unpack_dir

    def get_project_dir(self, cache: pytest.Cache) -> Path:
        """Return the cached/unpacked project directory for this project.

        This makes use of the caching mechanism in pytest, documented on
        https://docs.pytest.org/en/7.1.x/reference/reference.html#config-cache.
        The caching happens in two stages: we cache the downloaded tarball,
        as well as the project directory that results from unpacking it.

        The unpacked tarball is keyed by the given sha256. Thus an updated
        sha256 will cause (a new download and) a new unpack.

        The actual integrity check only happens immediately after a download,
        hence we assume that the local cache (both the downloaded tarball, as
        well as the unpacked tarball) is uncorrupted and immutable.
        """
        tarball = self.get_tarball(cache)
        logger.info(f"Cached tarball is at: {tarball}")
        unpack_dir = self.get_unpack_dir(tarball, cache)

        # Most tarballs contains a single leading directory; descend into it.
        entries = list(unpack_dir.iterdir())
        if len(entries) == 1:
            project_dir = entries[0]
        else:
            project_dir = unpack_dir

        logger.info(f"Unpacked project is at {project_dir}")
        return project_dir


@pytest.mark.parametrize(
    "project, experiment",
    [
        pytest.param(proj, experiment, id=f"{proj.name}_{experiment.name}")
        for proj, experiment in ThirdPartyProject.collect()
    ],
)
def test_real_project(request, project, experiment):
    project_dir = project.get_project_dir(request.config.cache)
    analysis = run_fawltydeps(*experiment.args, cwd=project_dir)

    print(f"Checking project: {project.name} for experiment: {experiment.name}")

    experiment.verify_analysis_json(analysis)
