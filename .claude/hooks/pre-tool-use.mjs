#!/usr/bin/env node

/**
 * PM Agent PreToolUse Hook — ZERO-STATE GATEKEEPER + RULES ENFORCEMENT
 *
 * Runs before EVERY Claude Code tool call.
 *
 * ENFORCEMENT MODEL:
 * ┌───────────────────────────────────────────────────────────────┐
 * │ EVERY tool call is evaluated INDEPENDENTLY — zero session    │
 * │ state carries over between hook invocations.                 │
 * │                                                               │
 * │ The ONLY tools that pass through without context:             │
 * │   • SessionStart hook (tool === undefined)                    │
 * │   • AskUserQuestion — needed for user interaction             │
 * │   • pm_get_context — this is what sets contextChecked         │
 * │   • All other pm_* MCP tools (blocked from setting context)   │
 * │                                                               │
 * │ EVERY OTHER TOOL is BLOCKED unless contextChecked is true.    │
 * │                                                               │
 * │ BOTH flags are RESET after every hook invocation              │
 * │ EXCEPT the tool that sets them. This means:                   │
 * │   • pm_get_context → contextChecked = true, rest of batch OK  │
 * │   • pm_log_decision → decisionLogged = true, rest of batch OK │
 * │   • All other pm_* tools reset BOTH flags (clean slate)       │
 * │   • Non-PM-agent tools pass only if BOTH flags are true       │
 * │   • Response boundaries CANNOT leak — every response starts   │
 * │     with both flags false, must call pm_get_context and        │
 * │     pm_log_decision fresh every time.                         │
 * │                                                               │
 * │ Write/destructive tools additionally require pm_log_decision  │
 * │ in the SAME response — it's not persistent across responses.  │
 * │                                                               │
 * │ RULES ENGINE: All tools (including pm_*) are evaluated        │
 * │ against the rules.toml file on every single call. If a hard   │
 * │ rule fires, the tool is blocked. If a soft/info rule fires,   │
 * │ the user sees the notification/suggestion.                    │
 * └───────────────────────────────────────────────────────────────┘
 *
 * Claude Code hook contract:
 *   export async function preToolUse({ tool, input }) -> { autoApproval?, reason? }
 *
 * Never crashes Claude Code — all errors are caught and return {}.
 */

// ---------------------------------------------------------------------------
// Dependencies
// ---------------------------------------------------------------------------

import { evaluateRules, wrapResult } from './hook-utils.mjs';

// ---------------------------------------------------------------------------
// Module-level state (persists within a single Node.js process)
// ---------------------------------------------------------------------------

/** Whether pm_get_context has been set in this batch. Reset to false at every hook return. */
let contextChecked = false;

/** Whether pm_log_decision has been called in this batch. Reset to false at every hook return. */
let decisionLogged = false;

// ---------------------------------------------------------------------------
// Tool classification
// ---------------------------------------------------------------------------

const BYPASS_TOOLS = new Set([
  'AskUserQuestion',
]);

const WRITE_TOOLS = new Set([
  'Write', 'Edit', 'Bash', 'Delete', 'Rename', 'Move', 'NotebookEdit',
]);

const PM_AGENT_TOOLS = [
  'pm_get_context',
  'pm_get_blockers',
  'pm_get_decisions',
  'pm_get_notes',
  'pm_get_scope',
  'pm_get_standup',
  'pm_prep_meeting',
  'pm_log_decision',
  'pm_log_note',
  'pm_check_scope',
  'pm_add_rule',
  'pm_enforce_rules',
  'pm_scan_codebase',
  'pm_get_dependency_graph',
  'pm_analyze_impact',
  'pm_search_codebase',
  'pm_get_architecture',
  'pm_get_file_context',
  'pm_hooks_setup',
  'pm_enforce_setup',
  'pm_understand_codebase',
];

// ---------------------------------------------------------------------------
// Block messages
// ---------------------------------------------------------------------------

const GATEKEEPER_MESSAGE = `[PM Agent — GATEKEEPER] Action blocked.

╔══════════════════════════════════════════════════════════════╗
║ You attempted to use a tool WITHOUT calling pm_get_context  ║
║ first in this response.                                     ║
║                                                              ║
║ ALL NON-PM-AGENT TOOL CALLS ARE BLOCKED.                    ║
║                                                              ║
║ CALL THIS FIRST (EVERY RESPONSE):  pm_get_context             ║
║                                                              ║
║ This tool provides the full project snapshot:                ║
║   • Architecture decisions                                   ║
║   • Active blockers                                          ║
║   • Notes and context                                        ║
║   • Project rules                                            ║
║   • Codebase structure                                       ║
║                                                              ║
║ Without it, you CANNOT use ANY tool.                         ║
║ Call pm_get_context to proceed.                              ║
╚══════════════════════════════════════════════════════════════╝

Required:  pm_get_context
Or CLI:    ! pm context

This cannot be bypassed. Call pm_get_context as the FIRST tool call in every response.`;

const DECISION_MESSAGE = `[PM Agent — GATEKEEPER] Write action blocked.

╔══════════════════════════════════════════════════════════════╗
║ You attempted to modify project files WITHOUT logging       ║
║ a decision first.                                           ║
║                                                              ║
║ CALL THIS FIRST:   pm_log_decision                           ║
║                                                              ║
║ Log what you are about to do and why:                        ║
║                                                              ║
║   pm_log_decision({                                          ║
║     title: "Implement user auth flow",                       ║
║     body: "Adding JWT middleware per ADR-003"                ║
║   })                                                         ║
║                                                              ║
║ Once the decision is logged, write tools are unblocked.      ║
╚══════════════════════════════════════════════════════════════╝

Required:  pm_log_decision
Or CLI:    ! pm log-decision "title" "body"`;

// ---------------------------------------------------------------------------
// Helper: evaluate rules and block if hard rule fires
// ---------------------------------------------------------------------------

/**
 * Evaluate PM Agent rules for the current tool call.
 * Returns { blocked: true, reason } if a hard rule blocks, or
 * { blocked: true, reason } if a soft/info rule fires a warning,
 * or {} if no rules fire or rules are disabled.
 */
function checkRules(toolName, toolInput) {
  try {
    const ruleResult = evaluateRules(toolName, toolInput || {});
    const wrapped = wrapResult(ruleResult.blocked, ruleResult.actions);
    if (wrapped.autoApproval === false) {
      return { blocked: true, reason: wrapped.reason };
    }
    return { blocked: false };
  } catch (e) {
    console.error('[PM Agent] Rule evaluation error:', e.message);
    return { blocked: false };
  }
}

// ---------------------------------------------------------------------------
// Main hook
// ---------------------------------------------------------------------------

export async function preToolUse({ tool, input } = {}) {
  try {
    // Step 1: SessionStart hook (no tool yet) — always pass through
    if (!tool) {
      return {};
    }

    // Step 2: Tools that always pass through unconditionally
    if (BYPASS_TOOLS.has(tool)) {
      return {};
    }

    // Step 3: PM Agent MCP tools — track state and evaluate rules
    if (tool.startsWith('pm_') || tool.startsWith('pm-')) {
      // Track pm_log_decision calls
      if (tool === 'pm_log_decision') {
        decisionLogged = true;
      }
      // pm_get_context sets contextChecked for subsequent tools in this batch
      if (tool === 'pm_get_context') {
        contextChecked = true;
      }
      // CRITICAL: every non-state-setting pm_* tool wipes both state flags
      // to prevent state bleed across response boundaries. If the AI ends a
      // response with pm_log_note, both flags are false for the next response.
      if (tool !== 'pm_get_context') {
        contextChecked = false;
      }
      if (tool !== 'pm_log_decision') {
        decisionLogged = false;
      }
      // Evaluate rules against this pm_* tool call
      const ruled = checkRules(tool, input);
      if (ruled.blocked) {
        return { autoApproval: false, reason: ruled.reason };
      }
      // Show visible confirmation — user sees PM Agent is active on every response
      if (tool === 'pm_get_context') {
        return { autoApproval: true, reason: '[PM Agent ✓] Context loaded — tools unblocked.' };
      }
      if (tool === 'pm_log_decision') {
        return { autoApproval: true, reason: '[PM Agent ✓] Decision logged — writes unblocked.' };
      }
      return { autoApproval: true, reason: '[PM Agent ✓]' };
    }

    // Step 4: Check MCP server tool calls via their input pattern
    if (input && typeof input === 'object') {
      const inputStr = JSON.stringify(input).toLowerCase();
      for (const pmTool of PM_AGENT_TOOLS) {
        if (inputStr.includes(pmTool.toLowerCase())) {
          if (pmTool === 'pm_log_decision') {
            decisionLogged = true;
          }
          if (pmTool === 'pm_get_context') {
            contextChecked = true;
          }
          // Same principle: non-state-setting pm_* tools reset both flags
          if (pmTool !== 'pm_get_context') {
            contextChecked = false;
          }
          if (pmTool !== 'pm_log_decision') {
            decisionLogged = false;
          }
          // Evaluate rules against the detected pm_* tool call
          const ruled = checkRules(pmTool, input);
          if (ruled.blocked) {
            return { autoApproval: false, reason: ruled.reason };
          }
          if (pmTool === 'pm_get_context') {
            return { autoApproval: true, reason: '[PM Agent ✓] Context loaded — tools unblocked.' };
          }
          if (pmTool === 'pm_log_decision') {
            return { autoApproval: true, reason: '[PM Agent ✓] Decision logged — writes unblocked.' };
          }
          return { autoApproval: true, reason: '[PM Agent ✓]' };
        }
      }
    }

    // Step 5: Context check — pm_get_context MUST have been called
    // earlier in this batch. contextChecked is always false at the
    // start of a new response, so the AI MUST call pm_get_context
    // before every single tool use.
    if (!contextChecked) {
      return {
        autoApproval: false,
        reason: GATEKEEPER_MESSAGE,
      };
    }

    // Step 6: Evaluate rules for this non-PM-agent tool call
    const ruled = checkRules(tool, input);
    if (ruled.blocked) {
      return { autoApproval: false, reason: ruled.reason };
    }

    // Step 7: Decision check — write tools need pm_log_decision
    if (WRITE_TOOLS.has(tool) && !decisionLogged) {
      return {
        autoApproval: false,
        reason: DECISION_MESSAGE,
      };
    }

    // Step 8: Allow through — reset both flags so the next response
    // starts clean. This non-PM-agent tool consumed the context; the AI
    // must call pm_get_context and pm_log_decision again in the next response.
    contextChecked = false;
    decisionLogged = false;
    return { autoApproval: true, reason: '[PM Agent ✓] Allowed.' };
  } catch (e) {
    // Never crash Claude Code from a hook
    console.error('[PM Agent] GATEKEEPER hook error:', e.message);
    return {};
  }
}
