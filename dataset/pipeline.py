""" Pipeline classes """
import traceback
import concurrent.futures as cf
import threading
#import multiprocessing as mpc
import asyncio
import logging
import queue as q
import numpy as np
try:
    import tensorflow as tf
except ImportError:
    pass

from .batch_base import BaseBatch
from .base import Baseset
from .exceptions import SkipBatchException
from .decorators import ModelDirectory


PIPELINE_ID = '#_pipeline'
JOIN_ID = '#_join'
MERGE_ID = '#_merge'
REBATCH_ID = '#_rebatch'
IMPORT_MODEL_ID = '#_import_model'
INIT_MODEL_ID = '#_init_model'


def mult_option(a, b):
    """ Multiply even if any arg is None """
    return a * b if a is not None and b is not None else a if a is not None else b


class Pipeline:
    """ Pipeline """
    def __init__(self, dataset=None, config=None, pipeline=None, proba=None, repeat=None):
        if pipeline is None:
            self.dataset = dataset
            self.config = config
            self._action_list = []
            self._variables = None
            self.delete_all_variables()
            self._lazy_run = None
        else:
            self.dataset = pipeline.dataset
            self.config = pipeline.config
            self._action_list = pipeline._action_list[:]  # pylint: disable=protected-access
            self._variables = pipeline._variables         # pylint: disable=protected-access
            if self.num_actions == 1:
                if proba is not None:
                    if self.get_last_action_repeat() is None:
                        self._action_list[-1]['proba'] = mult_option(proba, self.get_last_action_proba())
                elif repeat is not None:
                    if self.get_last_action_proba() is None:
                        self._action_list[-1]['repeat'] = mult_option(repeat, self.get_last_action_repeat())
            self._lazy_run = pipeline._lazy_run          # pylint: disable=protected-access

            if pipeline in ModelDirectory.models['static']:
                for each_model in ModelDirectory.models['static'][pipeline]:
                    ModelDirectory.import_model(each_model, pipeline, self)

        self._variables_lock = threading.Lock()
        self._tf_session = None

        self._stop_flag = False
        self._executor = None
        self._service_executor = None
        self._prefetch_count = None
        self._prefetch_queue = None
        self._batch_queue = None
        self._batch_generator = None
        self._rest_batch = None

        self.reset_iter()

    def __del__(self):
        """ Free pipeline resources """
        if ModelDirectory is not None:
            ModelDirectory.delete_all_models(self)

    def __enter__(self):
        """ Create a context and return an empty pipeline non-bound to any dataset """
        return type(self)()

    def __exit__(self, exc_type, exc_value, trback):
        pass

    @classmethod
    def from_pipeline(cls, pipeline, proba=None, repeat=None):
        """ Create a pipeline from another pipeline """
        if proba is None:
            if repeat is None:
                new_p = cls(pipeline=pipeline)
            else:
                if pipeline.num_actions == 1 and pipeline.get_last_action_proba() is None:
                    new_p = cls(pipeline=pipeline, repeat=repeat)
                else:
                    new_p = cls()
                    new_p.append_pipeline(pipeline, repeat=repeat)
        else:
            if pipeline.num_actions == 1 and pipeline.get_last_action_repeat() is None:
                new_p = cls(pipeline=pipeline, proba=proba)
            else:
                new_p = cls()
                new_p.append_pipeline(pipeline, proba=proba)
        return new_p

    @classmethod
    def concat(cls, pipe1, pipe2):
        """ Create a new pipeline concatenating two given pipelines """
        # pylint: disable=protected-access
        if pipe1.dataset != pipe2.dataset and pipe1.dataset is not None and pipe2.dataset is not None:
            raise ValueError("Cannot add pipelines with different datasets")

        new_p1 = cls.from_pipeline(pipe1)
        new_p2 = cls.from_pipeline(pipe2)
        new_p1._action_list += new_p2._action_list[:]
        new_p1._variables = {**pipe1._variables, **pipe2._variables}
        new_p1.dataset = pipe1.dataset or pipe2.dataset
        return new_p1

    def get_last_action_proba(self):
        """ Return a probability of the last action """
        return self._action_list[-1]['proba']

    def get_last_action_repeat(self):
        """ Return a repeat count of the last action """
        return self._action_list[-1]['repeat']

    def __add__(self, other):
        if not isinstance(other, Pipeline):
            raise TypeError("Both operands should be Pipelines")
        if other.num_actions > 0:
            return self.concat(self, other)
        else:
            return self

    def __matmul__(self, other):
        if self.num_actions == 0:
            raise ValueError("Cannot add probability to an empty pipeline")
        if not isinstance(other, float) and other not in [0, 1]:
            raise TypeError("Probability should be float or 0 or 1")
        other = float(other) if int(other) != 1 else None
        return self.from_pipeline(self, proba=other)

    def __mul__(self, other):
        if other < 0:
            raise ValueError("Repeat count cannot be negative. Use as pipeline * positive_number")
        elif isinstance(other, float):
            raise ValueError("Repeat count cannot be float. Use as pipeline * integer")
        elif isinstance(other, int):
            new_p = self.from_pipeline(self, repeat=other)
        return new_p

    def __lshift__(self, other):
        if not isinstance(other, Baseset):
            raise TypeError("Pipelines might take only Datasets. Use as pipeline << dataset")
        new_p = self.from_pipeline(self)
        new_p.dataset = other
        return new_p

    @staticmethod
    def _is_batch_method(name, cls=None):
        cls = BaseBatch if cls is None else cls
        if hasattr(cls, name) and callable(getattr(cls, name)):
            return True
        else:
            return any(Pipeline._is_batch_method(name, subcls) for subcls in cls.__subclasses__())

    def __getattr__(self, name):
        """ Check if an unknown attr is an action from some batch class """
        if self._is_batch_method(name):
            self._action_list.append({'name': name})
            return self.append_action
        else:
            raise AttributeError("%s not found in class %s" % (name, self.__class__.__name__))

    @property
    def num_actions(self):
        """ Return index length """
        return len(self._action_list)

    def append_action(self, *args, **kwargs):
        """ Add new action to the log of future actions """
        self._action_list[-1].update({'args': args, 'kwargs': kwargs, 'proba': None, 'repeat': None})
        new_p = self.from_pipeline(self)
        self._action_list = self._action_list[:-1]
        return new_p

    def append_pipeline(self, pipeline, proba=None, repeat=None):
        """ Add a nested pipeline to the log of future actions """
        self._action_list.append({'name': PIPELINE_ID, 'pipeline': pipeline,
                                  'proba': proba, 'repeat': repeat})

    def __getstate__(self):
        return {'dataset': self.dataset, 'action_list': self._action_list, 'variables': self._variables}

    def __setstate__(self, state):
        self.dataset = state['dataset']
        self._action_list = state['action_list']
        self._variables = state['variables']

    @property
    def index(self):
        """ Return index of the source dataset """
        return self.dataset.index

    @property
    def indices(self):
        """ Return the sequence of indices of the source dataset """
        return self.index.indices

    def __len__(self):
        """ Return index length """
        return len(self.index)

    def has_variable(self, name):
        """ Check if a variable exists
        Args:
            name: string - a name of the variable
        Return:
            True if the variable exists
        """
        return name in self._variables

    def get_variable(self, name, default=None, init=None, init_on_each_run=False):
        """ Return a variable value
        If the variable does not exists, it will be created and initialized (see `init_variable` below)
        Args:
            name: string - a name of the variable
            default - a value for the variable if it does not exists
            init: callable - a function which returns the default value
            init_on_each_run: bool - whether to initialize the variable before each run / gen_batch
        Return:
            a value of the variable
        """
        if name not in self._variables:
            self.init_variable(name, default, init, init_on_each_run)
        var = self._variables.get(name)
        return var.get('value', default)

    def init_variable(self, name, default=None, init=None, init_on_each_run=False):
        """ Create a variable if not exists.
        If the variable exists, does nothing.
        Args:
            name: string - a name of the variable
            default - an initial value for the variable
            init: callable - a function which returns the default value
            init_on_each_run: bool - whether to initialize the variable before each run / gen_batch
        Return:
            self - in order to use it in the pipeline chains

        Examples:
            pp = dataset.p.
                    .init_variable("loss_history", init=list, init_on_each_run=True)
                    .init_variable("accuracy", default=0)
                    .load('/some/path', fmt='blosc')
                    .train_resnet()
        """
        if name not in self._variables:
            with self._variables_lock:
                if name not in self._variables:
                    self._variables[name] = dict(default=default, init=init, init_on_each_run=init_on_each_run)
                    self.set_variable(name, default if init is None else init())
        return self

    def init_variables(self, variables):
        """ Create several variables
        Args:
            vars: dict - key: string - a variable name,
                         value: dict -  a variable value and params (see `init_variable`)
        Return:
            self - in order to use it in the pipeline chains

        Examples:
            pp = dataset.p.
                    .init_variables({"loss_history": dict(init=list, init_on_each_run=True),
                                     "accuracy", dict(default=0)})
                    .load('/some/path', fmt='blosc')
                    .train_resnet()
        """
        for name, var in variables.items():
            self.init_variable(name, **var)
        return self

    def _init_variables_before_run(self):
        for name, var in self._variables.items():
            if var['init_on_each_run']:
                self.set_variable(name, var['default'] if var['init'] is None else var['init']())

    def set_variable(self, name, value):
        """ Set a variable value
        If the variable does not exists, it will be created, however, the warning will be displayed that
        the variable was not initialized.
        Args:
            name: string - a name of the variable
            value - a value for the variable
        Return:
            self - in order to use it in the pipeline chains
        """
        if name not in self._variables:
            logging.warning("Pipeline variable '%s' was not initialized", name)
        self._variables[name].update({'value': value})
        return self

    def assign_variable(self, name, value):
        """ Assign a value to a variable
        Same as `set_variable(name, value)`.
        """
        return self.set_variable(name, value)

    def delete_variable(self, name):
        """ Delete a variable
        If the variable does not exists, the warning will be issued.
        Args:
            name: string - a name of the variable
                  iterable - several variable names
        Return:
            self - in order to use it in the pipeline chains
        """
        if name not in self._variables:
            logging.warning("Pipeline variable '%s' does not exist", name)
        else:
            if isinstance(name, str):
                self._variables.pop(name)
            else:
                for var in name:
                    self._variables.pop(var)
        return self

    def del_variable(self, name):
        """ Delete a variable
        Same as `delete_variable(name)`
        """
        return self.delete_variable(name)

    def delete_all_variables(self):
        """ Delete all variables """
        self._variables = dict()

    def get_model_by_name(self, model_name):
        """ Get a model specification by its name """
        models = ModelDirectory.get_model_by_name(model_name, pipeline=self)
        return models

    @staticmethod
    def _get_action_method(batch, name):
        if hasattr(batch, name):
            attr = getattr(batch, name)
            if attr.__self__ == batch:
                # action decorator with arguments
                # attr is bounded to the batch
                action_method = attr
                action_attr = attr
            else:
                # action decorator wihout arguments
                action_method = attr
                action_attr = attr.__self__

            if callable(action_attr):
                if hasattr(action_attr, 'action'):
                    action_spec = getattr(action_attr, 'action')
                else:
                    raise ValueError("Method %s is not marked with @action decorator" % name)
            else:
                raise TypeError("%s is not a method" % name)
        else:
            raise AttributeError("Method '%s' has not been found in the %s class" % (name, type(batch).__name__))
        return action_method, action_spec

    def _exec_one_action(self, batch, action, args, kwargs):
        if self._needs_exec(action):
            for _ in range(action['repeat'] or 1):
                batch.pipeline = self
                action_method, _ = self._get_action_method(batch, action['name'])
                batch = action_method(*args, **kwargs)
                batch.pipeline = self
        return batch

    def _exec_nested_pipeline(self, batch, action):
        if self._needs_exec(action):
            for _ in range(action['repeat'] or 1):
                batch = self._exec_all_actions(batch, action['pipeline']._action_list)  # pylint: disable=protected-access
        return batch

    def _exec_all_actions(self, batch, action_list=None):
        join_batches = None
        action_list = action_list or self._action_list
        for _action in action_list:
            if _action['name'] in [JOIN_ID, MERGE_ID]:
                join_batches = []
                for pipe in _action['pipelines']:   # pylint: disable=not-an-iterable
                    if _action['mode'] == 'i':
                        jbatch = pipe.create_batch(batch.index)
                    elif _action['mode'] == 'n':
                        jbatch = pipe.next_batch()
                    join_batches.append(jbatch)

                if _action['name'] == MERGE_ID:
                    if _action['merge_fn'] is None:
                        batch, _ = batch.merge([batch] + join_batches)
                    else:
                        batch, _ = _action['merge_fn']([batch] + join_batches)
                    join_batches = None
            elif _action['name'] == REBATCH_ID:
                pass
            elif _action['name'] == PIPELINE_ID:
                batch = self._exec_nested_pipeline(batch, _action)
            elif _action['name'] == IMPORT_MODEL_ID:
                ModelDirectory.import_model(_action['model_name'], _action['pipeline'], self)
            elif _action['name'] == INIT_MODEL_ID:
                # ModelDirectory.init_model(_action['model_name'], pipeline=self, batch=batch)
                pass
            else:
                if join_batches is None:
                    _action_args = _action['args']
                else:
                    _action_args = tuple([tuple(join_batches), *_action['args']])
                    join_batches = None

                batch = self._exec_one_action(batch, _action, _action_args, _action['kwargs'])

                if 'tf_queue' in _action:
                    self._put_batch_into_tf_queue(batch, _action)
        return batch

    def _needs_exec(self, action):
        if action['proba'] is None:
            return True
        else:
            return np.random.binomial(1, action['proba']) == 1

    def _exec(self, batch, new_loop=False):
        if new_loop:
            asyncio.set_event_loop(asyncio.new_event_loop())
        batch.pipeline = self
        batch_res = self._exec_all_actions(batch)
        batch_res.pipeline = self
        return batch_res

    def init_model(self, model_name, config=None):
        """ Initialize a static model
        Args:
            model_name: string - a name of the model to import
            config - configurations parameters
        """
        ModelDirectory.init_model(model_name, pipeline=self, config=config)
        return self
        #self._action_list.append({'name': INIT_MODEL_ID, 'model_name': model_name, 'config': config})
        #return self.append_action()

    def import_model(self, model_name, pipeline):
        """ Import a model from another pipeline
        Args:
            model_name: string - a name of the model to import
            pipeline - a pipeline that holds a model
        """
        self._action_list.append({'name': IMPORT_MODEL_ID, 'model_name': model_name, 'pipeline': pipeline})
        return self.append_action()

    def join(self, *pipelines):
        """ Join pipelines
        Args:
            one or several pipelines
        """
        self._action_list.append({'name': JOIN_ID, 'pipelines': pipelines, 'mode': 'i'})
        return self.append_action()

    def merge(self, *pipelines, merge_fn=None):
        """ Merge pipelines """
        self._action_list.append({'name': MERGE_ID, 'pipelines': pipelines,    # pylint: disable=protected-access
                                  'mode': 'n', 'merge_fn': merge_fn})
        return self.append_action()

    def rebatch(self, batch_size, merge_fn=None):
        """ Set the output batch size """
        new_p = type(self)(self.dataset)
        new_p._action_list.append({'name': REBATCH_ID, 'batch_size': batch_size,  # pylint: disable=protected-access
                                   'pipeline': self, 'merge_fn': merge_fn})
        return new_p.append_action()

    def put_into_tf_queue(self, session=None, queue=None, get_tensor=None):
        """ Insert a tensorflow queue after the action"""
        if len(self._action_list) > 0:
            action = dict()
            action['tf_session'] = session
            action['tf_queue'] = queue
            action['get_tensor'] = get_tensor
            action['tf_enqueue_op'] = None
            action['tf_placeholders'] = None
            action['tf_action_lock'] = threading.Lock()
            self._action_list[-1].update(action)
        else:
            raise RuntimeError('tf_queue should be precedeed by at least one action')
        return self

    @staticmethod
    def _get_dtypes(tensors=None, action=None):
        if tensors:
            return [tensor.dtype for tensor in tensors]
        else:
            return [placeholder.dtype for placeholder in action['tf_placeholders']]

    def _create_tf_queue(self, tensors, action):
        if action['tf_session'] is None:
            action['tf_session'] = self._tf_session
        if action['tf_session'] is None:
            raise ValueError("Tensorflow session cannot be None")
        maxsize = 1 if self._prefetch_queue is None else self._prefetch_queue.maxsize
        with action['tf_session'].graph.as_default():
            action['tf_queue'] = tf.FIFOQueue(capacity=maxsize, dtypes=self._get_dtypes(tensors, action))

    @staticmethod
    def _get_tf_placeholders(tensors, action):
        tensors = tensors if isinstance(tensors, tuple) else tuple([tensors])
        with action['tf_session'].graph.as_default():
            placeholders = [tf.placeholder(dtype=tensor.dtype) for tensor in tensors]
        return placeholders

    @staticmethod
    def _get_tensor(batch, action):
        if action['get_tensor'] is None:
            return batch.data
        else:
            return action['get_tensor'](batch)

    def _put_batch_into_tf_queue(self, batch, action):
        tensors = self._get_tensor(batch, action)
        tensors = tensors if isinstance(tensors, tuple) else tuple([tensors])
        if action['tf_queue'] is None:
            with action['tf_action_lock']:
                if action['tf_queue'] is None:
                    self._create_tf_queue(tensors, action)
        if action['tf_enqueue_op'] is None:
            with action['tf_action_lock']:
                if action['tf_enqueue_op'] is None:
                    action['tf_placeholders'] = self._get_tf_placeholders(tensors, action)
                    action['tf_enqueue_op'] = action['tf_queue'].enqueue(action['tf_placeholders'])
        action['tf_session'].run(action['tf_enqueue_op'], feed_dict=dict(zip(action['tf_placeholders'], tensors)))


    def _put_batches_into_queue(self, gen_batch):
        while not self._stop_flag:
            self._prefetch_count.put(1, block=True)
            try:
                batch = next(gen_batch)
            except StopIteration:
                break
            else:
                future = self._executor.submit(self._exec, batch, new_loop=True)
                self._prefetch_queue.put(future, block=True)
        self._prefetch_queue.put(None, block=True)

    def _run_batches_from_queue(self):
        skip_batch = False
        while not self._stop_flag:
            future = self._prefetch_queue.get(block=True)
            if future is None:
                self._prefetch_queue.task_done()
                self._batch_queue.put(None)
                break
            else:
                try:
                    batch = future.result()
                except SkipBatchException:
                    skip_batch = True
                except Exception:   # pylint: disable=broad-except
                    exc = future.exception()
                    print("Exception in a thread:", exc)
                    traceback.print_tb(exc.__traceback__)
                finally:
                    if not skip_batch:
                        self._batch_queue.put(batch, block=True)
                        skip_batch = False
                    self._prefetch_queue.task_done()
        return None

    def reset_iter(self):
        """ Clear all iteration metadata in order to start iterating from scratch """
        def _clear_queue(queue):
            if queue is not None:
                while not queue.empty():
                    queue.get(block=True)
                    queue.task_done()

        def _stop_executor(executor):
            if executor is not None:
                executor.shutdown()

        self._stop_flag = True

        _clear_queue(self._prefetch_queue)
        _clear_queue(self._batch_queue)
        _clear_queue(self._prefetch_count)

        _stop_executor(self._executor)
        _stop_executor(self._service_executor)

        self._executor = None
        self._service_executor = None
        self._prefetch_count = None
        self._prefetch_queue = None
        self._batch_queue = None
        self._batch_generator = None
        self._rest_batch = None

        if self.dataset is not None:
            self.dataset.reset_iter()

        self._init_variables_before_run()


    def gen_rebatch(self, *args, **kwargs):
        """ Generate batches for rebatch operation """
        _action = self._action_list[0]
        self._rest_batch = None
        while True:
            if self._rest_batch is None:
                cur_len = 0
                batches = []
            else:
                cur_len = len(self._rest_batch)
                batches = [self._rest_batch]
                self._rest_batch = None
            while cur_len < _action['batch_size']:
                try:
                    new_batch = _action['pipeline'].next_batch(*args, **kwargs)
                except StopIteration:
                    break
                else:
                    batches.append(new_batch)
                    cur_len += len(new_batch)
            if len(batches) == 0:
                break
            else:
                if _action['merge_fn'] is None:
                    batch, self._rest_batch = batches[0].merge(batches, batch_size=_action['batch_size'])
                else:
                    batch, self._rest_batch = _action['merge_fn'](batches, batch_size=_action['batch_size'])
                yield batch


    def gen_batch(self, batch_size, shuffle=True, n_epochs=1, drop_last=False, prefetch=0, *args, **kwargs):
        """ Generate batches """
        target = kwargs.pop('target', 'threads')
        self._tf_session = kwargs.pop('tf_session', None)

        if len(self._action_list) > 0 and self._action_list[0]['name'] == REBATCH_ID:
            batch_generator = self.gen_rebatch(batch_size, shuffle, n_epochs, drop_last, prefetch, *args, **kwargs)
        else:
            batch_generator = self.dataset.gen_batch(batch_size, shuffle, n_epochs, drop_last, *args, **kwargs)

        if prefetch > 0:
            # pool cannot have more than 63 workers
            prefetch = min(prefetch, 62)

            if target in ['threads', 't']:
                self._executor = cf.ThreadPoolExecutor(max_workers=prefetch + 1)
            elif target in ['mpc', 'm']:
                self._executor = cf.ProcessPoolExecutor(max_workers=prefetch + 1)   # pylint: disable=redefined-variable-type
            else:
                raise ValueError("target should be one of ['threads', 'mpc']")

            self._stop_flag = False
            self._prefetch_count = q.Queue(maxsize=prefetch + 1)
            self._prefetch_queue = q.Queue(maxsize=prefetch)
            self._batch_queue = q.Queue(maxsize=1)
            self._service_executor = cf.ThreadPoolExecutor(max_workers=2)
            self._service_executor.submit(self._put_batches_into_queue, batch_generator)
            self._service_executor.submit(self._run_batches_from_queue)

            while not self._stop_flag:
                batch_res = self._batch_queue.get(block=True)
                self._batch_queue.task_done()
                if batch_res is not None:
                    yield batch_res
                    self._prefetch_count.get(block=True)
                    self._prefetch_count.task_done()
                else:
                    self._stop_flag = True
        else:
            for batch in batch_generator:
                try:
                    batch_res = self._exec(batch)
                except SkipBatchException:
                    pass
                else:
                    yield batch_res

    def create_batch(self, batch_index, *args, **kwargs):
        """ Create a new batch by given indices and execute all previous lazy actions """
        batch = self.dataset.create_batch(batch_index, *args, **kwargs)
        batch_res = self._exec(batch)
        return batch_res

    def next_batch(self, *args, **kwargs):
        """ Get the next batch and execute all previous lazy actions
        next_batch(self, batch_size, shuffle=True, n_epochs=1, drop_last=False, prefetch=0, *args, **kwargs):
        """
        if len(args) == 0 and len(kwargs) == 0:
            if self._lazy_run is None:
                raise RuntimeError("next_batch without arguments requires a lazy run at the end of the pipeline")
            batch_res = self.next_batch(*self._lazy_run[0], **self._lazy_run[1])
        elif True or kwargs.get('prefetch', 0) > 0:
            if self._batch_generator is None:
                self._lazy_run = args, kwargs
                self._batch_generator = self.gen_batch(*args, **kwargs)
            batch_res = next(self._batch_generator)
        else:
            self._lazy_run = args, kwargs
            _kwargs = kwargs.copy()
            # target is not used here, but people tend to forget removing it when set prefetch to 0
            _ = _kwargs.pop('target', 'threads')
            # prefetch could be 0
            _ = _kwargs.pop('prefetch', 0)
            batch_res = None
            while batch_res is None:
                batch_index = self.index.next_batch(*args, **_kwargs)
                try:
                    batch_res = self.create_batch(batch_index, **_kwargs)
                except SkipBatchException:
                    pass
        return batch_res

    def run(self, *args, **kwargs):
        """ Execute all lazy actions for each batch in the dataset
        run(self, batch_size, shuffle=True, n_epochs=1, drop_last=False, prefetch=0, *args, **kwargs):
        """
        if kwargs.pop('lazy', False):
            self._lazy_run = args, kwargs
        else:
            if len(args) == 0 and len(kwargs) == 0:
                args, kwargs = self._lazy_run
            for _ in self.gen_batch(*args, **kwargs):
                pass
        return self
