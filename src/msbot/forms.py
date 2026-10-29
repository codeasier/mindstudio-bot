from __future__ import annotations

import re
from dataclasses import dataclass

from .config import ConfigurationError, load_yaml, text


def clean(value: str) -> str:
    return re.sub(r"<!--.*?-->", "", value, flags=re.S).strip()


def meaningful(value: str) -> bool:
    value = re.sub(r"(?m)^\s{0,3}#{1,6}\s+.*$", "", clean(value))
    value = re.sub(r"(?m)^\s{0,3}(?:`{3,}|~{3,}).*$", "", value)
    return value.strip("_ *-\n\t").casefold() not in {
        "",
        "no response",
        "none provided",
        "未填写",
        "待填写",
    }


@dataclass(frozen=True)
class FormField:
    id: str
    label: str
    kind: str
    required: bool
    options: tuple[str, ...]
    multiple: bool
    description: str


@dataclass(frozen=True)
class IssueForm:
    fields: tuple[FormField, ...]

    @classmethod
    def parse(cls, source: str) -> IssueForm:
        root = load_yaml(source)
        body = root.get("body")
        if not isinstance(body, list) or not body or len(body) > 100:
            raise ConfigurationError("Template body must contain 1–100 entries")
        fields = []
        ids: set[str] = set()
        labels: set[str] = set()
        for entry in body:
            if not isinstance(entry, dict):
                raise ConfigurationError("Invalid template entry")
            kind = entry.get("type")
            if kind == "markdown":
                continue
            if kind not in {"input", "textarea", "dropdown", "checkboxes"}:
                raise ConfigurationError("Unsupported Issue Forms field type")
            identity = text(entry.get("id"), "field id")
            attributes = entry.get("attributes")
            if not isinstance(attributes, dict):
                raise ConfigurationError("Missing field attributes")
            label = text(attributes.get("label"), "field label")
            if "\n" in label or identity in ids or label in labels:
                raise ConfigurationError("Field IDs and labels must be unique, single-line values")
            validations = entry.get("validations", {})
            if not isinstance(validations, dict) or set(validations) - {"required"}:
                raise ConfigurationError("Unsupported field validations")
            required = validations.get("required", False)
            multiple = attributes.get("multiple", False)
            if type(required) is not bool or type(multiple) is not bool:
                raise ConfigurationError("required and multiple must be booleans")
            options = []
            if kind in {"dropdown", "checkboxes"}:
                choices = attributes.get("options")
                if not isinstance(choices, list) or not choices:
                    raise ConfigurationError("Choice fields require options")
                for choice in choices:
                    if kind == "checkboxes":
                        if not isinstance(choice, dict) or set(choice) - {"label", "required"}:
                            raise ConfigurationError("Invalid checkbox option")
                        if choice.get("required"):
                            raise ConfigurationError("Per-option required checkboxes are not supported")
                        choice = choice.get("label")
                    options.append(text(choice, "option"))
                if len(set(options)) != len(options):
                    raise ConfigurationError("Choice values must be unique")
            description = attributes.get("description", "")
            if not isinstance(description, str):
                raise ConfigurationError("Field description must be text")
            fields.append(FormField(identity, label, kind, required, tuple(options), multiple, description))
            ids.add(identity)
            labels.add(label)
        if not fields or not any(f.required for f in fields):
            raise ConfigurationError("Audit template must declare required fields")
        return cls(tuple(fields))

    def validate(self, comment: str) -> list[str]:
        sections: dict[str, list[str]] = {}
        current: str | None = None
        duplicate: set[str] = set()
        fence: tuple[str, int] | None = None
        for line in clean(comment).splitlines():
            marker = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
            if marker:
                value = marker[1]
                if fence is None:
                    fence = (value[0], len(value))
                elif value[0] == fence[0] and len(value) >= fence[1]:
                    fence = None
            if fence is None and re.match(r"^\s{0,3}#{1,2}\s+", line):
                current = None
                continue
            heading = None if fence else re.match(r"^\s{0,3}###\s+(.+?)\s*#*\s*$", line)
            if heading:
                current = heading[1].strip()
                if current in sections:
                    duplicate.add(current)
                sections.setdefault(current, [])
            elif current is not None:
                sections[current].append(line)
        errors = []
        for item in self.fields:
            value = clean("\n".join(sections.get(item.label, [])))
            if item.label in duplicate:
                errors.append(f"{item.label}：字段重复")
                continue
            if not meaningful(value):
                if item.required:
                    errors.append(f"{item.label}：必填项未填写")
                continue
            if item.kind == "input" and len(value.splitlines()) != 1:
                errors.append(f"{item.label}：应为单行文本")
            elif item.kind == "dropdown":
                selected = [v.strip() for v in re.split(r"[,\n]", value) if v.strip()]
                if (
                    not selected
                    or any(v not in item.options for v in selected)
                    or len(selected) != len(set(selected))
                    or (not item.multiple and len(selected) != 1)
                ):
                    errors.append(f"{item.label}：下拉选项不合法")
            elif item.kind == "checkboxes":
                choices = []
                invalid = False
                for line in value.splitlines():
                    if not line.strip():
                        continue
                    match = re.fullmatch(r"\s*[-*]\s+\[([ xX])\]\s+(.+?)\s*", line)
                    if not match or match[2] not in item.options:
                        invalid = True
                        break
                    if match[1].casefold() == "x":
                        choices.append(match[2])
                if invalid or len(choices) != len(set(choices)) or (item.required and not choices):
                    errors.append(f"{item.label}：复选项不合法或必选项未勾选")
        return errors

    def render(self) -> str:
        lines = ["## Bug 关闭登记", "", "<!-- 请填写每个必填字段；不要删除字段标题。 -->", ""]
        for item in self.fields:
            lines.extend([f"### {item.label}", ""])
            description = item.description.replace("-->", "")
            lines.extend([f"<!-- {'必填' if item.required else '可选'}；{description} -->", ""])
            if item.kind == "checkboxes":
                lines.extend(f"- [ ] {option}" for option in item.options)
            elif item.kind == "dropdown":
                lines.append(f"<!-- 可选值：{' / '.join(item.options)} -->")
            lines.extend(["", ""])
        return "\n".join(lines)
