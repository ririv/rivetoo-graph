use std::collections::{HashMap, HashSet};
use std::error::Error;
use std::fmt::{self, Display, Formatter};

/// Stable component identity during one capability resolution.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub struct ComponentId(usize);

impl ComponentId {
    #[must_use]
    pub const fn new(index: usize) -> Self {
        Self(index)
    }

    #[must_use]
    pub const fn index(self) -> usize {
        self.0
    }
}

/// Stable capability identity during one capability resolution.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub struct CapabilityId(usize);

impl CapabilityId {
    #[must_use]
    pub const fn new(index: usize) -> Self {
        Self(index)
    }

    #[must_use]
    pub const fn index(self) -> usize {
        self.0
    }
}

/// Stable requirement identity used by an explicit provider selection.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub struct RequirementId(usize);

impl RequirementId {
    #[must_use]
    pub const fn new(index: usize) -> Self {
        Self(index)
    }

    #[must_use]
    pub const fn index(self) -> usize {
        self.0
    }
}

/// A requirement either accepts one deterministic provider or names it exactly.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum CapabilityRequirement {
    Any {
        consumer: ComponentId,
        capability: CapabilityId,
    },
    Exact {
        consumer: ComponentId,
        capability: CapabilityId,
        provider: ComponentId,
    },
}

impl CapabilityRequirement {
    #[must_use]
    pub const fn consumer(self) -> ComponentId {
        match self {
            Self::Any { consumer, .. } | Self::Exact { consumer, .. } => consumer,
        }
    }

    #[must_use]
    pub const fn capability(self) -> CapabilityId {
        match self {
            Self::Any { capability, .. } | Self::Exact { capability, .. } => capability,
        }
    }
}

/// An explicit provider choice for one any-provider requirement.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct ProviderSelection {
    requirement: RequirementId,
    provider: ComponentId,
}

impl ProviderSelection {
    #[must_use]
    pub const fn new(requirement: RequirementId, provider: ComponentId) -> Self {
        Self {
            requirement,
            provider,
        }
    }
}

/// One fully resolved capability connection.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct ResolvedCapabilityBinding {
    pub provider: ComponentId,
    pub consumer: ComponentId,
    pub capability: CapabilityId,
}

/// A deterministic capability resolution failure.
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum CapabilityResolutionError {
    UnknownConsumer(ComponentId),
    UnknownProvider(ComponentId),
    DuplicateSelection(RequirementId),
    UnknownSelectionRequirement(RequirementId),
    SelectionForExactRequirement(RequirementId),
    MixedRequirementKinds {
        consumer: ComponentId,
        capability: CapabilityId,
    },
    DuplicateAnyRequirement {
        consumer: ComponentId,
        capability: CapabilityId,
    },
    DuplicateExactRequirement {
        consumer: ComponentId,
        capability: CapabilityId,
        provider: ComponentId,
    },
    NoProvider {
        consumer: ComponentId,
        capability: CapabilityId,
    },
    AmbiguousProvider {
        consumer: ComponentId,
        capability: CapabilityId,
        candidates: Vec<ComponentId>,
    },
    ProviderDoesNotOffer {
        provider: ComponentId,
        capability: CapabilityId,
    },
    UnusedSelection(RequirementId),
}

impl Display for CapabilityResolutionError {
    fn fmt(&self, formatter: &mut Formatter<'_>) -> fmt::Result {
        match self {
            Self::UnknownConsumer(component) => write!(
                formatter,
                "capability requirement has unknown consumer {}",
                component.index()
            ),
            Self::UnknownProvider(component) => write!(
                formatter,
                "capability requirement has unknown provider {}",
                component.index()
            ),
            Self::DuplicateSelection(requirement) => write!(
                formatter,
                "capability requirement {} has multiple provider selections",
                requirement.index()
            ),
            Self::UnknownSelectionRequirement(requirement) => write!(
                formatter,
                "provider selection references unknown requirement {}",
                requirement.index()
            ),
            Self::SelectionForExactRequirement(requirement) => write!(
                formatter,
                "provider selection references exact requirement {}",
                requirement.index()
            ),
            Self::MixedRequirementKinds {
                consumer,
                capability,
            } => write!(
                formatter,
                "capability input mixes any and exact requirements: consumer {} capability {}",
                consumer.index(),
                capability.index()
            ),
            Self::DuplicateAnyRequirement {
                consumer,
                capability,
            } => write!(
                formatter,
                "capability requirement is declared more than once: consumer {} capability {}",
                consumer.index(),
                capability.index()
            ),
            Self::DuplicateExactRequirement {
                consumer,
                capability,
                provider,
            } => write!(
                formatter,
                "exact capability requirement is declared more than once: consumer {} capability {} provider {}",
                consumer.index(),
                capability.index(),
                provider.index()
            ),
            Self::NoProvider {
                consumer,
                capability,
            } => write!(
                formatter,
                "capability has no provider: consumer {} capability {}",
                consumer.index(),
                capability.index()
            ),
            Self::AmbiguousProvider {
                consumer,
                capability,
                candidates,
            } => write!(
                formatter,
                "capability provider is ambiguous: consumer {} capability {} candidates {:?}",
                consumer.index(),
                capability.index(),
                candidates
                    .iter()
                    .map(|candidate| candidate.index())
                    .collect::<Vec<_>>()
            ),
            Self::ProviderDoesNotOffer {
                provider,
                capability,
            } => write!(
                formatter,
                "selected component {} does not provide capability {}",
                provider.index(),
                capability.index()
            ),
            Self::UnusedSelection(requirement) => write!(
                formatter,
                "provider selection has no matching any-provider requirement: {}",
                requirement.index()
            ),
        }
    }
}

impl Error for CapabilityResolutionError {}

/// Resolves every requirement in declaration order.
///
/// # Errors
///
/// Returns [`CapabilityResolutionError`] when declarations are ambiguous,
/// duplicated, inconsistent, or reference unknown components.
pub fn resolve_capabilities(
    component_outputs: &[Vec<CapabilityId>],
    requirements: &[CapabilityRequirement],
    selections: &[ProviderSelection],
) -> Result<Vec<ResolvedCapabilityBinding>, CapabilityResolutionError> {
    let providers = index_providers(component_outputs);
    let selection_by_requirement = index_selections(requirements, selections)?;
    validate_requirement_groups(component_outputs.len(), requirements)?;

    let mut used_selections = HashSet::new();
    let mut bindings = Vec::with_capacity(requirements.len());
    for (index, requirement) in requirements.iter().copied().enumerate() {
        let requirement_id = RequirementId::new(index);
        let consumer = requirement.consumer();
        let capability = requirement.capability();
        let provider = match requirement {
            CapabilityRequirement::Exact { provider, .. } => provider,
            CapabilityRequirement::Any { .. } => {
                let candidates = providers.get(&capability).map_or(&[][..], Vec::as_slice);
                if let Some(provider) = selection_by_requirement.get(&requirement_id) {
                    used_selections.insert(requirement_id);
                    if !candidates.contains(provider) {
                        return Err(CapabilityResolutionError::ProviderDoesNotOffer {
                            provider: *provider,
                            capability,
                        });
                    }
                    *provider
                } else {
                    match candidates {
                        [] => {
                            return Err(CapabilityResolutionError::NoProvider {
                                consumer,
                                capability,
                            });
                        }
                        [provider] => *provider,
                        _ => {
                            return Err(CapabilityResolutionError::AmbiguousProvider {
                                consumer,
                                capability,
                                candidates: candidates.to_vec(),
                            });
                        }
                    }
                }
            }
        };
        require_component(component_outputs.len(), provider, false)?;
        if !component_outputs[provider.index()].contains(&capability) {
            return Err(CapabilityResolutionError::ProviderDoesNotOffer {
                provider,
                capability,
            });
        }
        bindings.push(ResolvedCapabilityBinding {
            provider,
            consumer,
            capability,
        });
    }

    for requirement in selection_by_requirement.keys() {
        if !used_selections.contains(requirement) {
            return Err(CapabilityResolutionError::UnusedSelection(*requirement));
        }
    }
    Ok(bindings)
}

fn index_providers(
    component_outputs: &[Vec<CapabilityId>],
) -> HashMap<CapabilityId, Vec<ComponentId>> {
    let mut providers: HashMap<CapabilityId, Vec<ComponentId>> = HashMap::new();
    for (index, outputs) in component_outputs.iter().enumerate() {
        let component = ComponentId::new(index);
        for capability in outputs {
            let candidates = providers.entry(*capability).or_default();
            if !candidates.contains(&component) {
                candidates.push(component);
            }
        }
    }
    providers
}

fn index_selections(
    requirements: &[CapabilityRequirement],
    selections: &[ProviderSelection],
) -> Result<HashMap<RequirementId, ComponentId>, CapabilityResolutionError> {
    let mut indexed = HashMap::new();
    for selection in selections {
        let Some(requirement) = requirements.get(selection.requirement.index()) else {
            return Err(CapabilityResolutionError::UnknownSelectionRequirement(
                selection.requirement,
            ));
        };
        if matches!(requirement, CapabilityRequirement::Exact { .. }) {
            return Err(CapabilityResolutionError::SelectionForExactRequirement(
                selection.requirement,
            ));
        }
        if indexed
            .insert(selection.requirement, selection.provider)
            .is_some()
        {
            return Err(CapabilityResolutionError::DuplicateSelection(
                selection.requirement,
            ));
        }
    }
    Ok(indexed)
}

fn validate_requirement_groups(
    component_count: usize,
    requirements: &[CapabilityRequirement],
) -> Result<(), CapabilityResolutionError> {
    let mut kinds = HashMap::new();
    let mut any = HashSet::new();
    let mut exact = HashSet::new();
    for requirement in requirements {
        let consumer = requirement.consumer();
        let capability = requirement.capability();
        require_component(component_count, consumer, true)?;
        let is_any = matches!(requirement, CapabilityRequirement::Any { .. });
        if let Some(previous) = kinds.get(&(consumer, capability)) {
            if *previous != is_any {
                return Err(CapabilityResolutionError::MixedRequirementKinds {
                    consumer,
                    capability,
                });
            }
        } else {
            kinds.insert((consumer, capability), is_any);
        }
        match requirement {
            CapabilityRequirement::Any { .. } => {
                if !any.insert((consumer, capability)) {
                    return Err(CapabilityResolutionError::DuplicateAnyRequirement {
                        consumer,
                        capability,
                    });
                }
            }
            CapabilityRequirement::Exact { provider, .. } => {
                require_component(component_count, *provider, false)?;
                if !exact.insert((consumer, capability, *provider)) {
                    return Err(CapabilityResolutionError::DuplicateExactRequirement {
                        consumer,
                        capability,
                        provider: *provider,
                    });
                }
            }
        }
    }
    Ok(())
}

fn require_component(
    component_count: usize,
    component: ComponentId,
    consumer: bool,
) -> Result<(), CapabilityResolutionError> {
    if component.index() < component_count {
        return Ok(());
    }
    if consumer {
        Err(CapabilityResolutionError::UnknownConsumer(component))
    } else {
        Err(CapabilityResolutionError::UnknownProvider(component))
    }
}

#[cfg(test)]
mod tests {
    use super::{
        CapabilityId, CapabilityRequirement, CapabilityResolutionError, ComponentId,
        ProviderSelection, RequirementId, ResolvedCapabilityBinding, resolve_capabilities,
    };

    #[test]
    fn resolves_selected_any_provider_and_exact_provider() {
        let capability = CapabilityId::new(0);
        let bindings = resolve_capabilities(
            &[vec![capability], vec![capability], vec![], vec![]],
            &[
                CapabilityRequirement::Any {
                    consumer: ComponentId::new(2),
                    capability,
                },
                CapabilityRequirement::Exact {
                    consumer: ComponentId::new(3),
                    capability,
                    provider: ComponentId::new(0),
                },
            ],
            &[ProviderSelection::new(
                RequirementId::new(0),
                ComponentId::new(1),
            )],
        )
        .unwrap();

        assert_eq!(
            bindings,
            [
                ResolvedCapabilityBinding {
                    provider: ComponentId::new(1),
                    consumer: ComponentId::new(2),
                    capability,
                },
                ResolvedCapabilityBinding {
                    provider: ComponentId::new(0),
                    consumer: ComponentId::new(3),
                    capability,
                },
            ]
        );
    }

    #[test]
    fn rejects_an_ambiguous_any_provider_requirement() {
        let capability = CapabilityId::new(0);
        let result = resolve_capabilities(
            &[vec![capability], vec![capability], vec![]],
            &[CapabilityRequirement::Any {
                consumer: ComponentId::new(2),
                capability,
            }],
            &[],
        );

        assert_eq!(
            result,
            Err(CapabilityResolutionError::AmbiguousProvider {
                consumer: ComponentId::new(2),
                capability,
                candidates: vec![ComponentId::new(0), ComponentId::new(1)],
            })
        );
    }

    #[test]
    fn rejects_multiple_selections_for_one_requirement() {
        let capability = CapabilityId::new(0);
        let result = resolve_capabilities(
            &[vec![capability], vec![capability], vec![]],
            &[CapabilityRequirement::Any {
                consumer: ComponentId::new(2),
                capability,
            }],
            &[
                ProviderSelection::new(RequirementId::new(0), ComponentId::new(0)),
                ProviderSelection::new(RequirementId::new(0), ComponentId::new(1)),
            ],
        );

        assert_eq!(
            result,
            Err(CapabilityResolutionError::DuplicateSelection(
                RequirementId::new(0)
            ))
        );
    }
}
