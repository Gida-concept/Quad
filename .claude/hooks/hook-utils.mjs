#!/usr/bin/env node

/**
 * PM Agent Hook Utilities
 *
 * Shared utilities for PreToolUse and SessionStart Claude Code hooks.
 * Designed to never crash Claude Code — all errors are caught and
 * result in graceful fallbacks.
 *
 * This module is fully self-contained — no dependencies on @gida-concept/pm-agent-core.
 * All expression evaluation and rule engine logic is inlined.
 *
 * @module hook-utils
 */

import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';

// ---------------------------------------------------------------------------
// Config resolution
// ---------------------------------------------------------------------------

/**
 * Resolve the PM Agent config file path.
 *
 * Lookup order:
 * 1. PM_AGENT_CONFIG environment variable (explicit override)
 * 2. project/.pm-agent/config.toml (project-local — this is the default)
 * 3. ~/.config/pm-agent/config.toml (global fallback)
 *
 * @returns {object|null} Parsed config object, or null if not found
 */
export function resolveConfig() {
  try {
    // 1. Check explicit env var
    if (process.env.PM_AGENT_CONFIG) {
      const cfgPath = process.env.PM_AGENT_CONFIG;
      if (fs.existsSync(cfgPath)) {
        return parseTomlFile(cfgPath);
      }
    }

    // 2. Check project-local .pm-agent/config.toml (cwd)
    const cwd = process.cwd();
    const localConfigPath = path.join(cwd, '.pm-agent', 'config.toml');
    if (fs.existsSync(localConfigPath)) {
      return parseTomlFile(localConfigPath);
    }

    // 3. Fallback to global ~/.config/pm-agent/config.toml
    const home = process.env.HOME || process.env.USERPROFILE || '~';
    const globalConfigPath = path.resolve(home.replace(/^~/, home), '.config', 'pm-agent', 'config.toml');
    if (fs.existsSync(globalConfigPath)) {
      return parseTomlFile(globalConfigPath);
    }

    return null;
  } catch (e) {
    console.error('[PM Agent Hook] Config resolution error:', e.message);
    return null;
  }
}

/**
 * Parse a TOML file using a minimal inline parser.
 * Optionally tries the `toml` npm package first (if available).
 *
 * @param {string} filePath
 * @returns {object|null}
 */
function parseTomlFile(filePath) {
  try {
    const raw = fs.readFileSync(filePath, 'utf-8');
    if (!raw.trim()) return null;

    // Try using the `toml` package if available
    try {
      const require = createRequire(import.meta.url);
      const toml = require('toml');
      return toml.parse(raw);
    } catch {
      // Fallback: minimal TOML parser for all config fields
      return minimalTomlParse(raw);
    }
  } catch {
    return null;
  }
}

/**
 * Minimal TOML parser that extracts top-level sections, string values,
 * booleans, and [[array-of-tables]] entries.
 * Only handles the subset of TOML used in PM Agent config and rules files.
 *
 * @param {string} raw
 * @returns {object}
 */
function minimalTomlParse(raw) {
  const result = {};
  let currentSection = result;

  for (const line of raw.split('\n')) {
    const trimmed = line.trim();

    // Skip comments and blank lines
    if (!trimmed || trimmed.startsWith('#')) continue;

    // Array of tables: [[rule]]
    const arrayMatch = trimmed.match(/^\[\[([^\]]+)\]\]$/);
    if (arrayMatch) {
      const name = arrayMatch[1];
      if (!result[name]) result[name] = [];
      const entry = {};
      result[name].push(entry);
      currentSection = entry;
      continue;
    }

    // Section header: [section] or [section.subsection]
    const sectionMatch = trimmed.match(/^\[([^\]]+)\]$/);
    if (sectionMatch) {
      const parts = sectionMatch[1].split('.');
      let obj = result;
      for (const part of parts) {
        if (!obj[part]) obj[part] = {};
        obj = obj[part];
      }
      currentSection = obj;
      continue;
    }

    // Key = "value"
    const kvMatch = trimmed.match(/^([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*"([^"]*)"$/);
    if (kvMatch) {
      currentSection[kvMatch[1]] = kvMatch[2];
      continue;
    }

    // Key = true/false
    const boolMatch = trimmed.match(/^([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*(true|false)$/);
    if (boolMatch) {
      currentSection[boolMatch[1]] = boolMatch[2] === 'true';
      continue;
    }
  }

  return result;
}

// ---------------------------------------------------------------------------
// Inline Expression Evaluator
// Ported from packages/core/src/rules/expression.ts
// ---------------------------------------------------------------------------

const MAX_RECURSION_DEPTH = 50;

const OP_PRECEDENCE = {
  '||': 1,
  '&&': 2,
};

function tokenize(input) {
  const tokens = [];
  let pos = 0;

  while (pos < input.length) {
    const ch = input[pos];

    // Skip whitespace
    if (ch !== undefined && /^\s$/.test(ch)) {
      pos++;
      continue;
    }

    if (ch === undefined) break;

    // Single-character tokens
    if (ch === '.') {
      tokens.push({ type: 'DOT', value: '.', position: pos });
      pos++;
      continue;
    }

    if (ch === '(') {
      tokens.push({ type: 'LPAREN', value: '(', position: pos });
      pos++;
      continue;
    }

    if (ch === ')') {
      tokens.push({ type: 'RPAREN', value: ')', position: pos });
      pos++;
      continue;
    }

    // Multi-character operators
    if (ch === '=' && input[pos + 1] === '=') {
      tokens.push({ type: 'EQ', value: '==', position: pos });
      pos += 2;
      continue;
    }

    if (ch === '!' && input[pos + 1] === '=') {
      tokens.push({ type: 'NEQ', value: '!=', position: pos });
      pos += 2;
      continue;
    }

    if (ch === '>' && input[pos + 1] === '=') {
      tokens.push({ type: 'GTE', value: '>=', position: pos });
      pos += 2;
      continue;
    }

    if (ch === '<' && input[pos + 1] === '=') {
      tokens.push({ type: 'LTE', value: '<=', position: pos });
      pos += 2;
      continue;
    }

    if (ch === '>') {
      tokens.push({ type: 'GT', value: '>', position: pos });
      pos++;
      continue;
    }

    if (ch === '<') {
      tokens.push({ type: 'LT', value: '<', position: pos });
      pos++;
      continue;
    }

    if (ch === '&' && input[pos + 1] === '&') {
      tokens.push({ type: 'AND', value: '&&', position: pos });
      pos += 2;
      continue;
    }

    if (ch === '|' && input[pos + 1] === '|') {
      tokens.push({ type: 'OR', value: '||', position: pos });
      pos += 2;
      continue;
    }

    // String literal (single-quoted)
    if (ch === "'") {
      const start = pos;
      pos++;
      let value = '';
      while (pos < input.length) {
        const sc = input[pos];
        if (sc === "'") {
          break;
        }
        value += sc;
        pos++;
      }
      if (pos >= input.length) {
        throw new Error(`Unterminated string literal at position ${start}`);
      }
      pos++; // skip closing quote
      tokens.push({ type: 'STRING', value, position: start });
      continue;
    }

    // Number or duration literal
    if (/^[0-9]$/.test(ch)) {
      const start = pos;
      let numStr = '';
      while (pos < input.length && /^[0-9]$/.test(input[pos] ?? '')) {
        numStr += input[pos];
        pos++;
      }
      // Decimal part
      if (
        pos < input.length &&
        input[pos] === '.' &&
        pos + 1 < input.length &&
        /^[0-9]$/.test(input[pos + 1] ?? '')
      ) {
        numStr += '.';
        pos++;
        while (pos < input.length && /^[0-9]$/.test(input[pos] ?? '')) {
          numStr += input[pos];
          pos++;
        }
      }

      // Check for duration unit (h, d, or m)
      const nextCh = input[pos];
      if (nextCh !== undefined && /^[hdm]$/.test(nextCh)) {
        // Ensure it's NOT followed by more alpha characters
        const afterUnit = input[pos + 1];
        if (afterUnit === undefined || !/^[a-zA-Z]$/.test(afterUnit)) {
          const unit = nextCh;
          const value = parseFloat(numStr);
          tokens.push({ type: 'DURATION', value: numStr + unit, position: start });
          pos++;
          continue;
        }
      }

      tokens.push({ type: 'NUMBER', value: numStr, position: start });
      continue;
    }

    // Identifier
    if (/^[a-zA-Z_]$/.test(ch)) {
      const start = pos;
      let value = '';
      while (pos < input.length && /^[a-zA-Z0-9_]$/.test(input[pos] ?? '')) {
        value += input[pos];
        pos++;
      }
      tokens.push({ type: 'IDENTIFIER', value, position: start });
      continue;
    }

    throw new Error(`Unexpected character '${ch}' at position ${pos}`);
  }

  tokens.push({ type: 'EOF', value: '', position: pos });
  return tokens;
}

function parse(tokens) {
  const result = parseExpression(tokens, 0, 0, 0);
  return result.node;
}

function parseExpression(tokens, pos, minPrecedence, depth) {
  if (depth > MAX_RECURSION_DEPTH) {
    throw new Error('Maximum recursion depth exceeded in expression parser');
  }

  let current = parseComparison(tokens, pos, depth + 1);

  while (current.pos < tokens.length) {
    const token = tokens[current.pos];
    if (token === undefined) break;
    if (token.type !== 'AND' && token.type !== 'OR') break;

    const prec = OP_PRECEDENCE[token.value];
    if (prec === undefined || prec < minPrecedence) break;

    // Consume operator
    current.pos++;

    const right = parseExpression(tokens, current.pos, prec + 1, depth + 1);
    current = {
      node: { type: 'BinaryOp', operator: token.value, left: current.node, right: right.node },
      pos: right.pos,
    };
  }

  return current;
}

function parseComparison(tokens, pos, depth) {
  if (depth > MAX_RECURSION_DEPTH) {
    throw new Error('Maximum recursion depth exceeded in expression parser');
  }

  let current = parseUnary(tokens, pos, depth + 1);

  while (current.pos < tokens.length) {
    const token = tokens[current.pos];
    if (token === undefined) break;
    if (
      token.type !== 'EQ' &&
      token.type !== 'NEQ' &&
      token.type !== 'GT' &&
      token.type !== 'LT' &&
      token.type !== 'GTE' &&
      token.type !== 'LTE'
    ) {
      break;
    }

    current.pos++;
    const right = parseUnary(tokens, current.pos, depth + 1);
    current = {
      node: { type: 'BinaryOp', operator: token.value, left: current.node, right: right.node },
      pos: right.pos,
    };
  }

  return current;
}

function parseUnary(tokens, pos, depth) {
  if (depth > MAX_RECURSION_DEPTH) {
    throw new Error('Maximum recursion depth exceeded in expression parser');
  }

  const token = tokens[pos];
  if (token === undefined) {
    throw new Error(`Unexpected end of input at position ${pos}`);
  }

  // Parenthesized expression
  if (token.type === 'LPAREN') {
    const inner = parseExpression(tokens, pos + 1, 0, depth + 1);
    const rparen = tokens[inner.pos];
    if (rparen === undefined || rparen.type !== 'RPAREN') {
      throw new Error(`Expected ')' at position ${inner.pos}`);
    }
    return { node: inner.node, pos: inner.pos + 1 };
  }

  // String literal
  if (token.type === 'STRING') {
    return { node: { type: 'StringLiteral', value: token.value }, pos: pos + 1 };
  }

  // Number literal
  if (token.type === 'NUMBER') {
    return { node: { type: 'NumberLiteral', value: parseFloat(token.value) }, pos: pos + 1 };
  }

  // Duration literal
  if (token.type === 'DURATION') {
    const raw = token.value;
    const unit = raw[raw.length - 1];
    const numVal = parseFloat(raw.slice(0, -1));
    let normalizedHours;
    switch (unit) {
      case 'h':
        normalizedHours = numVal;
        break;
      case 'd':
        normalizedHours = numVal * 24;
        break;
      case 'm':
        normalizedHours = numVal / 60;
        break;
    }
    return {
      node: { type: 'DurationLiteral', value: numVal, unit, normalizedHours },
      pos: pos + 1,
    };
  }

  // Identifier — property access or function call
  if (token.type === 'IDENTIFIER') {
    return parsePropertyOrCall(tokens, pos, depth + 1);
  }

  throw new Error(`Unexpected token '${token.value}' at position ${token.position}`);
}

function parsePropertyOrCall(tokens, pos, depth) {
  const firstToken = tokens[pos];
  if (firstToken === undefined) {
    throw new Error(`Unexpected end of input at position ${pos}`);
  }

  const path = [firstToken.value];
  let currentPos = pos + 1;

  while (currentPos < tokens.length) {
    const dot = tokens[currentPos];
    if (dot === undefined || dot.type !== 'DOT') break;

    currentPos++;

    const ident = tokens[currentPos];
    if (ident === undefined || ident.type !== 'IDENTIFIER') {
      throw new Error(`Expected identifier after '.' at position ${currentPos}`);
    }
    currentPos++;

    // Check if this is a function call: .identifier(
    if (currentPos < tokens.length) {
      const lparen = tokens[currentPos];
      if (lparen !== undefined && lparen.type === 'LPAREN') {
        currentPos++; // skip (
        let args = [];
        if (currentPos < tokens.length) {
          const rp = tokens[currentPos];
          if (rp === undefined || rp.type !== 'RPAREN') {
            const arg = parseExpression(tokens, currentPos, 0, depth + 1);
            args = [arg.node];
            currentPos = arg.pos;
          }
        }
        const rparen = tokens[currentPos];
        if (rparen === undefined || rparen.type !== 'RPAREN') {
          throw new Error(`Expected ')' at position ${currentPos}`);
        }
        currentPos++;
        return {
          node: {
            type: 'FunctionCall',
            object: path,
            function: ident.value,
            args,
          },
          pos: currentPos,
        };
      }
    }

    // Regular property access
    path.push(ident.value);
  }

  return { node: { type: 'PropertyAccess', path }, pos: currentPos };
}

function evaluateAst(ast, context) {
  switch (ast.type) {
    case 'StringLiteral':
      return ast.value;

    case 'NumberLiteral':
      return ast.value;

    case 'DurationLiteral':
      // Return the node itself so comparisons can access normalizedHours
      return ast;

    case 'PropertyAccess':
      return evalPropertyAccess(ast.path, context);

    case 'FunctionCall':
      return evalFunctionCall(ast.object, ast.function, ast.args, context);

    case 'BinaryOp':
      return evalBinaryOp(ast.operator, ast.left, ast.right, context);
  }
}

function evalPropertyAccess(path, context) {
  // Special handling for .count suffix (e.g., files.count)
  if (path.length > 1) {
    const lastSegment = path[path.length - 1];
    if (lastSegment === 'count') {
      let value = context;
      for (let i = 0; i < path.length - 1; i++) {
        const segment = path[i];
        if (segment === undefined) break;
        if (value === null || value === undefined || typeof value !== 'object') {
          return 0;
        }
        value = value[segment];
      }
      if (Array.isArray(value)) {
        return value.length;
      }
      return 0;
    }
  }

  // Normal property traversal
  let value = context;
  for (const segment of path) {
    if (value === null || value === undefined || typeof value !== 'object') {
      return null;
    }
    value = value[segment];
  }
  return value ?? null;
}

function evalFunctionCall(objectPath, func, args, context) {
  // Resolve the object value by traversing the path
  let value = context;
  for (const segment of objectPath) {
    if (value === null || value === undefined || typeof value !== 'object') {
      return func === 'contains' ? false : null;
    }
    value = value[segment];
  }

  if (func === 'contains') {
    if (value === null || value === undefined) return false;
    const argNode = args[0];
    const argValue = argNode !== undefined ? evaluateAst(argNode, context) : undefined;
    if (typeof value === 'string') {
      return value.includes(String(argValue));
    }
    if (Array.isArray(value)) {
      return value.includes(argValue);
    }
    return false;
  }

  return null;
}

function evalBinaryOp(operator, left, right, context) {
  const leftVal = evaluateAst(left, context);
  const rightVal = evaluateAst(right, context);

  // null/undefined comparisons return false
  if (leftVal === null || leftVal === undefined) return false;
  if (rightVal === null || rightVal === undefined) return false;

  switch (operator) {
    case '&&':
      return Boolean(leftVal) && Boolean(rightVal);
    case '||':
      return Boolean(leftVal) || Boolean(rightVal);
    case '==':
      return compareEqual(leftVal, rightVal);
    case '!=':
      return !compareEqual(leftVal, rightVal);
    case '>':
      return compareOrdered(leftVal, rightVal) > 0;
    case '<':
      return compareOrdered(leftVal, rightVal) < 0;
    case '>=':
      return compareOrdered(leftVal, rightVal) >= 0;
    case '<=':
      return compareOrdered(leftVal, rightVal) <= 0;
    default:
      return false;
  }
}

function compareEqual(left, right) {
  // Duration comparisons — normalize to hours
  if (isDurationNode(left) && isDurationNode(right)) {
    return left.normalizedHours === right.normalizedHours;
  }
  if (isDurationNode(left) && typeof right === 'number') {
    return left.normalizedHours === right;
  }
  if (isDurationNode(right) && typeof left === 'number') {
    return left === right.normalizedHours;
  }

  // Glob matching for string comparisons against patterns
  if (typeof left === 'string' && typeof right === 'string' && hasGlobChars(right)) {
    return simpleGlobMatch(left, right);
  }

  return left === right;
}

function compareOrdered(left, right) {
  // Duration comparisons — normalize to hours
  if (isDurationNode(left) && isDurationNode(right)) {
    return left.normalizedHours - right.normalizedHours;
  }
  if (isDurationNode(left) && typeof right === 'number') {
    return left.normalizedHours - right;
  }
  if (isDurationNode(right) && typeof left === 'number') {
    return left - right.normalizedHours;
  }

  // Number comparison
  if (typeof left === 'number' && typeof right === 'number') {
    return left - right;
  }

  // String comparison
  return String(left).localeCompare(String(right));
}

function isDurationNode(val) {
  return (
    typeof val === 'object' &&
    val !== null &&
    val.type === 'DurationLiteral'
  );
}

function hasGlobChars(s) {
  return s.includes('*') || s.includes('?') || s.includes('[');
}

/**
 * Simple glob matcher — replaces minimatch for inline use.
 * Supports *, **, and ? glob patterns.
 */
function simpleGlobMatch(str, pattern) {
  if (!pattern.includes('*') && !pattern.includes('?') && !pattern.includes('[')) {
    return str === pattern;
  }
  let regex = '';
  let i = 0;
  while (i < pattern.length) {
    const ch = pattern[i];
    if (ch === '*') {
      if (i + 1 < pattern.length && pattern[i + 1] === '*') {
        regex += '.*';
        i += 2;
      } else {
        regex += '[^/]*';
        i++;
      }
    } else if (ch === '?') {
      regex += '[^/]';
      i++;
    } else if ('.^${}()|\\+'.includes(ch)) {
      regex += '\\' + ch;
      i++;
    } else {
      regex += ch;
      i++;
    }
  }
  try {
    return new RegExp('^' + regex + '$').test(str);
  } catch {
    return false;
  }
}

// ---------------------------------------------------------------------------
// Template interpolator
// ---------------------------------------------------------------------------

export function interpolate(template, context) {
  return template.replace(/\{([^}]+)\}/g, (match, pathStr) => {
    const parts = pathStr.trim().split('.');
    let value = context;
    for (const part of parts) {
      if (value === null || value === undefined || typeof value !== 'object') {
        return match;
      }
      value = value[part];
    }
    if (value !== undefined && value !== null) {
      return String(value);
    }
    return match;
  });
}

// ---------------------------------------------------------------------------
// Inline Rule Engine
// Ported from packages/core/src/rules/engine.ts (pure functions only)
// ---------------------------------------------------------------------------

/**
 * Parse an action string into its type and message template.
 * "block: 'Cannot close {ticket.id}'" -> { type: 'block', message: 'Cannot close {ticket.id}' }
 */
function parseAction(actionStr) {
  const colonIdx = actionStr.indexOf(':');
  if (colonIdx === -1) {
    // default to notify if no colon separator
    return { type: 'notify', message: actionStr.trim() };
  }

  const typeStr = actionStr.slice(0, colonIdx).trim();
  const type = ['block', 'confirm', 'notify', 'suggest'].includes(typeStr)
    ? typeStr
    : 'notify';

  let message = actionStr.slice(colonIdx + 1).trim();
  // Strip surrounding quotes if present
  if (
    (message.startsWith("'") && message.endsWith("'")) ||
    (message.startsWith('"') && message.endsWith('"'))
  ) {
    message = message.slice(1, -1);
  }

  return { type, message };
}

/**
 * Evaluate a single rule against context.
 */
function evaluateRule(rule, context) {
  // Parse and evaluate trigger
  try {
    const triggerTokens = tokenize(rule.trigger);
    const triggerAst = parse(triggerTokens);
    const triggerResult = evaluateAst(triggerAst, context);

    // If trigger is false-y, rule doesn't fire
    if (!triggerResult) {
      return {
        rule: rule.name,
        severity: rule.severity,
        action: 'notify',
        message: '',
        triggered: false,
        passed: true,
      };
    }
  } catch (e) {
    // If trigger parsing/evaluation fails, rule doesn't fire (graceful degradation)
    console.warn(`[pm-agent] Failed to evaluate trigger for rule '${rule.name}': ${e}`);
    return {
      rule: rule.name,
      severity: rule.severity,
      action: 'notify',
      message: '',
      triggered: false,
      passed: true,
    };
  }

  // Trigger matched — check condition if present
  if (rule.condition) {
    try {
      const condTokens = tokenize(rule.condition);
      const condAst = parse(condTokens);
      const condResult = evaluateAst(condAst, context);

      if (!condResult) {
        // Condition not met — rule doesn't fire
        return {
          rule: rule.name,
          severity: rule.severity,
          action: 'notify',
          message: '',
          triggered: false,
          passed: true,
        };
      }
    } catch (e) {
      console.warn(`[pm-agent] Failed to evaluate condition for rule '${rule.name}': ${e}`);
      return {
        rule: rule.name,
        severity: rule.severity,
        action: 'notify',
        message: '',
        triggered: false,
        passed: true,
      };
    }
  }

  // Rule fired — parse action
  const parsed = parseAction(rule.action);
  const message = interpolate(parsed.message, context);

  const isHardBlock = parsed.type === 'block';

  return {
    rule: rule.name,
    severity: rule.severity,
    action: parsed.type,
    message,
    triggered: true,
    passed: !isHardBlock,
  };
}

/**
 * Pure function: evaluate rules against a context object.
 * Takes pre-loaded rules and a context object.
 *
 * @param {Array} rules - Array of rule objects
 * @param {object} context - Evaluation context
 * @returns {{ blocked: boolean, results: Array, rules_evaluated: number, rules_triggered: number, rules_blocked: number, status: string, confirmation_required: boolean }}
 */
function pureEvaluateRules(rules, context) {
  const results = [];
  let blocked = false;

  // Sort by severity: hard first, then soft, then info
  const severityOrder = { hard: 0, soft: 1, info: 2 };
  const sorted = [...rules].sort((a, b) => severityOrder[a.severity] - severityOrder[b.severity]);

  for (const rule of sorted) {
    if (rule.enabled === false) continue;
    const result = evaluateRule(rule, context);
    results.push(result);

    if (!result.triggered) continue;

    if (result.action === 'block' && !result.passed) {
      blocked = true;
      break;
    }
  }

  const triggeredCount = results.filter(r => r.triggered).length;
  const blockedCount = results.filter(r => r.action === 'block' && !r.passed).length;

  return {
    status: blocked ? 'rejected' : 'completed',
    results,
    rules_evaluated: results.length,
    rules_triggered: triggeredCount,
    rules_blocked: blockedCount,
    blocked,
    confirmation_required: false,
  };
}

/**
 * Load rules from a TOML file.
 * Uses the minimal inline TOML parser — no external dependencies.
 *
 * @param {string} configPath - Path to the rules TOML file
 * @returns {Array} Array of rule objects
 */
export function loadRules(configPath) {
  if (!fs.existsSync(configPath)) {
    console.warn(`[pm-agent] Rules file not found: ${configPath}. No rules loaded.`);
    return [];
  }

  const raw = fs.readFileSync(configPath, 'utf-8');
  if (!raw.trim()) return [];

  const parsed = minimalTomlParse(raw);
  // eslint-disable-next-line no-undef
  const ruleEntries = parsed.rule;
  if (!ruleEntries || !Array.isArray(ruleEntries)) return [];

  return ruleEntries.map(r => ({
    name: r.name,
    scope: r.scope ?? 'all',
    trigger: r.trigger,
    condition: r.condition,
    action: r.action,
    severity: r.severity ?? 'info',
    description: r.description,
    enabled: r.enabled !== false,
  }));
}

// ---------------------------------------------------------------------------
// Exported API: evaluateRules (same signature as before, self-contained)
// ---------------------------------------------------------------------------

/**
 * Evaluate PM Agent rules against a tool invocation context.
 *
 * This is the main entry point for the PreToolUse hook.
 * Fully self-contained — no dependencies on core packages.
 *
 * @param {string} toolName - The Claude Code tool being called
 * @param {object} toolArgs - The arguments passed to the tool
 * @returns {{ blocked: boolean, reason?: string, warnings?: string[], actions: Array }}
 */
export function evaluateRules(toolName, toolArgs) {
  try {
    const config = resolveConfig();
    if (!config || config.rules?.enabled === false) {
      return { blocked: false, reason: 'PM Agent rules disabled', actions: [], warnings: [] };
    }

    const rulesPath = config.rules?.config_path;
    if (!rulesPath) {
      return { blocked: false, reason: 'No PM Agent rules path configured', actions: [], warnings: [] };
    }

    const rules = loadRules(rulesPath);
    if (!rules || rules.length === 0) {
      return { blocked: false, reason: 'No PM Agent rules loaded', actions: [], warnings: [] };
    }

    // Build context for rule evaluation
    const context = {
      tool_name: toolName,
      action: `calling ${toolName}`,
      tool_args: toolArgs,
      ...toolArgs,
    };

    const result = pureEvaluateRules(rules, context);

    const blocked = result.blocked === true;
    const warnings = (result.results || [])
      .filter(r => r.triggered && (r.action === 'notify' || r.action === 'suggest'))
      .map(r => r.message)
      .filter(Boolean);

    return {
      blocked,
      actions: result.results || [],
      warnings,
      reason: blocked
        ? (result.results || []).find(r => r.action === 'block' && !r.passed)?.message || 'Blocked by PM Agent rule'
        : undefined,
    };
  } catch (e) {
    console.error('[PM Agent] evaluateRules error:', e.message);
    return { blocked: false, reason: 'PM Agent evaluation error', actions: [], warnings: [] };
  }
}

// ---------------------------------------------------------------------------
// Tool classification
// ---------------------------------------------------------------------------

/**
 * Determine if a tool should be enforced by PM Agent rules.
 *
 * Write/destructive tools are enforced. Read-only and search tools
 * are allowed through to avoid friction during exploration.
 *
 * @param {string} toolName
 * @returns {boolean}
 */
export function shouldEnforce(toolName) {
  const writeTools = new Set([
    'Bash', 'Write', 'Edit', 'Rename', 'Move', 'Delete',
    'NotebookEdit', 'TaskStop', 'ExitWorktree',
  ]);
  return writeTools.has(toolName);
}

// ---------------------------------------------------------------------------
// Result formatting
// ---------------------------------------------------------------------------

/**
 * Format an enforcement result for the Claude Code hook return value.
 *
 * Returns an object that can be merged into the hook's response to
 * control auto-approval and display messages to the user.
 *
 * @param {boolean} blocked
 * @param {Array} actions - Rule evaluation results
 * @returns {object}
 */
export function wrapResult(blocked, actions) {
  if (blocked) {
    const hardBlock = (actions || []).find(
      a => a.action === 'block' && a.passed === false
    );
    return {
      autoApproval: false,
      reason: hardBlock
        ? `[PM Agent] BLOCKED: ${hardBlock.message}`
        : '[PM Agent] BLOCKED by rule enforcement',
    };
  }

  const warnings = (actions || [])
    .filter(a => a.triggered && (a.action === 'notify' || a.action === 'suggest'))
    .map(a => a.message);

  if (warnings.length > 0) {
    return {
      autoApproval: false,
      reason: `[PM Agent] ${warnings[0]}`,
    };
  }

  return {};
}
