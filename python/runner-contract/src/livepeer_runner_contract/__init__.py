"""Runner contract v1, the live runner standard: the gateway library and the ``runner-contract`` builder tool.

The library reads what runners publish and what the registry says, and nothing else. It makes no
network calls and holds no policy: approvals, routes and retail rules stay in each gateway.
README.md documents the public API.
"""

from __future__ import annotations

from .envelope import (
    Admission,
    AttributeValue,
    CapacityGroup,
    Channels,
    Endpoint,
    Envelope,
    Ineligible,
    IneligibleReason,
    Model,
    Runtime,
    canonical_json,
    deployment_revision,
    envelope_problems,
    parse_envelope,
)
from .ignored import forget_ignored, log_ignored
from .registry import (
    Attempts,
    Entry,
    EntryKind,
    ErrorKind,
    ErrorMapping,
    FinalCondition,
    Framing,
    Location,
    Operation,
    Registry,
    RegistryError,
    Retry,
    SuccessRule,
    UnknownEndpoint,
    UnknownProfile,
)
from .requests import RequestCheck, validate_request
from .results import Classification, Result, ResultReader, classify, framing_of, read_result
from .select import (
    ApprovalKey,
    Approvals,
    Discovery,
    IgnoredRunner,
    Price,
    Runner,
    RunnerKey,
    Selection,
    parse_discovery,
    select,
)
from .standard import MAX_METADATA_BYTES, REPOSITORY, RUNNER_CONTRACT, Capability, Mode, capabilities

__version__ = "0.1.0"

__all__ = [
    "MAX_METADATA_BYTES",
    "REPOSITORY",
    "RUNNER_CONTRACT",
    "Admission",
    "ApprovalKey",
    "Approvals",
    "Attempts",
    "AttributeValue",
    "Capability",
    "CapacityGroup",
    "Channels",
    "Classification",
    "Discovery",
    "Endpoint",
    "Entry",
    "EntryKind",
    "Envelope",
    "ErrorKind",
    "ErrorMapping",
    "FinalCondition",
    "Framing",
    "IgnoredRunner",
    "Ineligible",
    "IneligibleReason",
    "Location",
    "Mode",
    "Model",
    "Operation",
    "Price",
    "Registry",
    "RegistryError",
    "RequestCheck",
    "Result",
    "ResultReader",
    "Retry",
    "Runner",
    "RunnerKey",
    "Runtime",
    "Selection",
    "SuccessRule",
    "UnknownEndpoint",
    "UnknownProfile",
    "__version__",
    "canonical_json",
    "capabilities",
    "classify",
    "deployment_revision",
    "envelope_problems",
    "forget_ignored",
    "framing_of",
    "log_ignored",
    "parse_discovery",
    "parse_envelope",
    "read_result",
    "select",
    "validate_request",
]
