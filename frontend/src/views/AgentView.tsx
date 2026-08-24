/**
 * M14 made watchable.
 *
 * The trace is the point of this screen. An answer produced by a sequence of
 * tool calls is worth exactly what a reader's ability to see those calls is
 * worth, so the steps are shown in order with their arguments and what came
 * back, and the answer sits underneath them rather than on its own.
 *
 * The toolbelt is listed with the one writing tool marked, because the safety
 * claim is about what is absent. Saying "the agent cannot approve anything" is
 * weaker than showing the nine tools it has and letting a reader notice that
 * approve is not among them.
 */

import { useEffect, useState } from "react";
import { api, ApiFailure } from "../api/client";
import type { AgentRun, AgentTool, Source } from "../api/types";
import { Badge, Button, Empty, ErrorNote, Field, Panel } from "../components/ui";

const EXAMPLES = [
  "Which meetings or channels mention the staging environment being down, and who raised it?",
  "What has Priya committed to, across every meeting? Quote each one.",
  "Summarise what is blocked right now and say who raised each blocker.",
  "What is waiting for review, and which items have no owner?",
];

export function AgentView() {
  const [tools, setTools] = useState<AgentTool[]>([]);
  const [sources, setSources] = useState<Source[]>([]);
  const [scope, setScope] = useState("");
  const [instruction, setInstruction] = useState(EXAMPLES[0]);
  const [budget, setBudget] = useState(6);
  const [run, setRun] = useState<AgentRun | null>(null);
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState<number | null>(null);
  const [error, setError] = useState<{ code?: string; message: string } | null>(null);

  useEffect(() => {
    api.agentTools().then(setTools).catch(() => setTools([]));
    api
      .sources()
      .then((all) => setSources(all.filter((s) => s.status === "ingested")))
      .catch(() => setSources([]));
  }, []);

  const go = async () => {
    if (!instruction.trim()) return;
    setBusy(true);
    setError(null);
    setRun(null);
    try {
      setRun(await api.runAgent(instruction.trim(), budget, scope ? [scope] : undefined));
    } catch (exc) {
      setError(exc instanceof ApiFailure ? { code: exc.code, message: exc.message } : { message: String(exc) });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-4">
      <Panel
        title="Agent"
        subtitle="Plans, calls tools, reads what came back, decides again. It cannot approve, write or send."
        actions={
          <div className="flex flex-wrap items-center gap-2">
            {/* A metadata filter, not a prompt instruction. The tools refuse
                anything outside it, so the model cannot wander into another
                project by forgetting. */}
            <label className="flex items-center gap-1 text-xs text-[var(--color-muted)]">
              scope
              <select
                className="max-w-[16rem] rounded border border-[var(--color-line)] px-2 py-1 text-xs"
                value={scope}
                onChange={(e) => setScope(e.target.value)}
                title="Restricts every tool to one source. Enforced in the tools, not asked for in the prompt."
              >
                <option value="">every source</option>
                {sources.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.title}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex items-center gap-1 text-xs text-[var(--color-muted)]">
              step budget
              <input
                type="number"
                min={1}
                max={20}
                className="w-14 rounded border border-[var(--color-line)] px-1.5 py-1"
                value={budget}
                onChange={(e) => setBudget(Math.max(1, Math.min(20, Number(e.target.value) || 1)))}
              />
            </label>
            <Button tone="ok" disabled={busy || !instruction.trim()} onClick={go}>
              {busy ? "running…" : "Run"}
            </Button>
          </div>
        }
      >
        <div className="space-y-2 px-4 py-3">
          <textarea
            className="h-20 w-full rounded border border-[var(--color-line)] p-2 text-sm"
            value={instruction}
            onChange={(e) => setInstruction(e.target.value)}
            placeholder="Ask it to find something across the meetings and channels"
          />
          <div className="flex flex-wrap gap-1">
            {EXAMPLES.map((example) => (
              <Button key={example} onClick={() => setInstruction(example)}>
                {example.length > 46 ? `${example.slice(0, 46)}…` : example}
              </Button>
            ))}
          </div>
        </div>

        {busy && (
          <div className="flex items-center gap-2 border-t border-[var(--color-line)] px-4 py-3 text-xs text-[var(--color-muted)]">
            <span className="h-3 w-3 animate-spin rounded-full border-2 border-[var(--color-line)] border-t-[var(--color-info)]" />
            planning, calling a tool, then planning again. One model call per step.
          </div>
        )}
      </Panel>

      <ErrorNote error={error} />

      {run && (
        <>
          <Panel title="The loop" subtitle={`${run.steps_used} of ${run.step_budget} steps used`}>
            <div className="grid grid-cols-2 gap-4 border-b border-[var(--color-line)] px-4 py-3 sm:grid-cols-4">
              <Field label="Stopped because">
                <Badge tone={run.stop_reason === "answered" ? "ok" : run.stop_reason === "step_budget" ? "warn" : "bad"}>
                  {run.stop_reason.replace("_", " ")}
                </Badge>
              </Field>
              <Field label="Tool calls">{run.steps_used}</Field>
              <Field label="Scope">
                {run.scope.length === 0 ? (
                  "every source"
                ) : (
                  <Badge tone="info">{run.scope.join(", ")}</Badge>
                )}
              </Field>
              <Field label="Planner">{run.provider}:{run.model}</Field>
              <Field label="Took">{(run.duration_ms / 1000).toFixed(1)}s</Field>
            </div>

            {run.steps.length === 0 ? (
              <Empty>It answered without calling a tool.</Empty>
            ) : (
              <ol className="divide-y divide-[var(--color-line)]">
                {run.steps.map((step) => (
                  <li key={step.step} className="px-4 py-2.5">
                    <button
                      type="button"
                      className="flex w-full flex-wrap items-center gap-2 text-left"
                      onClick={() => setOpen(open === step.step ? null : step.step)}
                    >
                      <span className="font-mono text-xs text-[var(--color-muted)]">{step.step}</span>
                      <Badge tone={step.ok ? "info" : "bad"}>{step.tool}</Badge>
                      <span className="font-mono text-xs text-[var(--color-muted)]">
                        {Object.entries(step.arguments)
                          .map(([key, value]) => `${key}=${JSON.stringify(value)}`)
                          .join(" ") || "no arguments"}
                      </span>
                      <span className="ml-auto text-xs text-[var(--color-muted)]">
                        {step.observation_chars.toLocaleString()} chars · {step.duration_ms}ms
                      </span>
                    </button>
                    {open === step.step && (
                      <pre className="mt-2 max-h-64 overflow-auto rounded border border-[var(--color-line)] bg-[var(--color-canvas)] p-2 text-xs whitespace-pre-wrap">
                        {step.observation}
                      </pre>
                    )}
                    {!step.ok && step.error && (
                      <p className="mt-1 text-xs text-[var(--color-bad)]">{step.error}</p>
                    )}
                  </li>
                ))}
              </ol>
            )}
          </Panel>

          <Panel title="Answer" subtitle="Built only from what the tools returned">
            {run.answer ? (
              <p className="whitespace-pre-wrap px-4 py-3 text-sm">{run.answer}</p>
            ) : (
              <Empty>No answer was produced.</Empty>
            )}
            {run.stop_reason === "step_budget" && (
              <p className="border-t border-[var(--color-line)] bg-[var(--color-warn-bg)] px-4 py-2 text-xs">
                The budget ran out before it decided it was done. The answer is what it had at that
                point, and it was asked to say what it could not check.
              </p>
            )}
          </Panel>
        </>
      )}

      <Panel
        title="What it is allowed to do"
        subtitle="The safety claim is about what is absent from this list"
      >
        {tools.length === 0 ? (
          <Empty>Tool list unavailable.</Empty>
        ) : (
          <ul className="divide-y divide-[var(--color-line)]">
            {tools.map((tool) => (
              <li key={tool.name} className="flex flex-wrap items-baseline gap-2 px-4 py-2 text-xs">
                <span className="font-mono">{tool.name}</span>
                {tool.writes ? (
                  <Badge tone="warn">writes, as pending</Badge>
                ) : (
                  <Badge tone="neutral">reads</Badge>
                )}
                <span className="text-[var(--color-muted)]">{tool.description}</span>
              </li>
            ))}
          </ul>
        )}
        <p className="border-t border-[var(--color-line)] px-4 py-2 text-xs text-[var(--color-muted)]">
          There is no tool for approving an extraction, writing to the tracker or sending a
          follow-up. The loop may read anything in scope and propose into the review queue, and a
          person still holds all three gates. When a scope is set the tools refuse anything outside
          it, and <code>focus_on_source</code> can narrow further but never widen.
        </p>
      </Panel>
    </div>
  );
}
