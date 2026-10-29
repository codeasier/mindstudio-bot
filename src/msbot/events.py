from __future__ import annotations


class EventError(ValueError):
    pass


def label_names(value) -> set[str]:
    if not isinstance(value, list):
        raise EventError("Expected a label list")
    names = set()
    for item in value:
        if isinstance(item, dict):
            item = item.get("name", item.get("title"))
        if not isinstance(item, str) or not item:
            raise EventError("Label identity is unavailable")
        names.add(item)
    return names


def classify(payload: dict, event_header: str, repo: dict) -> tuple[str | None, str | None]:
    project = payload.get("project")
    if not isinstance(project, dict):
        raise EventError("Missing project identity")
    if project.get("path_with_namespace") != repo["name"] or str(project.get("id")) != repo["project_id"]:
        raise EventError("Webhook project does not match its repository binding")
    if event_header != "Issue Hook" or payload.get("object_kind") != "issue":
        return None, "not_issue_hook"
    attributes = payload.get("object_attributes")
    if not isinstance(attributes, dict):
        raise EventError("Missing Issue attributes")
    number = attributes.get("iid")
    if type(number) not in {str, int} or not str(number).isalnum():
        raise EventError("Invalid Issue number")
    number = str(number)
    if attributes.get("action") != "update":
        return number, "not_issue_update"
    changes = payload.get("changes")
    if not isinstance(changes, dict) or "labels" not in changes:
        return number, "no_explicit_label_change"
    delta = changes["labels"]
    if not isinstance(delta, dict) or "previous" not in delta or "current" not in delta:
        return number, "unrecognized_label_change"
    try:
        before = label_names(delta["previous"])
        after = label_names(delta["current"])
    except EventError:
        return number, "unrecognized_label_change"
    if "resolved" not in after - before:
        return number, "resolved_not_added"
    return number, None
