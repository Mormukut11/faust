import inspect
import typing
from itertools import chain
from typing import Any, Awaitable, Callable, Iterable, List, Type, Union, cast
from mode import Service, ServiceT
from faust.exceptions import ImproperlyConfigured
from faust.types import AppT

if typing.TYPE_CHECKING:
    from .base import App
else:
    class App: ...  # noqa


class AppService(Service):
    """Service responsible for starting/stopping an application."""

    # The App() is created during module import and cannot subclass Service
    # directly as Service.__init__ creates the asyncio event loop, and
    # creating the event loop as a side effect of importing a module
    # is a dangerous practice (e.g., if you switch to uvloop after you can
    # end up in a situation where some services use the old loop).

    # To solve this we use ServiceProxy to split into App + AppService,
    # in a way such that the AppService is started lazily when first needed.

    _extra_service_instances: List[ServiceT] = None

    def __init__(self, app: App, **kwargs: Any) -> None:
        self.app: App = app
        super().__init__(loop=self.app.loop, **kwargs)

    def on_init_dependencies(self) -> Iterable[ServiceT]:
        # Client-Only: Boots up enough services to be able to
        # produce to topics and receive replies from topics.
        # XXX If we switch to socket RPC using routers we can remove this.
        if self.app.client_only:
            return self._components_client()
        # Server: Starts everything.
        return self._components_server()

    def _components_client(self) -> Iterable[ServiceT]:
        # Returns the components started when running in Client-Only mode.
        return cast(Iterable[ServiceT], (
            self.app.producer,
            self.app.consumer,
            self.app._reply_consumer,
            self.app.topics,
            self.app._fetcher,
        ))

    def _components_server(self) -> Iterable[ServiceT]:
        # Returns the components started when running normally (Server mode).
        # Note: has side effects: adds the monitor to the app's list of
        # sensors.

        # Add the main Monitor sensor.
        # The beacon is also reattached in case the monitor
        # was created by the user.
        self.app.monitor.beacon.reattach(self.beacon)
        self.app.monitor.loop = self.loop
        self.app.sensors.add(self.app.monitor)

        # Then return the list of "subservices",
        # those that'll be started when the app starts,
        # stopped when the app stops,
        # etc...
        return cast(
            Iterable[ServiceT],
            chain(
                # Sensors (Sensor): always start first and stop last.
                self.app.sensors,
                # Producer: always stop after Consumer.
                [self.app.producer],
                # Consumer: always stop after TopicConductor
                [self.app.consumer],
                # Leader Assignor (assignor.LeaderAssignor)
                [self.app._leader_assignor],
                # Reply Consumer (ReplyConsumer)
                [self.app._reply_consumer],
                # Agents (app.agents)
                self.app.agents.values(),
                # Topic Manager (app.TopicConductor))
                [self.app.topics],
                # Table Manager (app.TableManager)
                [self.app.tables],
                # Fetcher (transport.Fetcher)
                [self.app._fetcher],
            ),
        )

    async def on_first_start(self) -> None:
        self.app._create_directories()
        if not self.app.agents:
            # XXX I can imagine use cases where an app is useful
            #     without agents, but use this as more of an assertion
            #     to make sure agents are registered correctly. [ask]
            raise ImproperlyConfigured(
                'Attempting to start app that has no agents')
        await self.app.on_first_start()

    async def on_start(self) -> None:
        self.app.finalize()
        await self.app.on_start()

    async def on_started(self) -> None:
        # Wait for table recovery to complete.
        if not await self.wait_for_stopped(self.app.tables.recovery_completed):
            # Add all asyncio.Tasks, like timers, etc.
            await self.on_started_init_extra_tasks()

            # Start user-provided services.
            await self.on_started_init_extra_services()

            # Call the app-is-fully-started callback used by Worker
            # to print the "ready" message that signals to the user that
            # the worker is ready to start processing.
            if self.app.on_startup_finished:
                await self.app.on_startup_finished()

            await self.app.on_started()

    async def on_started_init_extra_tasks(self) -> None:
        for task in self.app._tasks:
            # pass app if decorated function takes argument
            target: Any
            if inspect.signature(task).parameters:
                target = cast(Callable[[AppT], Awaitable], task)(self.app)
            else:
                target = cast(Callable[[], Awaitable], task)()
            self.add_future(target)

    async def on_started_init_extra_services(self) -> None:
        if self._extra_service_instances is None:
            # instantiate the services added using the @app.service decorator.
            self._extra_service_instances = [
                self._prepare_subservice(s) for s in self.app._extra_services
            ]
            for service in self._extra_service_instances:
                # start the services now, or when the app is started.
                await self.add_runtime_dependency(service)

    def _prepare_subservice(
            self, service: Union[ServiceT, Type[ServiceT]]) -> ServiceT:
        if inspect.isclass(service):
            return service(loop=self.loop, beacon=self.beacon)
        return service

    async def on_stop(self) -> None:
        await self.app.on_stop()

    async def on_shutdown(self) -> None:
        await self.app.on_shutdown()

    async def on_restart(self) -> None:
        await self.app.on_restart()

    @property
    def label(self) -> str:
        return self.app.label

    @property
    def shortlabel(self) -> str:
        return self.app.shortlabel
