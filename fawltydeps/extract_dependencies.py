"Collect declared dependencies of the project"

import ast
import configparser
import logging
import sys
from pathlib import Path
from typing import Any, Dict, Iterator

from pkg_resources import parse_requirements

from fawltydeps.types import DeclaredDependency
from fawltydeps.utils import walk_dir

if sys.version_info >= (3, 11):
    import tomllib  # pylint: disable=E1101
else:
    import tomli as tomllib

TomlData = Dict[str, Any]  # type: ignore

logger = logging.getLogger(__name__)

ERROR_MESSAGE_TEMPLATE = "Failed to %s %s %s dependencies in %s."


class DependencyParsingError(Exception):
    "Error raised when parsing of dependency fails"

    def __init__(self, value: ast.AST):
        super().__init__(value)
        self.value = value


def parse_requirements_contents(
    text: str, path_hint: Path
) -> Iterator[DeclaredDependency]:
    """
    Extract dependencies (packages names) from the requirement.txt file
    and other following Requirements File Format. For more information, see
    https://pip.pypa.io/en/stable/reference/requirements-file-format/.

    Parsed requirements keys are put to lower cases.
    """
    for requirement in parse_requirements(text):
        yield DeclaredDependency(name=requirement.key, location=path_hint)


def parse_setup_contents(text: str, path_hint: Path) -> Iterator[DeclaredDependency]:
    """
    Extract dependencies (package names) from setup.py.
    Function call `setup` where dependencies are listed
    is at the outermost level of setup.py file.
    """

    def _extract_deps_from_bottom_level_list(
        deps: ast.AST,
    ) -> Iterator[DeclaredDependency]:
        if isinstance(deps, ast.List):
            for element in deps.elts:
                # Python v3.8 changed from ast.Str to ast.Constant
                if isinstance(element, (ast.Constant, ast.Str)):
                    yield from parse_requirements_contents(
                        ast.literal_eval(element), path_hint=path_hint
                    )
        else:
            raise DependencyParsingError(deps)

    def _extract_deps_from_setup_call(node: ast.Call) -> Iterator[DeclaredDependency]:
        for keyword in node.keywords:
            try:
                if keyword.arg == "install_requires":
                    yield from _extract_deps_from_bottom_level_list(keyword.value)
                elif keyword.arg == "extras_require":
                    if isinstance(keyword.value, ast.Dict):
                        logger.debug(ast.dump(keyword.value))
                        for elements in keyword.value.values:
                            logger.debug(ast.dump(elements))
                            yield from _extract_deps_from_bottom_level_list(elements)
                    else:
                        raise DependencyParsingError(keyword.value)
            except DependencyParsingError as exc:
                logger.debug(exc)
                if sys.version_info >= (3, 9):
                    unparsed_content = ast.unparse(exc.value)  # pylint: disable=E1101
                else:
                    unparsed_content = ast.dump(exc.value)
                logger.warning(
                    "Could not parse contents of `%s`: %s",
                    keyword.arg,
                    unparsed_content,
                )

    def _is_setup_function_call(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "setup"
        )

    setup_contents = ast.parse(text, filename=str(path_hint))
    for node in ast.walk(setup_contents):
        if _is_setup_function_call(node):
            # Below line is not checked by mypy, but `_is_setup_function_call`
            # makes sure that `node` is of a proper type.
            yield from _extract_deps_from_setup_call(node.value)  # type: ignore
            break


def parse_setup_cfg_contents(
    text: str, path_hint: Path
) -> Iterator[DeclaredDependency]:
    """
    Extract dependencies (package names) from setup.cfg.

    `ConfigParser` basic building blocks are "sections"
    which are marked by "[..]" in the configuration file.
    Requirements are declared as main dependencies (install_requires),
    extra dependencies (extras_require) and tests dependencies (tests_require).
    See https://setuptools.pypa.io/en/latest/userguide/declarative_config.html
    section: configuring-setup-using-setup-cfg-files for more details.
    The declaration uses `section` + `option` syntax where section may be [options]
    or [options.{requirements_type}].
    """
    parser = configparser.ConfigParser()
    parser.read_string(text)

    def parse_value(value: str) -> Iterator[DeclaredDependency]:
        yield from parse_requirements_contents(
            value,
            path_hint=path_hint,
        )

    def extract_section(section: str) -> Iterator[DeclaredDependency]:
        if section in parser:
            for option in parser.options(section):
                value = parser.get(section, option)
                logger.debug("deps found for [%s]: %s", section, value)
                yield from parse_value(value)

    def extract_option_from_section(
        section: str, option: str
    ) -> Iterator[DeclaredDependency]:
        if section in parser and option in parser.options(section):
            value = parser.get(section, option)
            logger.debug("deps found for [%s] / %s: %s", section, option, value)
            yield from parse_value(value)

    # Parse [options] -> install_requires
    yield from extract_option_from_section("options", "install_requires")

    # Parse [options] -> extras_require, or [options.extras_require]
    yield from extract_option_from_section("options", "extras_require")
    yield from extract_section("options.extras_require")

    # Parse [options] -> tests_require, or [options.tests_require]
    yield from extract_option_from_section("options", "tests_require")
    yield from extract_section("options.tests_require")


def parse_poetry_pyproject_dependencies(
    poetry_config: TomlData, path_hint: Path
) -> Iterator[DeclaredDependency]:
    """
    Extract dependencies (package names) from Poetry fields in pyproject.toml
    """

    def parse_main_dependencies(
        poetry_config: TomlData, path_hint: Path
    ) -> Iterator[DeclaredDependency]:
        for requirement in poetry_config["dependencies"].keys():
            if requirement != "python":
                yield DeclaredDependency(name=requirement, location=path_hint)

    def parse_group_dependencies(
        poetry_config: TomlData, path_hint: Path
    ) -> Iterator[DeclaredDependency]:
        for group in poetry_config["group"].values():
            for requirement in group["dependencies"].keys():
                if requirement != "python":
                    yield DeclaredDependency(name=requirement, location=path_hint)

    def parse_extra_dependencies(
        poetry_config: TomlData, path_hint: Path
    ) -> Iterator[DeclaredDependency]:
        for group in poetry_config["extras"].values():
            if isinstance(group, list):
                for requirement in group:
                    yield from parse_requirements_contents(requirement, path_hint)
            else:
                raise TypeError(f"{group!r} is of type {type(group)}. Expected a list.")

    fields_parsers = {
        "main": parse_main_dependencies,
        "group": parse_group_dependencies,
        "extra": parse_extra_dependencies,
    }

    for field_type, parser in fields_parsers.items():
        try:
            yield from parser(poetry_config, path_hint)
        except KeyError:  # missing fields:
            logger.debug(
                ERROR_MESSAGE_TEMPLATE,
                "find",
                "Poetry",
                field_type,
                path_hint,
            )
        except (AttributeError, TypeError):  # invalid config
            logger.error(
                ERROR_MESSAGE_TEMPLATE,
                "parse",
                "Poetry",
                field_type,
                path_hint,
            )


def parse_pep621_pyproject_contents(
    parsed_contents: TomlData, path_hint: Path
) -> Iterator[DeclaredDependency]:
    """
    Extract dependencies (package names) in PEP 621 styled pyproject.toml
    """

    def parse_main_dependencies(
        parsed_contents: TomlData, path_hint: Path
    ) -> Iterator[DeclaredDependency]:
        dependencies = parsed_contents["project"]["dependencies"]
        if isinstance(dependencies, list):
            for requirement in dependencies:
                yield from parse_requirements_contents(requirement, path_hint)
        else:
            raise TypeError(
                f"{dependencies!r} of type {type(dependencies)}. Expected list."
            )

    def parse_optional_dependencies(
        parsed_contents: TomlData, path_hint: Path
    ) -> Iterator[DeclaredDependency]:
        for group in parsed_contents["project"]["optional-dependencies"].values():
            for requirement in group:
                yield from parse_requirements_contents(requirement, path_hint)

    fields_parsers = {
        "main": parse_main_dependencies,
        "optional": parse_optional_dependencies,
    }
    for field_type, parser in fields_parsers.items():
        try:
            yield from parser(parsed_contents, path_hint)
        except KeyError:
            logger.debug(
                ERROR_MESSAGE_TEMPLATE, "find", "PEP621", field_type, path_hint
            )
        except (AttributeError, TypeError):
            logger.error(
                ERROR_MESSAGE_TEMPLATE, "parse", "PEP621", field_type, path_hint
            )


def parse_pyproject_contents(
    text: str, path_hint: Path
) -> Iterator[DeclaredDependency]:
    """
    Parse dependencies from specific metadata fields in a pyproject.toml file.
    This can currently parse dependencies from dependency fields in:
    - PEP 621 core metadata fields
    - Poetry-specific metadata
    """
    parsed_contents = tomllib.loads(text)

    yield from parse_pep621_pyproject_contents(parsed_contents, path_hint)

    if "poetry" in parsed_contents.get("tool", {}):
        yield from parse_poetry_pyproject_dependencies(
            parsed_contents["tool"]["poetry"], path_hint
        )
    else:
        logger.debug("%s does not contain [tool.poetry].")


def extract_dependencies(path: Path) -> Iterator[DeclaredDependency]:
    """
    Extract dependencies from supported file types.
    Traverse directory tree to find matching files.

    Generate (i.e. yield) dependency names that are declared in the supported files.
    There is no guaranteed ordering on the dependency names.
    """
    parsers = {
        "requirements.txt": parse_requirements_contents,
        "requirements.in": parse_requirements_contents,
        "setup.py": parse_setup_contents,
        "setup.cfg": parse_setup_cfg_contents,
        "pyproject.toml": parse_pyproject_contents,
    }

    logger.debug(path)

    if path.is_file():
        logger.debug(path)
        parser = parsers.get(path.name)
        if parser:
            yield from parser(path.read_text(), path_hint=path)
        else:
            logger.error("Parsing file %s is not supported", path.name)

    else:
        for file in walk_dir(path):
            if file.name in parsers:
                parser = parsers[file.name]
                logger.debug(f"Extracting dependency from {file}.")
                yield from parser(file.read_text(), path_hint=file)
