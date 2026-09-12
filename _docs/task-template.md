# Task template

Every groomed GitHub issue must use this structure.

```markdown
## Goal

One or two sentences describing what should be true when the task is complete.

## Acceptance criteria

- [ ] A checkable statement
- [ ] Another checkable statement
- [ ] Include important edge cases

## Out of scope

- Explicitly state what this task does not include
- Link to follow-up GitHub issues when appropriate

## Constraints

- Files/components affected
- Libraries or technologies to use
- Libraries or technologies to avoid
- Existing architectural decisions to preserve
- Relevant project documentation
```

## Quality bar for acceptance criteria

Acceptance criteria must be objectively verifiable.

Avoid vague criteria such as:

```text
- [ ] Make the UI better
- [ ] Improve performance
- [ ] Handle errors properly
```

Prefer criteria that can actually be tested or observed (commands, HTTP responses, DB state, UI behavior, failing tests for adversarial cases).
