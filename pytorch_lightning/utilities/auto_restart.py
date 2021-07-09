# Copyright The PyTorch Lightning team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any, Dict, Generator, Iterator, List, Optional, Union

from torch.utils.data import get_worker_info, Sampler
from torch.utils.data.dataloader import _MultiProcessingDataLoaderIter, DataLoader, IterableDataset

from pytorch_lightning.utilities.apply_func import _is_dataclass_instance, _is_namedtuple
from pytorch_lightning.utilities.enums import AutoRestartBatchKeys


class FastForwardSampler(Sampler):
    """
    This FastForwardSampler wraps a :class:`torch.utils.data.Sampler` and records the number of iterations
    performed during an epoch. It maintains a state, saved with :meth:`state_dict`, that can be reloaded with
    :meth:`load_state_dict`. If the sampler is used in a multiprocessing context, the ``FastForwardSampler`` will record
    the state of the current worker.

    When reloading, the ``FastForwardSampler`` will "fast-forward" the wrapped sampler by iterating through all the
    samples seen in the last iterations (for the current worker).
    """

    def __init__(self, sampler: Union[Sampler, Generator]) -> None:
        super().__init__(data_source=None)
        self._sampler = sampler
        self.restarting: bool = False
        self._current_iteration = 0
        self._dataloader_batch_size: Optional[int] = None
        self._cached_state_dict: Optional[Dict[str, Any]] = None

    def __getattr__(self, key: str) -> Any:
        if key in self.__dict__:
            return self.__dict__[key]
        return getattr(self._sampler, key, None)

    def setup(self, dataloader_batch_size: Optional[int] = None) -> None:
        """
        Setup the ``FastForwardSampler``.
        This is required only when the provided dataset subclassed :class:`torch.utils.data.Dataset`.
        """
        self._dataloader_batch_size = dataloader_batch_size

    @property
    def worker_id(self) -> int:
        worker_info = get_worker_info()
        return worker_info.id if worker_info else 0

    def __iter__(self) -> Iterator[Any]:
        # split restart logic to avoid user with tempering with "fast-forwarding"

        if not self.restarting:
            for batch in self._sampler:
                self._current_iteration += 1
                yield batch

        else:
            for i, batch in enumerate(self._sampler):

                # the `state dict` was cached as workers were available before.
                if self._cached_state_dict is not None and self.worker_id in self._cached_state_dict:

                    # reload the current state dict
                    self.load_state_dict(self._cached_state_dict, workers_initialized=True)
                    self._cached_state_dict = None

                # when the current index matching the current_iteration, we have "fast forwarded" the sampler.
                if self._current_iteration <= i:
                    self._current_iteration += 1
                    yield batch

        self._current_iteration = 0

    def __len__(self) -> int:
        return len(self.sampler)

    def _compute_current_iteration(self, num_batches_processed: Optional[int] = None) -> int:
        """
        This function is used to compute the effective iteration.
        As DataLoader can perform ``prefecthing`` or training can fail while processing a batch,
        the current iteration needs to be computed using the ``num_batches_processed`` processed information.
        """
        if num_batches_processed is not None:
            current_iteration = num_batches_processed
        else:
            current_iteration = self._current_iteration

        if self._dataloader_batch_size:
            current_iteration *= self._dataloader_batch_size

        return current_iteration

    def state_dict(self, num_batches_processed: Optional[int] = None) -> Dict[int, Dict[str, int]]:
        """ Returns the state of the sampler in the current worker. The worker id indexes the state dict."""
        return {self.worker_id: {"current_iteration": self._compute_current_iteration(num_batches_processed)}}

    def load_state_dict(self, state_dict: Dict[int, Any], workers_initialized: bool = False) -> None:
        """
        Loads the saved state for the wrapped sampler.
        If the ``state_dict`` contains multiple states, it means there were multiple workers.
        The state will be cached and fully reloaded (fast-forward) the first time :meth:`__iter__` is called.
        """
        # as workers aren't available, the ``state_dict``` is cached until workers are made available.
        if len(state_dict) > 1 and not workers_initialized:
            self._cached_state_dict = deepcopy(state_dict)
            self.restarting = self._cached_state_dict[self.worker_id]["current_iteration"] > 0
            return
        self._current_iteration = state_dict[self.worker_id]["current_iteration"]
        self.restarting = self._current_iteration > 0


class CaptureIterableDataset(IterableDataset):
    """
    The ``CaptureIterableDataset`` is used to wrap an :class:`torch.utils.data.IterableDataset`.
    On ``__iter__`` function call,   the ``CaptureIterableDataset`` will wrap the wrapped dataset
        generators into ``FastForwardSampler`` to keep track of progress.
    On ``__next__`` function call, the ``CaptureIterableDataset`` will return a dictionary containing
        user data and metadata containing the ``FastForwardSampler`` samplers state_dict.
    """

    def __init__(self, dataset: IterableDataset, initial_seed: Optional[int] = None) -> None:
        super().__init__()
        self.dataset = deepcopy(dataset)
        self.state_dict: Optional[Dict[int, Any]] = None
        self.initial_seed = initial_seed
        self.samplers: Optional[Dict[str, FastForwardSampler]] = None

    @property
    def sampler(self) -> Sampler:
        return self.dataset.sampler

    def load_state_dict(self, state_dict: Dict[int, Any]) -> None:
        self.state_dict = deepcopy(state_dict)

    def _wrap_generator_samplers(self) -> None:
        if self.samplers is not None:
            return

        self.samplers = {}

        # access wrapped dataset attributes
        dataset_dict = self.dataset.__dict__

        # create a tuple of sampler names
        samplers_names = tuple(v.__class__.__name__ for k, v in dataset_dict.items() if isinstance(v, Sampler))

        # create a dictionary of generator present within the dataset attributes
        dataset_sampler_generators = {k: v for k, v in dataset_dict.items() if isinstance(v, (Generator, Iterator))}

        # iterate over the generator. If a generator was created from a ``Sampler```,
        # it will be wrapped into a ``FastForwardSampler``.
        for (generator_attr_name, generator) in dataset_sampler_generators.items():

            if isinstance(generator, Sampler):
                continue

            # used to handle a weird behaviour from PyTorch 1.6
            # where the sampler is converted to a list_iterator
            is_legacy = False

            if isinstance(generator, Generator):
                # Generator name have the  the form `SamplerName.__iter__`
                generator_name = generator.__qualname__.split('.')[0]
            else:
                # assume the retrieved iterator is coming from sampler.
                is_legacy = True

            # validate the base generator name matches a sampler name.
            if is_legacy or any(sampler_name == generator_name for sampler_name in samplers_names):

                # wrap the generator into a ``FastForwardSampler``
                sampler = FastForwardSampler(generator)

                # if ``CaptureIterableDataset`` was available, the sampler should reload its own state.
                if self.state_dict is not None:
                    sampler.load_state_dict(self.state_dict[generator_attr_name])

                # store the samplers
                self.samplers[generator_attr_name] = sampler

                # replace generator with the generator from the ``FastForwardSampler``.
                dataset_dict[generator_attr_name] = iter(sampler)

        # reset state dict.
        self.state_dict = None

    def reset_on_epoch(self) -> None:
        self.state_dict = None

    def __iter__(self) -> Iterator:
        # create a generator from the wrapped Iterative Dataset
        # if the dataset contained samplers, they will be transformers into generators
        self.iter_data = iter(self.dataset)

        # wrap any generator associated to a Sampler into a ``FastForwardSampler``.
        self._wrap_generator_samplers()
        return self

    def __next__(self) -> Dict[str, Any]:
        # fetch next data
        data = next(self.iter_data)

        # create current samplers state_dict
        worker_info = get_worker_info()
        state_dicts = {"id": worker_info.id if worker_info is not None else 0}
        state_dicts.update({k: v.state_dict() for k, v in self.samplers.items()})

        # return both current data and samplers ``state_dict``.
        return {"data": data, AutoRestartBatchKeys.PL_SAMPLERS: state_dicts}

    @staticmethod
    def _sanetize_batch_from_sampler_state(data: Any, state_dict: List):
        elem_type = type(data)

        # Recursively apply to collection items
        if isinstance(data, Mapping):
            out = []
            for k, v in data.items():
                if k == AutoRestartBatchKeys.PL_SAMPLERS:
                    iterable_dataset_state_dict = {}
                    batch_worker_id = v.pop("id")
                    worker_id = batch_worker_id[-1].item()
                    for sampler_name, sampler_state_dict in v.items():
                        iterable_dataset_state_dict[sampler_name] = {
                            worker_id: {
                                "current_iteration": sampler_state_dict[worker_id]["current_iteration"][-1].item()
                            }
                        }
                    state_dict.append(iterable_dataset_state_dict)
                    return data["data"]
                out.append((k, CaptureIterableDataset._sanetize_batch_from_sampler_state(v, state_dict)))
            return elem_type(OrderedDict(out))

        is_namedtuple = _is_namedtuple(data)
        is_sequence = isinstance(data, Sequence) and not isinstance(data, str)
        if is_namedtuple or is_sequence:
            out = []
            for d in data:
                v = CaptureIterableDataset._sanetize_batch_from_sampler_state(d, state_dict)
                out.append(v)
            return elem_type(*out) if is_namedtuple else elem_type(out)

        if _is_dataclass_instance(data):
            out = dict()
            for field in data.__dataclass_fields__:
                v = CaptureIterableDataset._sanetize_batch_from_sampler_state(getattr(data, field), state_dict)
                out[field] = v
            return elem_type(**out)

        return data

    @staticmethod
    def convert_batch_into_state_dict(batch) -> List[Dict[int, Any]]:
        """
        This function is used to convert a batch into a state_dict
        """
        samplers_state_dict = []

        batch = CaptureIterableDataset._sanetize_batch_from_sampler_state(batch, samplers_state_dict)

        return batch, samplers_state_dict


def _find_next_worker_id(iter, state_dict: Dict[str, Any], num_workers: int):
    if isinstance(iter, _MultiProcessingDataLoaderIter):
        next_worker = (next(iter._worker_queue_idx_cycle)) % num_workers
        previous_worker = (next_worker - 1) % num_workers
        while next(iter._worker_queue_idx_cycle) != previous_worker:
            pass
    else:
        previous_worker = None

    state_dict.update({"num_workers": iter._num_workers, "previous_worker": previous_worker})


def find_fast_forward_samplers(dataloader: DataLoader) -> Optional[FastForwardSampler]:
    if isinstance(dataloader.sampler, FastForwardSampler):
        return dataloader.sampler

    elif isinstance(dataloader.batch_sampler, FastForwardSampler):
        return dataloader.batch_sampler


def fetch_previous_worker_state_dict(iter: Iterator, out: List):
    num_workers = getattr(iter, "_num_workers", 0)
    if isinstance(iter, _MultiProcessingDataLoaderIter):
        next_worker = (next(iter._worker_queue_idx_cycle)) % num_workers
        previous_worker = (next_worker - 1) % num_workers
        while next(iter._worker_queue_idx_cycle) != previous_worker:
            pass
    else:
        previous_worker = None

    out.append({"num_workers": num_workers, "previous_worker": previous_worker})


def fetch_fast_forward_samplers_state_dict(dataloader: DataLoader, out: List, count: int):
    fast_forward_samplers = find_fast_forward_samplers(dataloader)

    if fast_forward_samplers is not None:
        try:
            out[count]
        except IndexError:
            out.append({})
        out[count]["sampler"] = fast_forward_samplers.state_dict()


def cycle_to_next_worker(iter: Iterator, state_dict: List[Dict[str, Any]], count: int):
    current = state_dict[count]
    num_workers = iter._num_workers
    assert current["num_workers"] == num_workers
    if isinstance(iter, _MultiProcessingDataLoaderIter):
        # move back to 0
        while next(iter._worker_queue_idx_cycle) != 0:
            pass
        # increment previous worker
        for _ in range(current["previous_worker"] - 1):
            next(iter._worker_queue_idx_cycle)
        iter._reset = iter._ori_reset
        iter._reset(current["loader"], first_iter=True)

    count += 1


def fast_forward_sampler_load_state_dict(dataloader, state_dict: List[Dict[str, Any]], count: int):
    current_state_dict = state_dict[count]["sampler"]
    fast_forward_samplers = find_fast_forward_samplers(dataloader)

    if fast_forward_samplers is not None:
        fast_forward_samplers.load_state_dict(current_state_dict)
        count += 1


def dataloader_to_state_dict(dataloader: DataLoader, iter: Iterator) -> List[Dict[str, Any]]:
    out = []
    if iter is not None:
        fetch_previous_worker_state_dict(iter, out)

    count = 0
    fetch_fast_forward_samplers_state_dict(dataloader, out, count)
    return out


def dataloader_load_state_dict(dataloader: DataLoader, state_dict: List[Dict[str, Any]]) -> None:
    fast_forward_sampler = find_fast_forward_samplers(dataloader)

    if isinstance(fast_forward_sampler, Sampler):
        fast_forward_sampler.load_state_dict(state_dict[0]["sampler"])
