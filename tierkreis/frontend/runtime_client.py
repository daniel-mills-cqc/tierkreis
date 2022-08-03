"""Send requests to tierkreis server to execute a graph."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import wraps
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    Coroutine,
    Dict,
    Type,
    TypeVar,
    cast,
)

import betterproto
import keyring
from grpclib.client import Channel
from grpclib.events import SendRequest, listen

import tierkreis.core.protos.tierkreis.graph as pg
import tierkreis.core.protos.tierkreis.runtime as pr
import tierkreis.core.protos.tierkreis.signature as ps
from tierkreis.core.function import TierkreisFunction
from tierkreis.core.tierkreis_graph import TierkreisGraph
from tierkreis.core.types import TierkreisTypeErrors, TypeScheme
from tierkreis.core.values import IncompatiblePyType, StructValue, TierkreisValue

if TYPE_CHECKING:
    from betterproto.grpc.grpclib_client import ServiceStub

    from tierkreis.worker.worker import Worker


@dataclass
class RuntimeHTTPError(Exception):
    """Error while communicating with tierkreis server."""

    endpoint: str
    code: int
    content: str

    def __str__(self) -> str:
        return (
            f"Request to endpoint '{self.endpoint}'"
            f" failed with code {self.code}"
            f" and content '{self.content}'."
        )


@dataclass
class NamespaceDefs:
    functions: Dict[str, TierkreisFunction]
    aliases: Dict[str, TypeScheme]


RuntimeSignature = Dict[str, NamespaceDefs]


@dataclass(frozen=True)
class TaskHandle:
    """Handle for server task"""

    task_id: str


@dataclass
class InputConversionError(Exception):
    input_name: str
    value: Any

    def __str__(self) -> str:
        return (
            f"Input value with name {self.input_name} cannot be"
            "converted to a TierkreisValue."
        )


StubType = TypeVar("StubType", bound="ServiceStub")


class RuntimeClient:
    """Client for tierkreis server."""

    def __init__(self, channel: "Channel") -> None:
        self._channel = channel
        self._stubs: dict[str, ServiceStub] = {}

    def socket_address(self) -> str:
        return f"{self._channel._host}:{self._channel._port}"

    def _stub_gen(self, key: str, stub_t: Type[StubType]) -> StubType:
        if key not in self._stubs:
            self._stubs[key] = stub_t(self._channel)
        stub = self._stubs[key]
        assert isinstance(stub, stub_t)
        return stub

    @property
    def _signature_stub(self) -> ps.SignatureStub:
        return self._stub_gen("signature", ps.SignatureStub)

    @property
    def _runtime_stub(self) -> pr.RuntimeStub:
        return self._stub_gen("runtime", pr.RuntimeStub)

    @property
    def _type_stub(self) -> ps.TypeInferenceStub:
        return self._stub_gen("type", ps.TypeInferenceStub)

    async def get_signature(self) -> RuntimeSignature:
        return signature_from_proto(await self._signature_stub.list_functions())

    async def start_task(
        self, graph: TierkreisGraph, py_inputs: Dict[str, Any]
    ) -> TaskHandle:
        """
        Spawn a task that runs a graph and return the task's id. The id can
        later be used to await the completion of the task or cancel its
        execution.

        :param graph: The graph to run.
        :param inputs: The inputs to the graph as map from label to value.
        :return: The id of the running task.
        """
        inputs = {}
        for key, val in py_inputs.items():
            try:
                inputs[key] = TierkreisValue.from_python(val)
            except IncompatiblePyType as err:
                raise InputConversionError(key, val) from err

        decoded = await self._runtime_stub.run_task(
            graph=graph.to_proto(),
            inputs=pg.StructValue(map=StructValue(inputs).to_proto_dict()),
        )
        name, _ = betterproto.which_one_of(decoded, "result")

        if name == "task_id":
            return TaskHandle(decoded.task_id)
        raise TierkreisTypeErrors.from_proto(decoded.type_errors)

    async def list_tasks(
        self,
    ) -> Dict[TaskHandle, str]:
        """
        List the id and status for every task on the server.
        """

        decoded = await self._runtime_stub.list_tasks()
        result = {}

        for task in decoded.tasks:
            status_name, _ = betterproto.which_one_of(task, "status")

            if status_name is (None or ""):
                status_name = "running"

            result[TaskHandle(task.id)] = status_name

        return result

    async def await_task(self, task: TaskHandle) -> Dict[str, TierkreisValue]:
        """
        Await the completion of a task with a given id.

        :param task: The id of the task to wait for.
        :return: The result of the task.
        """

        decoded = await self._runtime_stub.await_task(id=task.task_id)
        status, status_value = betterproto.which_one_of(decoded.task, "status")

        if status != "success":
            raise RuntimeError(f"Task execution failed with message:\n{status_value}")
        assert status_value is not None
        return StructValue.from_proto_dict(status_value.map).values

    async def delete_task(self, task: TaskHandle):
        """
        Delete a task. Stops the task's execution if it is still running.

        :param task: The id of the task to delete.
        """
        await self._runtime_stub.delete_task(id=task.task_id)

    async def run_graph(
        self, graph: TierkreisGraph, py_inputs: Dict[str, Any]
    ) -> Dict[str, TierkreisValue]:
        """
        Run a graph and return results. This combines `start_task` and `await_task`.

        :param gb: Graph to run.
        :param inputs: Inputs to graph as map from label to value.
        :return: Outputs as map from label to value.
        """
        task = await self.start_task(graph, py_inputs)
        outputs = await self.await_task(task)
        return outputs

    def run_graph_block(
        self, graph: TierkreisGraph, py_inputs: Dict[str, Any]
    ) -> Dict[str, TierkreisValue]:
        async def _run(
            host: str,
            port: int,
        ):
            async with Channel(host, port) as channel:
                return await RuntimeClient(channel).run_graph(graph, py_inputs)

        return async_to_sync(_run)(self._channel._host, self._channel._port)

    async def type_check_graph(self, graph: TierkreisGraph) -> TierkreisGraph:
        value = TierkreisValue.from_python(graph).to_proto()

        response = await self._type_stub.infer_type(value=value)
        name, message = betterproto.which_one_of(response, "response")

        if name == "success":
            message = cast(ps.InferTypeSuccess, message)
            assert message.value.graph is not None
            return TierkreisValue.from_proto(message.value).to_python(TierkreisGraph)

        errors = cast(ps.TypeErrors, message)
        raise TierkreisTypeErrors.from_proto(errors)


def _gen_auth_injector(login: str, pwd: str) -> Callable[["SendRequest"], Coroutine]:
    async def _inject_auth(event: SendRequest) -> None:
        event.metadata["token"] = login
        event.metadata["key"] = pwd

    return _inject_auth


def with_runtime_client(worker: "Worker") -> Callable:
    from tierkreis.worker.worker import _KEYRING_SERVICE

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapped_func(*args, **kwargs):
            if worker.callback is None:
                raise RuntimeError(
                    "Callback address has not been extracted from request."
                )
            async with Channel(*worker.callback) as channel:
                _token = keyring.get_password(_KEYRING_SERVICE, "token")
                _key = keyring.get_password(_KEYRING_SERVICE, "key")
                if not (_token is None or _key is None):
                    listen(channel, SendRequest, _gen_auth_injector(_token, _key))
                args = (RuntimeClient(channel),) + args
                return await func(*args, **kwargs)

        try:
            wrapped_func.__annotations__.pop("client")
        except KeyError as e:
            raise RuntimeError(
                "Function to be wrapped must have"
                " 'channel: RuntimeClient' as first argument"
            ) from e
        return wrapped_func

    return decorator


def signature_from_proto(pr_sig: ps.ListFunctionsResponse) -> RuntimeSignature:
    namespaces: Dict[str, NamespaceDefs] = dict()

    for name, entry in pr_sig.functions.items():
        namespace, fname = name.split("/", 2)
        func = TierkreisFunction.from_proto(entry)
        if namespace in namespaces:
            namespaces[namespace].functions[fname] = func
        else:
            namespaces[namespace] = NamespaceDefs({fname: func}, {})

    for name, type_proto in pr_sig.aliases.items():
        namespace, alias_name = name.split("/", 2)
        type_ = TypeScheme.from_proto(type_proto)
        if namespace in namespaces:
            namespaces[namespace].aliases[alias_name] = type_
        else:
            namespaces[namespace] = NamespaceDefs({}, {alias_name: type_})

    return namespaces


class RuntimeLaunchFailed(Exception):
    """Starting server locally failed."""


CallableReturn = TypeVar("CallableReturn")


def async_to_sync(
    func: Callable[..., Awaitable[CallableReturn]]
) -> Callable[..., CallableReturn]:
    """
    Converts an asynchronous function into a synchronous one by running it
    on a new async event loop in a newly created thread.
    """

    @wraps(func)
    def sync(*args, **kwargs):
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(lambda: asyncio.run(func(*args, **kwargs)))
            return future.result()

    return sync
