"""Public domain errors, independent of chat and HTTP transports."""


class RuleError(Exception):
    """A user action does not satisfy a booking rule."""


class AccessDenied(RuleError):
    """The visitor or actor lacks the needed rights."""


class SlotUnavailable(RuleError):
    """The chosen slot is no longer available."""


class StateConflict(RuleError):
    """The caller's revision is stale; reload before attempting another change."""


class ActiveDraftExists(StateConflict, SlotUnavailable):
    """A second hold cannot be created until the existing draft is reset."""


class StorageUnavailable(Exception):
    """The database is temporarily unavailable; safe to show to the user."""
