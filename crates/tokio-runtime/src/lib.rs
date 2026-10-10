//! Tokio callback driver used only for cross-language runtime experiments.

use std::error::Error;
use std::fmt::{self, Display, Formatter};
use std::sync::Arc;

use rivetoo_graph::{
    CompiledExecutionGraph, ConditionRequest, ConditionResolution, ExecutionEvent,
    ExecutionSession, ExecutionUpdate, NodeId, SessionError,
};
use tokio::runtime::Builder;
use tokio::task::JoinSet;

/// Host callbacks required by the experimental Tokio driver.
#[allow(clippy::missing_errors_doc)]
pub trait CallbackHost: Send + Sync + 'static {
    type Error: Error + Send + Sync + 'static;

    fn initially_skipped(&self) -> Result<Vec<NodeId>, Self::Error>;
    fn resolve_conditions(
        &self,
        requests: &[ConditionRequest],
    ) -> Result<Vec<ConditionResolution>, Self::Error>;
    fn apply_events(&self, events: &[ExecutionEvent]) -> Result<(), Self::Error>;
    fn run_action(&self, node: NodeId) -> Result<(), Self::Error>;
    fn stop_requested(&self) -> Result<bool, Self::Error>;
    fn request_stop(&self) -> Result<(), Self::Error>;
    fn require_running(&self) -> Result<(), Self::Error>;
}

/// Failure produced by the experimental Tokio callback driver.
#[derive(Debug)]
pub enum DriverError<E> {
    InvalidConcurrency,
    Runtime(std::io::Error),
    Session(SessionError),
    Host(E),
    Join(tokio::task::JoinError),
    Stalled,
}

impl<E: Display> Display for DriverError<E> {
    fn fmt(&self, formatter: &mut Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidConcurrency => write!(formatter, "graph concurrency must be >= 1"),
            Self::Runtime(error) => write!(formatter, "failed to create Tokio runtime: {error}"),
            Self::Session(error) => Display::fmt(error, formatter),
            Self::Host(error) => Display::fmt(error, formatter),
            Self::Join(error) => write!(formatter, "Tokio action task failed: {error}"),
            Self::Stalled => write!(formatter, "running Tokio graph session has no action"),
        }
    }
}

impl<E: Error + 'static> Error for DriverError<E> {}

/// Runs the callback experiment with a bounded number of blocking actions.
///
/// # Errors
/// Returns a driver error when runtime creation, a host callback, session
/// transition, or spawned action fails.
pub fn run<H>(
    graph: Arc<CompiledExecutionGraph>,
    host: Arc<H>,
    concurrency: usize,
) -> Result<(), DriverError<H::Error>>
where
    H: CallbackHost,
{
    if concurrency == 0 {
        return Err(DriverError::InvalidConcurrency);
    }
    let runtime = Builder::new_multi_thread()
        .worker_threads(concurrency)
        .max_blocking_threads(concurrency)
        .build()
        .map_err(DriverError::Runtime)?;
    runtime.block_on(run_async(graph, host, concurrency))
}

async fn run_async<H>(
    graph: Arc<CompiledExecutionGraph>,
    host: Arc<H>,
    concurrency: usize,
) -> Result<(), DriverError<H::Error>>
where
    H: CallbackHost,
{
    let initially_skipped = host.initially_skipped().map_err(DriverError::Host)?;
    let mut session =
        ExecutionSession::new(graph, initially_skipped).map_err(DriverError::Session)?;
    let mut tasks = JoinSet::new();
    let mut batch = session.start(concurrency).map_err(DriverError::Session)?;
    let mut failure = None;

    loop {
        let mut actions = Vec::new();
        loop {
            host.apply_events(batch.events())
                .map_err(DriverError::Host)?;
            actions.extend_from_slice(batch.actions());
            if batch.conditions().is_empty() {
                break;
            }
            let resolutions = host
                .resolve_conditions(batch.conditions())
                .map_err(DriverError::Host)?;
            batch = session
                .advance(
                    ExecutionUpdate::new(
                        [],
                        resolutions,
                        host.initially_skipped().map_err(DriverError::Host)?,
                    ),
                    concurrency - actions.len() - tasks.len(),
                )
                .map_err(DriverError::Session)?;
        }

        if failure.is_none() && !host.stop_requested().map_err(DriverError::Host)? {
            for node in actions {
                let action_host = Arc::clone(&host);
                tasks.spawn_blocking(move || (node, action_host.run_action(node)));
            }
        }

        if batch.status() != rivetoo_graph::SessionStatus::Running && tasks.is_empty() {
            break;
        }
        if tasks.is_empty() {
            host.require_running().map_err(DriverError::Host)?;
            return Err(DriverError::Stalled);
        }

        let Some(joined) = tasks.join_next().await else {
            return Err(DriverError::Stalled);
        };
        let (node, result) = joined.map_err(DriverError::Join)?;
        let mut completed = Vec::new();
        match result {
            Ok(()) if failure.is_none() => completed.push(node),
            Ok(()) => {}
            Err(error) => {
                if failure.is_none() {
                    host.request_stop().map_err(DriverError::Host)?;
                    failure = Some(error);
                }
            }
        }
        if tasks.is_empty()
            && let Some(error) = failure.take()
        {
            return Err(DriverError::Host(error));
        }
        if failure.is_some() {
            continue;
        }

        batch = session
            .advance(
                ExecutionUpdate::new(
                    completed.into_iter().map(|node| (node, Ok(()))),
                    [],
                    host.initially_skipped().map_err(DriverError::Host)?,
                ),
                concurrency - tasks.len(),
            )
            .map_err(DriverError::Session)?;
    }
    host.require_running().map_err(DriverError::Host)
}
