import logging
import threading
import typing as t
from contextlib import contextmanager
from dataclasses import dataclass
from types import MappingProxyType
from typing import TypeVar

from sqlmesh.core.config import CategorizerConfig
from sqlmesh.core.console import Console, set_console
from sqlmesh.core.context import Context
from sqlmesh.core.model import Model
from sqlmesh.utils.dag import DAG
from sqlmesh.utils.date import TimeLike

from ..config import SQLMeshContextConfig
from ..console import (
    ConsoleEvent,
    ConsoleEventHandler,
    ConsoleException,
    DebugEventConsole,
    EventConsole,
    SnapshotCategorizer,
)
from ..events import ConsoleGenerator

logger = logging.getLogger(__name__)

T = TypeVar("T", bound="SQLMeshController")


class PlanRunSharedOptions(t.TypedDict):
    """Options shared between plan and run operations"""

    start: t.NotRequired[TimeLike]
    end: t.NotRequired[TimeLike]
    execution_time: t.NotRequired[TimeLike]
    select_models: t.NotRequired[t.Collection[str]]


class PlanOnlyOptions(t.TypedDict):
    """Options specific ONLY to plan operations"""

    create_from: t.NotRequired[str]
    skip_tests: t.NotRequired[bool]
    restate_models: t.NotRequired[t.Iterable[str]]
    no_gaps: t.NotRequired[bool]
    skip_backfill: t.NotRequired[bool]
    forward_only: t.NotRequired[bool]
    allow_destructive_models: t.NotRequired[t.Collection[str]]
    no_auto_categorization: t.NotRequired[bool]
    effective_from: t.NotRequired[TimeLike]
    include_unmodified: t.NotRequired[bool]
    backfill_models: t.NotRequired[t.Collection[str]]
    categorizer_config: t.NotRequired[CategorizerConfig]
    enable_preview: t.NotRequired[bool]
    run: t.NotRequired[bool]


class RunOnlyOptions(t.TypedDict):
    """Options specific ONLY to run operations"""

    skip_janitor: t.NotRequired[bool]
    ignore_cron: t.NotRequired[bool]


class PlanOptions(PlanOnlyOptions, PlanRunSharedOptions):
    pass


class RunOptions(RunOnlyOptions, PlanRunSharedOptions):
    pass


class PlanAndRunOptions(t.TypedDict):
    """Combined options for plan_and_run operations"""

    shared: t.NotRequired[PlanRunSharedOptions]
    plan: t.NotRequired[PlanOnlyOptions]
    run: t.NotRequired[RunOnlyOptions]


@dataclass(kw_only=True)
class SQLMeshParsedFQN:
    catalog: str
    schema: str
    view_name: str


def parse_fqn(fqn: str) -> SQLMeshParsedFQN:
    split_fqn = fqn.split(".")

    # Remove any quotes
    split_fqn = list(map(lambda a: a.strip("'\""), split_fqn))
    return SQLMeshParsedFQN(
        catalog=split_fqn[0], schema=split_fqn[1], view_name=split_fqn[2]
    )


@dataclass(kw_only=True)
class SQLMeshModelDep:
    fqn: str
    model: Model | None = None

    def parse_fqn(self) -> SQLMeshParsedFQN:
        return parse_fqn(self.fqn)


class SQLMeshInstance:
    """
    A class that manages sqlmesh operations and context within a specific
    environment. This class will run sqlmesh in a separate thread.

    This class provides an interface to plan, run, and manage sqlmesh operations
    with proper console event handling and threading support.

    This class should not be instantiated directly, but instead should be
    created via the `SQLMeshController.instance` method.

    Attributes:
        config (SQLMeshContextConfig): Configuration settings for sqlmesh
        context. console (EventConsole): Console handler for event management.
        logger (logging.Logger): Logger instance for debug and error messages.
        context (Context): sqlmesh context instance. environment (str): Target
        environment name.
    """

    config: SQLMeshContextConfig
    console: EventConsole
    logger: logging.Logger
    context: Context
    environment: str

    def __init__(
        self,
        environment: str,
        console: EventConsole,
        config: SQLMeshContextConfig,
        context: Context,
        logger: logging.Logger,
    ):
        self.environment = environment
        self.console = console
        self.config = config
        self.context = context
        self.logger = logger

    @contextmanager
    def console_context(
        self, handler: ConsoleEventHandler
    ) -> t.Generator[None, None, None]:
        id = self.console.add_handler(handler)
        yield
        self.console.remove_handler(id)

    def plan(
        self,
        categorizer: SnapshotCategorizer | None = None,
        default_catalog: str | None = None,
        **plan_options: t.Unpack[PlanOptions],
    ) -> t.Generator[ConsoleEvent, None, None]:
        """
        Executes a sqlmesh plan operation in a separate thread and yields
        console events.

        This method creates a plan for sqlmesh operations, runs it in a separate
        thread, and provides real-time console output through a generator.

        Args:
            categorizer (Optional[SnapshotCategorizer]): Categorizer for
                snapshots. Defaults to None.
            default_catalog (Optional[str]): Default catalog to use for the
                plan. Defaults to None.
            **plan_options (**PlanOptions): Additional options for plan
                execution.

        Yields:
            ConsoleEvent: Console events generated during plan execution.

        Raises:
            ConsoleException: If an error occurs during plan execution.
        """

        context = self.context

        # Runs things in thread
        def run_sqlmesh_thread(
            logger: logging.Logger,
            context: Context,
            controller: SQLMeshController,
            environment: str,
            plan_options: PlanOptions,
            default_catalog: str,
        ) -> None:
            try:
                builder = context.plan_builder(
                    environment=environment,
                    **plan_options,
                )
                logger.debug("dagster-sqlmesh: plan")
                controller.console.plan(
                    builder,
                    auto_apply=True,
                    default_catalog=default_catalog,
                )
            except Exception as e:
                controller.console.exception(e)

        generator = ConsoleGenerator(self.logger)

        if categorizer:
            self.console.add_snapshot_categorizer(categorizer)

        with self.console_context(generator):
            thread = threading.Thread(
                target=run_sqlmesh_thread,
                args=(
                    self.logger,
                    context,
                    self,
                    self.environment,
                    plan_options,
                    default_catalog,
                ),
            )
            thread.start()

            for event in generator.events(thread):
                match event:
                    case ConsoleException(e):
                        raise e
                    case _:
                        yield event

            thread.join()

    def run(
        self, **run_options: t.Unpack[RunOptions]
    ) -> t.Generator[ConsoleEvent, None, None]:
        """Executes sqlmesh run in a separate thread with console output.

        This method executes SQLMesh operations in a dedicated thread while
        capturing and yielding console events. It handles both successful
        execution and exceptions that might occur during the run.

        Args:
            **run_options (**RunOptions): Additional run options. See the
                RunOptions type.

        Yields:
            ConsoleEvent: Various console events generated during the sqlmesh
                run, including logs, progress updates, and potential
                exceptions.

        Raises:
            Exception: Re-raises any exception caught during the sqlmesh run in
            the main thread.
        """

        # Runs things in thread
        def run_sqlmesh_thread(
            logger: logging.Logger,
            context: Context,
            controller: SQLMeshController,
            environment: str,
            run_options: RunOptions,
        ) -> None:
            logger.debug("dagster-sqlmesh: run")
            try:
                context.run(environment=environment, **run_options)
            except Exception as e:
                controller.console.exception(e)

        generator = ConsoleGenerator(self.logger)
        with self.console_context(generator):
            thread = threading.Thread(
                target=run_sqlmesh_thread,
                args=(
                    self.logger,
                    self.context,
                    self,
                    self.environment,
                    run_options or {},
                ),
            )
            thread.start()

            for event in generator.events(thread):
                match event:
                    case ConsoleException(e):
                        raise e
                    case _:
                        yield event

            thread.join()

    def plan_and_run(
        self,
        categorizer: SnapshotCategorizer | None = None,
        default_catalog: str | None = None,
        plan_and_run_options: PlanAndRunOptions | None = None,
    ) -> t.Generator[ConsoleEvent, None, None]:
        run_options: RunOptions = {}
        plan_options: PlanOptions = {}

        if plan_and_run_options is not None:
            run_options.update(plan_and_run_options.get("run", {}))
            plan_options.update(plan_and_run_options.get("plan", {}))
            run_options.update(plan_and_run_options.get("shared", {}))
            plan_options.update(plan_and_run_options.get("shared", {}))

        yield from self.plan(categorizer, default_catalog, **plan_options)
        yield from self.run(**run_options)

    def models(self) -> MappingProxyType[str, Model]:
        return self.context.models

    def models_dag(self) -> DAG[str]:
        return self.context.dag


class SQLMeshController:
    """Allows control of sqlmesh via a python interface. It is not suggested to
    use the constructor of this class directly, but instead use the provided
    `setup` or `setup_with_config` class methods.

    Attributes:
        config (SQLMeshContextConfig): Configuration settings for sqlmesh
        console (EventConsole): Console handler for event management. logger
        (logging.Logger): Logger instance for debug and error messages.

    Examples:

    To run create a controller for a project located in `path/to/sqlmesh`:

        >>> controller = SQLMeshController.setup("path/to/sqlmesh")

    The controller itself does not represent a running sqlmesh instance. This
    ensures that the controller does not maintain a connection unnecessarily to
    any database. However, when you need to call sqlmesh operations you will
    need to instantiate an instance. To do so, the `instance` method provides a
    context manager for an instance so that any connections are properly closed.
    At this time, only a single instance is allowed to exist at a time and is
    enforced by the `instance()` method.

    To then use the controller to run a plan and run operation in the `dev`
    environment you would do this:

        >>> with controller.instance("dev") as mesh:
        >>>     for event in mesh.plan_and_run():
        >>>         print(event)
    """

    config: SQLMeshContextConfig
    console: EventConsole
    logger: logging.Logger

    @classmethod
    def setup(
        cls,
        path: str,
        gateway: str = "local",
        debug_console: Console | None = None,
        log_override: logging.Logger | None = None,
    ) -> "SQLMeshController":
        return cls.setup_with_config(
            config=SQLMeshContextConfig(path=path, gateway=gateway),
            debug_console=debug_console,
            log_override=log_override,
        )

    @classmethod
    def setup_with_config(
        cls: type[T],
        config: SQLMeshContextConfig,
        debug_console: Console | None = None,
        log_override: logging.Logger | None = None,
    ) -> T:
        console = EventConsole(log_override=log_override)
        if debug_console:
            console = DebugEventConsole(debug_console)
        controller = cls(
            console=console,
            config=config,
            log_override=log_override,
        )
        return controller

    def __init__(
        self,
        config: SQLMeshContextConfig,
        console: EventConsole,
        log_override: logging.Logger | None = None,
    ) -> None:
        self.config = config
        self.console = console
        self.logger = log_override or logger
        self._context_open = False

    def set_logger(self, logger: logging.Logger) -> None:
        self.logger = logger

    def add_event_handler(self, handler: ConsoleEventHandler) -> str:
        handler_id: str = self.console.add_handler(handler)
        return handler_id

    def remove_event_handler(self, handler_id: str) -> None:
        self.console.remove_handler(handler_id)

    def _create_context(self) -> Context:
        options: dict[str, t.Any] = dict(
            paths=self.config.path,
            gateway=self.config.gateway,
        )
        if self.config.sqlmesh_config:
            options["config"] = self.config.sqlmesh_config
        set_console(self.console)
        return Context(**options)

    @contextmanager
    def instance(
        self, environment: str, component: str = "unknown"
    ) -> t.Generator[SQLMeshInstance, None, None]:
        self.logger.info(
            f"Opening sqlmesh instance for env={environment} component={component}"
        )
        if self._context_open:
            raise Exception("Only one sqlmesh instance at a time")

        context = self._create_context()
        self._context_open = True
        try:
            yield SQLMeshInstance(
                environment, self.console, self.config, context, self.logger
            )
        finally:
            self.logger.info(
                f"Closing sqlmesh instance for env={environment} component={component}"
            )
            self._context_open = False
            context.close()

    def run(
        self,
        environment: str,
        **run_options: t.Unpack[RunOptions],
    ) -> t.Generator[ConsoleEvent, None, None]:
        with self.instance(environment, "run") as mesh:
            yield from mesh.run(**run_options)

    def plan(
        self,
        environment: str,
        categorizer: SnapshotCategorizer | None,
        default_catalog: str | None,
        plan_options: PlanOptions,
    ) -> t.Generator[ConsoleEvent, None, None]:
        with self.instance(environment, "plan") as mesh:
            yield from mesh.plan(categorizer, default_catalog, **plan_options)

    def plan_and_run(
        self,
        environment: str,
        categorizer: SnapshotCategorizer | None = None,
        default_catalog: str | None = None,
        plan_and_run_options: PlanAndRunOptions | None = None,
    ) -> t.Generator[ConsoleEvent, None, None]:
        with self.instance(environment, "plan_and_run") as mesh:
            yield from mesh.plan_and_run(
                categorizer=categorizer,
                default_catalog=default_catalog,
                plan_and_run_options=plan_and_run_options,
            )
