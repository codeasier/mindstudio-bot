import pytest

from msbot.config import ConfigurationError, audit_configuration
from msbot.forms import IssueForm

from .conftest import CONFIG, TEMPLATE, VALID_COMMENT


def test_user_template_and_complete_registration():
    form = IssueForm.parse(TEMPLATE)
    assert form.validate(VALID_COMMENT) == []
    assert len(form.fields) == 12


def test_generated_blank_registration_is_not_complete():
    form = IssueForm.parse(TEMPLATE)
    assert len(form.validate(form.render())) == sum(f.required for f in form.fields)


@pytest.mark.parametrize("replacement", ["", "_No response_", "<!-- 请填写 -->", "待填写"])
def test_empty_required_values_fail(replacement):
    form = IssueForm.parse(TEMPLATE)
    invalid = VALID_COMMENT.replace("v1.2.0", replacement)
    errors = form.validate(invalid)
    assert any("里程碑" in error for error in errors)
    assert any("包名 / 版本号 / 包链接" in error for error in errors)


@pytest.mark.parametrize("value", ["unknown", "daily, tag", "daily\ntag"])
def test_invalid_dropdown(value):
    comment = VALID_COMMENT.replace("\ntag\n", f"\n{value}\n")
    assert any("回归包类型" in error for error in IssueForm.parse(TEMPLATE).validate(comment))


def test_optional_checkbox_options_are_still_validated():
    comment = VALID_COMMENT.replace("- [ ] UI 变化", "- [x] 不存在的类型")
    assert any("可感知变更类型" in error for error in IssueForm.parse(TEMPLATE).validate(comment))


def test_duplicate_field_rejected_in_comment():
    errors = IssueForm.parse(TEMPLATE).validate(VALID_COMMENT + "\n### 里程碑\n\nv2.0\n")
    assert "里程碑：字段重复" in errors


def test_code_block_headings_cannot_supply_missing_fields():
    wrapped = "```markdown\n" + VALID_COMMENT + "\n```"
    assert IssueForm.parse(TEMPLATE).validate(wrapped)


def test_section_headings_do_not_become_input_values():
    comment = VALID_COMMENT.replace("### 里程碑\n\nv1.2.0", "### 里程碑\n\n## 原因分析")
    assert "里程碑：必填项未填写" in IssueForm.parse(TEMPLATE).validate(comment)


def test_markdown_heading_alone_is_not_required_content():
    comment = VALID_COMMENT.replace(
        "切换项目后仍复用旧缓存，触发查询时返回上一个项目的数据；影响项目切换后的首次查询。",
        "#### 原因分析（待填写）",
    )
    assert "引入缺陷的原因：必填项未填写" in IssueForm.parse(TEMPLATE).validate(comment)


@pytest.mark.parametrize(
    "source",
    [
        TEMPLATE.replace("id: root_cause", "id: defect_introduced_by"),
        TEMPLATE.replace("type: textarea", "type: arbitrary-handler", 1),
        TEMPLATE.replace('options: ["daily", "tag"]', 'options: ["daily", "daily"]'),
        "body: []",
    ],
)
def test_invalid_templates_cannot_activate(source):
    with pytest.raises(ConfigurationError):
        IssueForm.parse(source)


@pytest.mark.parametrize(
    "source",
    [
        CONFIG.replace("enabled: true", 'enabled: "true"'),
        CONFIG.replace(".msbot/template/bug-close.yml", ".msbot/../secrets.yml"),
        CONFIG.replace("enabled: true", "enabled: true\n    enabled: false"),
        CONFIG.replace("enabled: true", "enabled: true\n    unknown: true"),
    ],
)
def test_invalid_repository_configuration(source):
    with pytest.raises(ConfigurationError):
        audit_configuration(source)
