#!/usr/bin/env node

/**
 * PM Agent SessionStart Hook
 *
 * Runs once when a Claude Code session starts. Injects ironclad instructions
 * into the system prompt — the AI reads these BEFORE EVERY RESPONSE because
 * they're part of its core system prompt.
 *
 * Claude Code hook contract:
 *   export async function sessionStart() -> { instructions?, environment? }
 *
 * Never crashes Claude Code — all errors are caught and return {}.
 */

export async function sessionStart() {
  try {
    const { resolveConfig, loadRules } = await import('./hook-utils.mjs');

    const config = resolveConfig();

    // Load active rules
    let rulesBlock = '(no rules loaded)';
    let projectName = 'this project';

    try {
      if (config?.rules?.config_path) {
        const rules = loadRules(config.rules.config_path);
        const activeRules = rules.filter(r => r.enabled !== false);
        if (activeRules.length > 0) {
          rulesBlock = activeRules
            .map(r => `  [${r.severity}] ${r.name}: ${r.description || r.trigger}`)
            .join('\n');
        } else {
          rulesBlock = '  (no active rules)';
        }
      }
      if (config?.project?.name) {
        projectName = config.project.name;
      }
    } catch {
      // Best-effort
    }

    return {
      instructions: `=== PM AGENT — MANDATORY PER-RESPONSE ENFORCEMENT ===
Project: ${projectName}

You are REQUIRED to follow PM Agent workflow on EVERY response. These are not suggestions — they are ENFORCED at the hook level.

▸ The PreToolUse hook BLOCKS EVERY tool call unless you call PM Agent tools first.
▸ You MUST call pm_get_context in EVERY response before using any other tool.
▸ Without pm_get_context, ALL Write, Read, Bash, Edit, and MCP tool calls are BLOCKED.
▸ Only AskUserQuestion and pm_* tools are allowed to pass through.

DECISION LOGGING IS PER-RESPONSE (NOT SESSION-PERSISTENT):
▸ pm_log_decision must be called in EVERY response before any write/destructive tool.
▸ The hook WILL BLOCK write tools if pm_log_decision hasn't been called in THIS response.
▸ Log ALL architectural decisions, feature implementations, and configuration changes.
▸ Not logging a decision before modifying files is a VIOLATION.

WORKFLOW FOR EVERY RESPONSE:
  1. pm_get_context — get project snapshot
  2. pm_get_blockers — check active blockers
  3. pm_log_decision — log what you intend to do (BEFORE writing files)
  4. Proceed with write tools (allowed only after step 3)
  5. pm_log_note — log insights discovered during work

Active project rules:
${rulesBlock}

FAILURE TO FOLLOW THIS WORKFLOW WILL RESULT IN BLOCKED OPERATIONS.
The PreToolUse hook enforces this at the tool-call level — you cannot bypass it.`,
      environment: {
        PM_AGENT_ACTIVE: 'true',
        PM_AGENT_ENFORCED: 'true',
        PM_AGENT_ENFORCEMENT: 'per-response',
        PM_AGENT_ENFORCEMENT_VERSION: '2.0',
        PM_AGENT_PROJECT: projectName,
        PM_AGENT_CONFIG: config?.rules?.config_path || '',
      },
    };
  } catch (e) {
    // Never crash Claude Code from a hook
    console.error('[PM Agent] SessionStart hook error:', e.message);
    return {};
  }
}
