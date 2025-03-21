from __future__ import annotations

import inspect
from functools import partial
from types import SimpleNamespace
from typing import Any, Generator

import colorama
import pytest

from .data import MultihostItemData
from .logging import MultihostLogger
from .multihost import MultihostConfig, MultihostDomain, MultihostHost, MultihostRole
from .topology import Topology, TopologyDomain


class MultihostFixture(object):
    """
    Multihost object provides access to underlaying multihost configuration,
    individual domains and hosts. This object should be used only in tests
    as the :func:`mh` pytest fixture.

    Domains are accessible as dynamically created properties of this object,
    hosts are accessible by roles as dynamically created properties of each
    domain. Each host object is instance of specific role class based on
    :mod:`~pytest_mh.MultihostRole`.

    .. code-block:: yaml
        :caption: Example multihost configuration

        domains:
        - id: test
          hosts:
          - name: client
            hostname: client.test
            role: client

          - name: ldap
            hostname: master.ldap.test
            role: ldap

    The configuration above creates one domain of id ``test`` with two hosts.
    The following example shows how to access the hosts:

    .. code-block:: python
        :caption: Example of the MultihostFixture object

        def test_example(mh: MultihostFixture):
            mh.test            # -> namespace containing roles as properties
            mh.test.client     # -> list of hosts providing given role
            mh.test.client[0]  # -> host object, instance of specific role
    """

    def __init__(
        self, request: pytest.FixtureRequest, data: MultihostItemData, multihost: MultihostConfig, topology: Topology
    ) -> None:
        """
        :param request: Pytest request.
        :type request: pytest.FixtureRequest
        :param data: Multihost item data.
        :type data: MultihostItemData
        :param multihost: Multihost configuration.
        :type multihost: MultihostConfig
        :param topology: Multihost topology for this request.
        :type topology: Topology
        """

        self.data: MultihostItemData = data
        """
        Multihost item data.
        """

        self.request: pytest.FixtureRequest = request
        """
        Pytest request.
        """

        self.multihost: MultihostConfig = multihost
        """
        Multihost configuration.
        """

        self.topology: Topology = topology
        """
        Topology data.
        """

        self.logger: MultihostLogger = multihost.logger
        """
        Multihost logger.
        """

        self.roles: list[MultihostRole] = []
        """
        Available MultihostRole objects.
        """

        self.hosts: list[MultihostHost] = []
        """
        Available MultihostHost objects.
        """

        self._opt_artifacts_dir: str = self.request.config.getoption("mh_artifacts_dir")
        self._opt_artifacts_mode: str = self.request.config.getoption("mh_collect_artifacts")
        self._opt_artifacts_compression: bool = self.request.config.getoption("mh_compress_artifacts")

        self._paths: dict[str, list[MultihostRole] | MultihostRole] = {}
        self._skipped: bool = False

        for domain in self.multihost.domains:
            if domain.id in topology:
                setattr(self, domain.id, self._domain_to_namespace(domain, topology.get(domain.id)))

        self.roles = sorted([x for x in self._paths.values() if isinstance(x, MultihostRole)], key=lambda x: x.role)
        self.hosts = sorted(list({x.host for x in self.roles}), key=lambda x: x.hostname)

    def _domain_to_namespace(self, domain: MultihostDomain, topology_domain: TopologyDomain) -> SimpleNamespace:
        ns = SimpleNamespace()
        for role_name in domain.roles:
            if role_name not in topology_domain:
                continue

            count = topology_domain.get(role_name)
            roles = [domain.create_role(self, host) for host in domain.hosts_by_role(role_name)[:count]]

            self._paths[f"{domain.id}.{role_name}"] = roles
            for index, role in enumerate(roles):
                self._paths[f"{domain.id}.{role_name}[{index}]"] = role

            setattr(ns, role_name, roles)

        return ns

    def _lookup(self, path: str) -> MultihostRole | list[MultihostRole]:
        """
        Lookup host by path. The path format is ``$domain.$role``
        or ``$domain.$role[$index]``

        :param path: Host path.
        :type path: str
        :raises LookupError: If host is not found.
        :return: The role object if index was given, list of role objects otherwise.
        :rtype: MultihostRole | list[MultihostRole]
        """

        if path not in self._paths:
            raise LookupError(f'Name "{path}" does not exist')

        return self._paths[path]

    def _skip(self) -> bool:
        if self.data.topology_mark is None:
            raise ValueError("Multihost fixture is available but no topology mark was set")

        self._skipped = False

        fixtures: dict[str, Any] = {k: None for k in self.data.topology_mark.fixtures.keys()}
        fixtures.update(self.request.node.funcargs)
        self.data.topology_mark.apply(self, fixtures)

        # Make sure mh fixture is always available
        fixtures["mh"] = self

        for mark in self.request.node.iter_markers("require"):
            if len(mark.args) not in [1, 2]:
                raise ValueError(
                    f"{self.request.node.nodeid}::{self.request.node.originalname}: "
                    "invalid arguments for @pytest.mark.require"
                )

            condition = mark.args[0]
            reason = "Required condition was not met" if len(mark.args) != 2 else mark.args[1]

            args: list[str] = []
            if isinstance(condition, partial):
                spec = inspect.getfullargspec(condition.func)

                # Remove bound positional parameters
                args = spec.args[len(condition.args) :]

                # Remove bound keyword parameters
                args = [x for x in args if x not in condition.keywords]
            else:
                spec = inspect.getfullargspec(condition)
                args = spec.args

            if spec.varkw is None:
                # No **kwargs is present, just pick selected arguments
                callspec = {k: v for k, v in fixtures.items() if k in args}
            else:
                # **kwargs is present, pass everything
                callspec = fixtures

            callresult = condition(**callspec)
            if isinstance(callresult, tuple):
                if len(callresult) != 2:
                    raise ValueError(
                        f"{self.request.node.nodeid}::{self.request.node.originalname}: "
                        "invalid arguments for @pytest.mark.require"
                    )

                result = callresult[0]
                reason = callresult[1]
            else:
                result = callresult

            if not result:
                self._skipped = True
                pytest.skip(reason)

        return self._skipped

    def _setup(self) -> None:
        """
        Setup multihost. A setup method is called on each host and role
        to initialize the test environment to expected state.
        """
        if self._skipped:
            return

        setup_ok: list[MultihostHost | MultihostRole] = []
        for item in self.hosts + self.roles:
            try:
                item.setup()
            except Exception:
                # Teardown hosts and roles that were successfully setup before this error
                for i in reversed(setup_ok):
                    i.teardown()
                raise

            setup_ok.append(item)

    def _teardown(self) -> None:
        """
        Teardown multihost. The purpose of this method is to revert any changes
        that were made during a test run. It is automatically called when the
        test is finished.
        """
        if self._skipped:
            return

        # Create list of dynamically added artifacts
        additional_artifacts: dict[MultihostHost, list[str]] = {}
        for role in self.roles:
            additional_artifacts.setdefault(role.host, []).extend(role.artifacts)

        errors = []

        # Collect artifacts, it an error is raised, we will ignore it since
        # teardown is more important
        for host in self.hosts:
            try:
                self._collect_artifacts(host, additional_artifacts[host])
            except Exception as e:
                errors.append(e)

        for item in self.roles + self.hosts:
            try:
                item.teardown()
            except Exception as e:
                errors.append(e)

        if errors:
            raise Exception(errors)

    def _artifacts_dir(self) -> str | None:
        """
        Return test artifact directory or ``None`` if no artifacts should be
        stored.

        :return: Artifact directory or ``None``.
        :rtype: str | None
        """
        if self._skipped:
            return None

        dir = self._opt_artifacts_dir
        mode = self._opt_artifacts_mode
        if mode == "never" or (mode == "on-failure" and self.data.outcome != "failed"):
            return None

        name = self.request.node.name
        name = name.translate(str.maketrans('":<>|*? [', "---------", "]()"))

        return f"{dir}/{name}"

    def _collect_artifacts(self, host: MultihostHost, artifacts: list[str]) -> None:
        """
        Collect test artifacts that were requested by the multihost
        configuration.

        :param host: Host object where the artifacts will be collected.
        :type host: MultihostHost
        :param artifacts: Additional artifacts that will be fetched together
            with artifacts from configuration file.
        :type artifacts: list[str]
        """
        path = self._artifacts_dir()
        if path is None:
            return

        host.collect_artifacts(path, artifacts, self._opt_artifacts_compression)

    def _flush_logs(self) -> None:
        """
        Write log messages produced by current test case to a file, or clear
        them if no artifacts should be generated.
        """
        path = self._artifacts_dir()
        if path is None:
            self.logger.clear()
        else:
            self.logger.write_to_file(f"{path}/test.log")

    def log_phase(self, phase: str) -> None:
        """
        Log current test phase.

        :param phase: Phase name or description.
        :type phase: str
        """
        self.logger.info(
            self.logger.colorize(
                f"{phase} :: {self.request.node.nodeid}",
                colorama.Style.BRIGHT,
                colorama.Back.BLACK,
                colorama.Fore.WHITE,
            )
        )

    def __enter__(self) -> MultihostFixture:
        if self._skip():
            return self

        self.log_phase("BEGIN")
        self.log_phase("SETUP")
        try:
            self._setup()
        finally:
            self.log_phase("SETUP DONE")

        return self

    def __exit__(self, exception_type, exception_value, traceback) -> None:
        self.log_phase("TEARDOWN")
        try:
            self._teardown()
        finally:
            self.log_phase("TEARDOWN DONE")
            self.log_phase("END")
            self._flush_logs()


@pytest.fixture(scope="function")
def mh(request: pytest.FixtureRequest) -> Generator[MultihostFixture, None, None]:
    """
    Pytest multihost fixture. Returns instance of :class:`MultihostFixture`.
    When a pytest test is finished, this fixture takes care of tearing down the
    :class:`MultihostFixture` object automatically in order to clean up after
    the test run.

    .. note::

        It is preferred that the test case does not use this fixture directly
        but rather access the hosts through dynamically created role fixtures
        that are defined in ``@pytest.mark.topology``.

    :param request: Pytest's ``request`` fixture.
    :type request: pytest.FixtureRequest
    :raises ValueError: If not multihost configuration was given.
    :yield: MultihostFixture
    """

    data: MultihostItemData | None = MultihostItemData.GetData(request.node)
    if data is None:
        nodeid = f"{request.node.parent.nodeid}::{request.node.originalname}"
        raise ValueError(f"{nodeid}: mh fixture requested but no multihost configuration was provided")

    if data.multihost is None:
        raise ValueError("data.multihost must not be None")

    if data.topology_mark is None:
        raise ValueError("data.topology_mark must not be None")

    with MultihostFixture(request, data, data.multihost, data.topology_mark.topology) as mh:
        mh.log_phase("TEST")
        yield mh
        mh.log_phase("TEST DONE")
