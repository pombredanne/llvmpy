'''
This file implements the code-generator for parallel-vectorize.

ParallelUFunc is the platform independent base class for generating
the thread dispatcher.  This thread dispatcher launches threads
that execute the generated function of UFuncCore.
UFuncCore is subclassed to specialize for the input/output types.
The actual workload is invoked inside the function generated by UFuncCore.
UFuncCore also defines a work-stealing mechanism that allows idle threads
to steal works from other threads.
'''

from llvm.core import *
from llvm.passes import *

from llvm_cbuilder import *
import llvm_cbuilder.shortnames as C

class WorkQueue(CStruct):
    '''structure for workqueue for parallel-ufunc.
    '''

    _fields_ = [
        ('next', C.intp),  # next index of work item
        ('last', C.intp),  # last index of work item (exlusive)
        ('lock', C.int),   # for locking the workqueue
    ]


    def Lock(self):
        '''inline the lock procedure.
        '''
        with self.parent.loop() as loop:
            with loop.condition() as setcond:
                unlocked = self.parent.constant(self.lock.type, 0)
                locked = self.parent.constant(self.lock.type, 1)

                res = self.lock.reference().atomic_cmpxchg(unlocked, locked,
                                               ordering='acquire')
                setcond( res != unlocked )

            with loop.body():
                pass

    def Unlock(self):
        '''inline the unlock procedure.
        '''
        unlocked = self.parent.constant(self.lock.type, 0)
        locked = self.parent.constant(self.lock.type, 1)

        res = self.lock.reference().atomic_cmpxchg(locked, unlocked,
                                                   ordering='release')

        with self.parent.ifelse( res != locked ) as ifelse:
            with ifelse.then():
                # This shall kill the program
                self.parent.unreachable()


class ContextCommon(CStruct):
    '''structure for thread-shared context information in parallel-ufunc.
    '''
    _fields_ = [
        # loop ufunc args
        ('args',        C.pointer(C.char_p)),
        ('dimensions',  C.pointer(C.intp)),
        ('steps',       C.pointer(C.intp)),
        ('data',        C.void_p),
        # specifics for work queues
        ('func',        C.void_p),
        ('num_thread',  C.int),
        ('workqueues',  C.pointer(WorkQueue.llvm_type())),
    ]

class Context(CStruct):
    '''structure for thread-specific context information in parallel-ufunc.
    '''
    _fields_ = [
        ('common',    C.pointer(ContextCommon.llvm_type())),
        ('id',        C.int),
        ('completed', C.intp),
    ]

class ParallelUFunc(CDefinition):
    '''the generic parallel vectorize mechanism

    Can be specialized to the maximum number of threads on the platform.


    Platform dependent threading function is implemented in

    def _dispatch_worker(self, worker, contexts, num_thread):
        ...

    which should be implemented in subclass or mixin.
    '''

    _argtys_ = [
        ('func',       C.void_p),
        ('worker',     C.void_p),
        ('args',       C.pointer(C.char_p)),
        ('dimensions', C.pointer(C.intp)),
        ('steps',      C.pointer(C.intp)),
        ('data',       C.void_p),
    ]

    @classmethod
    def specialize(cls, num_thread):
        '''specialize to the maximum # of thread
        '''
        cls._name_ = 'parallel_ufunc_%d' % num_thread
        cls.ThreadCount = num_thread

    def body(self, func, worker, args, dimensions, steps, data):
        # Setup variables
        ThreadCount = self.ThreadCount
        common = self.var(ContextCommon, name='common')
        workqueues = self.array(WorkQueue, ThreadCount, name='workqueues')
        contexts = self.array(Context, ThreadCount, name='contexts')

        num_thread = self.var(C.int, ThreadCount, name='num_thread')

        # Initialize ContextCommon
        common.args.assign(args)
        common.dimensions.assign(dimensions)
        common.steps.assign(steps)
        common.data.assign(data)
        common.func.assign(func)
        common.num_thread.assign(num_thread.cast(C.int))
        common.workqueues.assign(workqueues.reference())

        # Determine chunksize, initial count of work-items per thread.
        # If total_work >= num_thread, equally divide the works.
        # If total_work % num_thread != 0, the last thread does all remaining works.
        # If total_work < num_thread, each thread does one work,
        # and set num_thread to total_work
        N = dimensions[0]
        ChunkSize = self.var_copy(N / num_thread.cast(N.type))
        ChunkSize_NULL = self.constant_null(ChunkSize.type)
        with self.ifelse(ChunkSize == ChunkSize_NULL) as ifelse:
            with ifelse.then():
                ChunkSize.assign(self.constant(ChunkSize.type, 1))
                num_thread.assign(N.cast(num_thread.type))

        # Populate workqueue for all threads
        self._populate_workqueues(workqueues, N, ChunkSize, num_thread)

        # Populate contexts for all threads
        self._populate_context(contexts, common, num_thread)

        # Dispatch worker threads
        self._dispatch_worker(worker, contexts,  num_thread)

        ## DEBUG ONLY ##
        # Check for race condition
        if True:
            total_completed = self.var(C.intp, 0, name='total_completed')
            for t in range(ThreadCount):
                cur_ctxt = contexts[t].as_struct(Context)
                total_completed += cur_ctxt.completed
                # self.debug(cur_ctxt.id, 'completed', cur_ctxt.completed)

            with self.ifelse( total_completed == N ) as ifelse:
                with ifelse.then():
                    # self.debug("All is well!")
                    pass # keep quite if all is well
                with ifelse.otherwise():
                    self.debug("ERROR: race occurred! Trigger segfault")
                    self.unreachable()

        # Return
        self.ret()

    def _populate_workqueues(self, workqueues, N, ChunkSize, num_thread):
        '''loop over all threads and populate the workqueue for each of them.
        '''
        ONE = self.constant(num_thread.type, 1)
        with self.for_range(num_thread) as (loop, i):
            cur_wq = workqueues[i].as_struct(WorkQueue)
            cur_wq.next.assign(i.cast(ChunkSize.type) * ChunkSize)
            cur_wq.last.assign((i + ONE).cast(ChunkSize.type) * ChunkSize)
            cur_wq.lock.assign(self.constant(C.int, 0))
        # end loop
        last_wq = workqueues[num_thread - ONE].as_struct(WorkQueue)
        last_wq.last.assign(N)

    def _populate_context(self, contexts, common, num_thread):
        '''loop over all threads and populate contexts for each of them.
        '''
        ONE = self.constant(num_thread.type, 1)
        with self.for_range(num_thread) as (loop, i):
            cur_ctxt = contexts[i].as_struct(Context)
            cur_ctxt.common.assign(common.reference())
            cur_ctxt.id.assign(i)
            cur_ctxt.completed.assign(
                                    self.constant_null(cur_ctxt.completed.type))

class ParallelUFuncPosixMixin(object):
    '''ParallelUFunc mixin that implements _dispatch_worker to use pthread.
    '''
    def _dispatch_worker(self, worker, contexts, num_thread):
        api = PThreadAPI(self)
        NULL = self.constant_null(C.void_p)

        threads = self.array(api.pthread_t, num_thread, name='threads')

        # self.debug("launch threads")
        # TODO error handling

        ONE = self.constant(num_thread.type, 1)
        with self.for_range(num_thread) as (loop, i):
            api.pthread_create(threads[i].reference(), NULL, worker,
                               contexts[i].reference().cast(C.void_p))

        with self.for_range(num_thread) as (loop, i):
            api.pthread_join(threads[i], NULL)

class UFuncCore(CDefinition):
    '''core work of a ufunc worker thread

    Subclass to implement UFuncCore._do_work

    Generates the workqueue handling and work stealing and invoke
    the work function for each work item.
    '''
    _name_ = 'ufunc_worker'
    _argtys_ = [
        ('context', C.pointer(Context.llvm_type())),
        ]

    def body(self, context):
        context = context.as_struct(Context)
        common = context.common.as_struct(ContextCommon)
        tid = context.id

        # self.debug("start thread", tid, "/", common.num_thread)
        workqueue = common.workqueues[tid].as_struct(WorkQueue)

        self._do_workqueue(common, workqueue, tid, context.completed)
        self._do_work_stealing(common, tid, context.completed) # optional

        self.ret()

    def _do_workqueue(self, common, workqueue, tid, completed):
        '''process local workqueue.
        '''
        ZERO = self.constant_null(C.int)

        with self.forever() as loop:
            workqueue.Lock()
            # Critical section
            item = self.var_copy(workqueue.next, name='item')
            workqueue.next += self.constant(item.type, 1)
            last = self.var_copy(workqueue.last, name='last')
            # Release
            workqueue.Unlock()

            with self.ifelse( item >= last ) as ifelse:
                with ifelse.then():
                    loop.break_loop()

            self._do_work(common, item, tid)
            completed += self.constant(completed.type, 1)

    def _do_work_stealing(self, common, tid, completed):
        '''steal work from other workqueues.
        '''
        # self.debug("start work stealing", tid)
        steal_continue = self.var(C.int, 1)
        STEAL_STOP = self.constant_null(steal_continue.type)

        # Loop until all workqueues are done.
        with self.loop() as loop:
            with loop.condition() as setcond:
                setcond( steal_continue != STEAL_STOP )

            with loop.body():
                steal_continue.assign(STEAL_STOP)
                self._do_work_stealing_innerloop(common, steal_continue, tid,
                                                 completed)

    def _do_work_stealing_innerloop(self, common, steal_continue, tid,
                                    completed):
        '''loop over all other threads and try to steal work.
        '''
        with self.for_range(common.num_thread) as (loop, i):
            with self.ifelse( i != tid ) as ifelse:
                with ifelse.then():
                    otherqueue = common.workqueues[i].as_struct(WorkQueue)
                    self._do_work_stealing_check(common, otherqueue,
                                                 steal_continue, tid,
                                                 completed)

    def _do_work_stealing_check(self, common, otherqueue, steal_continue, tid,
                                completed):
        '''check the workqueue for any remaining work and steal it.
        '''
        otherqueue.Lock()
        # Acquired
        ONE = self.constant(otherqueue.last.type, 1)
        STEAL_CONTINUE = self.constant(steal_continue.type, 1)
        with self.ifelse(otherqueue.next < otherqueue.last) as ifelse:
            with ifelse.then():
                otherqueue.last -= ONE
                item = self.var_copy(otherqueue.last)

                otherqueue.Unlock()
                # Released

                self._do_work(common, item, tid)
                completed += self.constant(completed.type, 1)

                # Mark incomplete thread
                steal_continue.assign(STEAL_CONTINUE)

            with ifelse.otherwise():
                otherqueue.Unlock()
                # Released

    def _do_work(self, common, item, tid):
        '''prepare to call the actual work function

        Implementation depends on number and type of arguments.
        '''
        raise NotImplementedError

class SpecializedParallelUFunc(CDefinition):
    '''a generic ufunc that wraps ParallelUFunc, UFuncCore and the workload
    '''
    _argtys_ = [
        ('args',       C.pointer(C.char_p)),
        ('dimensions', C.pointer(C.intp)),
        ('steps',      C.pointer(C.intp)),
        ('data',       C.void_p),
    ]

    def body(self, args, dimensions, steps, data,):
        pufunc = self.depends(self.PUFuncDef)
        core = self.depends(self.CoreDef)
        func = self.depends(self.FuncDef)
        to_void_p = lambda x: x.cast(C.void_p)
        pufunc(to_void_p(func), to_void_p(core), args, dimensions, steps, data)
        self.ret()

    @classmethod
    def specialize(cls, pufunc_def, core_def, func_def):
        '''specialize to a combination of ParallelUFunc, UFuncCore and workload
        '''
        cls._name_ = 'specialized_%s_%s_%s'% (pufunc_def, core_def, func_def)
        cls.PUFuncDef = pufunc_def
        cls.CoreDef = core_def
        cls.FuncDef = func_def

class PThreadAPI(CExternal):
    '''external declaration of pthread API
    '''
    pthread_t = C.void_p

    pthread_create = Type.function(C.int,
                                   [C.pointer(pthread_t),  # thread_t
                                    C.void_p,              # thread attr
                                    C.void_p,              # function
                                    C.void_p])             # arg

    pthread_join = Type.function(C.int, [C.void_p, C.void_p])


