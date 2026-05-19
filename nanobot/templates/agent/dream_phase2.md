Update memory files based on the analysis below.
- [FILE] entries: add the described content to the appropriate file
- [FILE-REMOVE] entries: delete the corresponding content from memory files
- [SKILL] entries: create a new skill under skills/<name>/SKILL.md using write_file
- Treat loose labels as file-scoped entries:
  - [MEMORY] / [MEMORY-REMOVE] => memory/MEMORY.md
  - [USER] / [USER-REMOVE] => USER.md
  - [SOUL] / [SOUL-REMOVE] => SOUL.md

## File paths (relative to workspace root)
- SOUL.md
- USER.md
- memory/MEMORY.md
- memory/topics/*.md (existing project/topic notes only)
- skills/<name>/SKILL.md (for [SKILL] entries only)

Do NOT guess paths.

## Editing rules
- Edit directly — file contents provided below, no read_file needed
- Use exact text as old_text, include surrounding blank lines for unique match
- Batch changes to the same file into one edit_file call
- For deletions: section header + all bullets as old_text, new_text empty
- Surgical edits only — never rewrite entire files
- For project progress, decisions, and active work streams, update the closest existing memory/topics/*.md file when one is provided in context.
- Do not create new memory/topics files unless explicitly instructed by the analysis.
- If the analysis contains actionable [FILE], [MEMORY], [USER], [SOUL], [FILE-REMOVE], [MEMORY-REMOVE], [USER-REMOVE], [SOUL-REMOVE], or [SKILL] entries, you must either:
  - update the appropriate file(s) with tools, or
  - explicitly refuse to update and explain why each actionable entry was not applied.
- If you update files, your final response must include:
  - the file path(s) changed;
  - the exact content added/removed/rewritten, summarized only when long;
  - the analysis entry or evidence that justified each update.
- Never claim a file was changed unless you actually called `edit_file` or `write_file` in this Phase 2 run and received a successful tool result.
- If you did not call a file-writing tool successfully, your final response must start with `No file updates` even if you believe updates should be made.
- If you do not update files, your final response must include:
  - "No file updates";
  - one reason per skipped actionable entry, e.g. duplicate, already present, stale, unsupported path, insufficient exact old_text, or unsafe change.
- If nothing in the analysis is actionable, stop without calling tools and state "No file updates: no actionable entries."

## Skill creation rules (for [SKILL] entries)
- Use write_file to create skills/<name>/SKILL.md
- Before writing, read_file `{{ skill_creator_path }}` for format reference (frontmatter structure, naming conventions, quality standards)
- **Dedup check**: read existing skills listed below to verify the new skill is not functionally redundant. Skip creation if an existing skill already covers the same workflow.
- Include YAML frontmatter with name and description fields
- Keep SKILL.md under 2000 words — concise and actionable
- Include: when to use, steps, output format, at least one example
- Do NOT overwrite existing skills — skip if the skill directory already exists
- Reference specific tools the agent has access to (read_file, write_file, exec, web_search, etc.)
- Skills are instruction sets, not code — do not include implementation code

## Quality
- Every line must carry standalone value
- Concise bullets under clear headers
- When reducing (not deleting): keep essential facts, drop verbose details
- If uncertain whether to delete, keep but add "(verify currency)"
