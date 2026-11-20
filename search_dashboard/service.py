"""Presentation helpers for server-owned state; no routing or lifecycle mutations."""
MODE_LABELS = {"CATCHING_UP": "Searching recordings", "LIVE_TRACKING": "Live tracking",
               "WAITING": "Waiting for sightings", "PAST_SUMMARY": "Search summary"}
STOP_LABELS = {"ALL_ENDED": "No further updates", "MAX_GAP": "Sighting gap limit reached",
               "BUDGET_SPENT": "Processing limit reached", "OPERATOR_CLOSED": "Closed by operator"}

def status_label(case):
    if case.lifecycle == "FAILED":
        return "Search failed"
    if case.lifecycle == "PREPARING":
        return "Preparing search"
    if case.lifecycle == "CLOSED":
        return "Search ended" if case.done else "Closing search"
    return MODE_LABELS.get(case.mode, case.mode)

def can_review(case):
    # Post-completion review support is not specified: default to read-only.
    return case.lifecycle == "SEARCHING" and not case.done and case.source != "legacy"

def route(case):
    return case.route


def updated_snapshot(previous, incoming):
    """Ignore stale/out-of-order views; route-only revisions also trigger a refresh."""
    if previous is None or previous.id != incoming.id:
        return incoming
    if incoming.revision < previous.revision or incoming.route_revision < previous.route_revision:
        return previous
    if (incoming.revision, incoming.route_revision) == (previous.revision, previous.route_revision):
        return previous
    return incoming
