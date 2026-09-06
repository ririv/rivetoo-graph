use std::collections::VecDeque;
use std::panic::{AssertUnwindSafe, catch_unwind};
use std::sync::{Arc, Condvar, Mutex, mpsc};
use std::thread::{self, JoinHandle};

use crate::{NativeActionContext, NativeActionError, NativeActionHandle};

struct NativeActionJob {
    node: usize,
    action: NativeActionHandle,
    context: NativeActionContext,
}

/// One native action completion retained until its graph session consumes it.
#[derive(Debug)]
pub struct NativeActionCompletion {
    node: usize,
    result: Result<(), NativeActionError>,
}

impl NativeActionCompletion {
    #[must_use]
    pub const fn node(&self) -> usize {
        self.node
    }

    /// Returns the concrete action result.
    ///
    /// # Errors
    /// Returns the error produced by the native action.
    pub fn into_result(self) -> Result<(), NativeActionError> {
        self.result
    }
}

#[derive(Default)]
struct CompletionQueue {
    values: Mutex<VecDeque<NativeActionCompletion>>,
    ready: Condvar,
}

struct ExecutorInner {
    jobs: mpsc::Sender<Option<NativeActionJob>>,
    completions: Arc<CompletionQueue>,
    workers: Vec<JoinHandle<()>>,
}

impl Drop for ExecutorInner {
    fn drop(&mut self) {
        for _ in &self.workers {
            let _ = self.jobs.send(None);
        }
        for worker in self.workers.drain(..) {
            let _ = worker.join();
        }
    }
}

/// A bounded Rust worker set used only for Rust-owned actions.
#[derive(Clone)]
pub struct NativeActionExecutor {
    inner: Arc<ExecutorInner>,
}

impl NativeActionExecutor {
    /// Creates a native worker set with an explicit positive capacity.
    ///
    /// # Errors
    /// Returns an error when capacity is zero or an operating-system thread
    /// cannot be created.
    pub fn new(workers: usize) -> Result<Self, NativeActionError> {
        if workers == 0 {
            return Err(NativeActionError::new(
                "native action workers must be greater than zero",
            ));
        }
        let (jobs, receiver) = mpsc::channel::<Option<NativeActionJob>>();
        let receiver = Arc::new(Mutex::new(receiver));
        let completions = Arc::new(CompletionQueue::default());
        let mut worker_handles = Vec::with_capacity(workers);
        for index in 0..workers {
            let receiver = Arc::clone(&receiver);
            let completions = Arc::clone(&completions);
            let worker = thread::Builder::new()
                .name(format!("NativeGraphWorker-{index}"))
                .spawn(move || worker_loop(&receiver, &completions))
                .map_err(|error| {
                    NativeActionError::new(format!("failed to create native graph worker: {error}"))
                })?;
            worker_handles.push(worker);
        }
        Ok(Self {
            inner: Arc::new(ExecutorInner {
                jobs,
                completions,
                workers: worker_handles,
            }),
        })
    }

    /// Submits one action to the native worker set.
    ///
    /// # Errors
    /// Returns an error when the executor has already stopped accepting work.
    pub fn submit(
        &self,
        node: usize,
        action: NativeActionHandle,
        context: NativeActionContext,
    ) -> Result<(), NativeActionError> {
        self.inner
            .jobs
            .send(Some(NativeActionJob {
                node,
                action,
                context,
            }))
            .map_err(|_| NativeActionError::new("native action executor is closed"))
    }

    /// Blocks until at least one completion can be drained.
    pub fn wait_for_completion(&self) {
        let mut values = self
            .inner
            .completions
            .values
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        while values.is_empty() {
            values = self
                .inner
                .completions
                .ready
                .wait(values)
                .unwrap_or_else(std::sync::PoisonError::into_inner);
        }
    }

    #[must_use]
    pub fn drain_completions(&self) -> Vec<NativeActionCompletion> {
        self.inner
            .completions
            .values
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .drain(..)
            .collect()
    }
}

fn worker_loop(
    jobs: &Mutex<mpsc::Receiver<Option<NativeActionJob>>>,
    completions: &CompletionQueue,
) {
    loop {
        let job = jobs
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .recv();
        let Ok(Some(job)) = job else {
            return;
        };
        let result = catch_unwind(AssertUnwindSafe(|| job.action.execute(&job.context)))
            .unwrap_or_else(|_| Err(NativeActionError::new("native action panicked")));
        completions
            .values
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .push_back(NativeActionCompletion {
                node: job.node,
                result,
            });
        completions.ready.notify_one();
    }
}
