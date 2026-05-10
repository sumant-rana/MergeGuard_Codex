import { normalizePath } from "./classifier.js";

export function runPromptCanaries(files, rawFiles = [], suites = []) {
  const rawByPath = new Map(rawFiles.map((file) => [normalizePath(file.path || file.filename || file.name), file]));
  return files
    .filter((file) => file.classification === "prompt")
    .map((file) => {
      const raw = rawByPath.get(file.path) || {};
      const suite = suites.find((candidate) => candidate.prompt_path === file.path) || defaultSuite(file.path);
      const text = [raw.patch, raw.current_content, raw.content].filter(Boolean).join("\n").toLowerCase();
      const formatFail = suite.assertions?.format === "json" && looksLikeInvalidJsonPrompt(text);
      const unsafeInstruction = /ignore (previous|all) instructions|disable safety|reveal secret|bypass/.test(text);
      const correctness = unsafeInstruction ? 0.48 : 0.86;
      const format = formatFail ? 0.35 : 0.9;
      const style = text.includes("verbose") || text.includes("aggressive") ? 0.68 : 0.86;
      const latency = estimateLatencyDelta(raw);
      const cost = estimateCostDelta(raw);
      const passed =
        correctness >= (suite.thresholds?.correctness ?? 0.75) &&
        format >= (suite.thresholds?.format ?? 0.8) &&
        style >= (suite.thresholds?.style ?? 0.65) &&
        latency <= (suite.thresholds?.latency_delta_ms ?? 750) &&
        cost <= (suite.thresholds?.cost_delta_pct ?? 35);

      return {
        suite: suite.name,
        prompt_path: file.path,
        model: suite.model || "repo-default",
        correctness,
        format,
        style,
        refusal: unsafeInstruction ? 0.42 : 0.84,
        latency,
        cost,
        status: passed ? "pass" : "fail",
        drift_summary: passed
          ? "Prompt canary heuristics stayed within configured thresholds."
          : "Prompt canary drift exceeded configured thresholds.",
        before_output: suite.before_output || null,
        after_output: suite.after_output || null,
        assertions: suite.assertions || {}
      };
    });
}

export function promptFindingsFromCanaries(canaryRuns) {
  return canaryRuns
    .filter((run) => run.status === "fail")
    .map((run) => ({
      type: "prompt-canary-failure",
      path: run.prompt_path,
      severity: "block",
      confidence: 0.76,
      message: `Prompt canary suite ${run.suite} failed for ${run.prompt_path}.`,
      suggested_action: "Fix the prompt/model change or update golden canaries with reviewer approval."
    }));
}

function defaultSuite(promptPath) {
  return {
    name: "default-prompt-safety",
    prompt_path: promptPath,
    model: "repo-default",
    assertions: {
      format: promptPath.endsWith(".json") ? "json" : "text",
      safety: "no instruction bypass"
    },
    thresholds: {
      correctness: 0.75,
      format: 0.8,
      style: 0.65,
      latency_delta_ms: 750,
      cost_delta_pct: 35
    }
  };
}

function looksLikeInvalidJsonPrompt(text) {
  return text.includes("{") && !text.includes("}") || text.includes("json") && text.includes("trailing comma");
}

function estimateLatencyDelta(raw) {
  return Math.min(1200, Math.max(0, Number(raw.additions || 0) * 8 - Number(raw.deletions || 0) * 2));
}

function estimateCostDelta(raw) {
  return Math.min(100, Math.max(0, Math.round((Number(raw.additions || 0) - Number(raw.deletions || 0)) / 3)));
}
