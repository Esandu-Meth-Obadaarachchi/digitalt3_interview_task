/**
 * M13 and M12: what one person is carrying, and the recap somebody sends.
 *
 * Three things a reviewer needs to see here.
 *
 * That grouping two people under one first name is a decision, not an
 * accident. Every person carries the owner strings collapsed into them, a
 * digest covering more than one full name says so, and each commitment names
 * the owner exactly as the transcript stated it.
 *
 * That unowned work is visible without being assigned. It gets its own digest
 * headed "Assignee unspecified" rather than being dropped or given to
 * somebody.
 *
 * That the agent never sends. The send button names the person in the header
 * and there is no default anywhere behind it: the API refuses a blank name and
 * refuses a service name, and the database refuses both again.
 */

import { useEffect, useState } from "react";
import { api, ApiFailure } from "../api/client";
import type { FollowUpDraft, Person, PersonDigest, Source } from "../api/types";
import { Badge, Button, Empty, ErrorNote, Field, Panel } from "../components/ui";

export function PeopleView({ reviewer }: { reviewer: string }) {
  const [people, setPeople] = useState<Person[]>([]);
  const [selected, setSelected] = useState("");
  const [digest, setDigest] = useState<PersonDigest | null>(null);

  const [sources, setSources] = useState<Source[]>([]);
  const [source, setSource] = useState("");
  const [draft, setDraft] = useState<FollowUpDraft | null>(null);
  const [text, setText] = useState("");
  const [drafts, setDrafts] = useState<FollowUpDraft[]>([]);

  const [busy, setBusy] = useState("");
  const [error, setError] = useState<{ code?: string; message: string } | null>(null);

  const fail = (exc: unknown) =>
    setError(exc instanceof ApiFailure ? { code: exc.code, message: exc.message } : { message: String(exc) });

  const loadPeople = () =>
    api.people().then((found) => {
      setPeople(found);
      if (found.length && !selected) setSelected(found[0].key);
    });

  useEffect(() => {
    loadPeople().catch(() => setPeople([]));
    api.sources().then((all) => {
      const ingested = all.filter((s) => s.status === "ingested" && s.source_type !== "chat_export");
      setSources(ingested);
      if (ingested.length && !source) setSource(ingested[0].id);
    });
    api.followups().then(setDrafts).catch(() => setDrafts([]));
  }, []);

  useEffect(() => {
    if (!selected) return;
    api.personDigest(selected).then(setDigest).catch(fail);
  }, [selected]);

  const run = async (action: string, work: () => Promise<void>) => {
    setBusy(action);
    setError(null);
    try {
      await work();
    } catch (exc) {
      fail(exc);
    } finally {
      setBusy("");
    }
  };

  const preview = () =>
    run("building the recap", async () => {
      const built = await api.previewFollowup(source);
      setDraft(built);
      setText(built.generated_body);
    });

  const create = () =>
    run("saving the draft", async () => {
      const created = await api.createFollowup(source);
      setDraft(created);
      setText(created.generated_body);
      setDrafts(await api.followups());
    });

  const save = () =>
    run("saving the edit", async () => {
      if (!draft) return;
      const edited = await api.editFollowup(draft.id, text, reviewer);
      setDraft(edited);
      setDrafts(await api.followups());
    });

  const send = () =>
    run("sending", async () => {
      if (!draft) return;
      const sent = await api.sendFollowup(draft.id, reviewer, "recap");
      setDraft(sent);
      setDrafts(await api.followups());
    });

  const unsaved = Boolean(draft) && draft?.draft_version === 0;
  const sent = draft?.status === "sent";

  return (
    <div className="space-y-4">
      <ErrorNote error={error} />

      <Panel
        title="Per-person digests"
        subtitle="One per person who has approved commitments. Somebody with none gets no digest."
        actions={
          <Button
            tone="ok"
            disabled={Boolean(busy)}
            onClick={() =>
              run("writing every person digest", async () => {
                await api.runAllPersonDigests();
                await loadPeople();
              })
            }
            title="Writes a digest for everyone who has one. Nobody empty is written."
          >
            Write them all
          </Button>
        }
      >
        {people.length === 0 ? (
          <Empty>Nothing approved yet, so nobody has commitments. Approve items in the review queue first.</Empty>
        ) : (
          <div className="grid gap-0 lg:grid-cols-[16rem_1fr]">
            <ul className="divide-y divide-[var(--color-line)] border-b border-[var(--color-line)] lg:border-b-0 lg:border-r">
              {people.map((person) => (
                <li key={person.key}>
                  <button
                    type="button"
                    onClick={() => setSelected(person.key)}
                    className={`w-full px-4 py-2.5 text-left text-sm transition-colors ${
                      selected === person.key ? "bg-[var(--color-line)]/40 font-medium" : ""
                    }`}
                  >
                    <span className="flex items-center justify-between gap-2">
                      {person.unassigned ? "Assignee unspecified" : person.display_name}
                      {person.ambiguous && <Badge tone="warn">grouped</Badge>}
                      {person.unassigned && <Badge tone="warn">nobody</Badge>}
                    </span>
                    {person.aliases.length > 1 && (
                      <span className="mt-0.5 block text-xs text-[var(--color-muted)]">
                        {person.aliases.join(" · ")}
                      </span>
                    )}
                  </button>
                </li>
              ))}
            </ul>

            <div>
              {!digest ? (
                <Empty>Select somebody.</Empty>
              ) : (
                <>
                  <div className="grid grid-cols-2 gap-4 border-b border-[var(--color-line)] px-4 py-3 sm:grid-cols-4">
                    <Field label="Person">
                      {digest.unassigned ? "Assignee unspecified" : digest.display_name}
                    </Field>
                    <Field label="Commitments">{digest.commitments.length}</Field>
                    <Field label="Date">{digest.digest_date}</Field>
                    <Field label="Trigger">{digest.trigger}</Field>
                  </div>

                  {digest.aliases.length > 1 && !digest.unassigned && (
                    <p className="border-b border-[var(--color-line)] bg-[var(--color-line)]/20 px-4 py-2 text-xs text-[var(--color-muted)]">
                      Grouped by first name, so this covers {digest.aliases.join(", ")}. Each line
                      names the owner exactly as the transcript stated it.
                    </p>
                  )}

                  {digest.unassigned && (
                    <p className="border-b border-[var(--color-line)] bg-[var(--color-line)]/20 px-4 py-2 text-xs text-[var(--color-muted)]">
                      Nobody was named for these. They are listed rather than dropped, and nothing
                      here was assigned by guessing.
                    </p>
                  )}

                  {digest.commitments.length === 0 ? (
                    <Empty>No approved commitments stand against this name.</Empty>
                  ) : (
                    <ul className="divide-y divide-[var(--color-line)]">
                      {digest.commitments.map((line) => (
                        <li key={line.extraction_id} className="px-4 py-2.5">
                          <p className="text-sm">{line.text}</p>
                          <p className="mt-0.5 text-xs text-[var(--color-muted)]">
                            {digest.unassigned ? (
                              <span className="text-[var(--color-warn)]">
                                assignee UNSPECIFIED, nobody was named
                              </span>
                            ) : (
                              <>owner as stated: {line.owner_as_stated}</>
                            )}{" "}
                            ·{" "}
                            {line.due_date && line.due_date !== "UNSPECIFIED" ? (
                              <>due {line.due_date}</>
                            ) : (
                              <span className="text-[var(--color-warn)]">
                                due UNSPECIFIED, no date was stated
                              </span>
                            )}
                          </p>
                          <blockquote className="quote mt-1 border-l-2 border-[var(--color-line)] pl-3 text-[var(--color-muted)]">
                            “{line.citation.quote}”
                          </blockquote>
                          <p className="mt-0.5 text-xs text-[var(--color-muted)]">
                            {line.citation.speaker ?? "unattributed"},{" "}
                            {line.citation.source_title ?? line.citation.source_id}
                            {line.citation.timestamp && ` at ${line.citation.timestamp}`}
                          </p>
                        </li>
                      ))}
                    </ul>
                  )}
                </>
              )}
            </div>
          </div>
        )}
      </Panel>

      <Panel
        title="Follow-up recap"
        subtitle="Built from approved items. A person edits it, and a person sends it."
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <select
              className="rounded border border-[var(--color-line)] px-2 py-1 text-xs"
              value={source}
              onChange={(e) => setSource(e.target.value)}
            >
              {sources.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.title}
                </option>
              ))}
            </select>
            <Button tone="info" disabled={Boolean(busy)} onClick={preview}>
              Preview
            </Button>
            <Button tone="ok" disabled={Boolean(busy)} onClick={create}>
              Save as a draft
            </Button>
          </div>
        }
      >
        {busy && (
          <div className="flex items-center gap-2 px-4 py-3 text-xs text-[var(--color-muted)]">
            <span className="h-3 w-3 animate-spin rounded-full border-2 border-[var(--color-line)] border-t-[var(--color-info)]" />
            {busy}…
          </div>
        )}

        {!draft && !busy && <Empty>Preview a recap to see it.</Empty>}

        {draft && !busy && (
          <>
            <div className="grid grid-cols-2 gap-4 border-b border-[var(--color-line)] px-4 py-3 sm:grid-cols-4">
              <Field label="Subject">{draft.subject}</Field>
              <Field label="Approved items">{draft.item_count}</Field>
              <Field label="Version">{unsaved ? "unsaved preview" : `v${draft.draft_version}`}</Field>
              <Field label="Status">
                <Badge tone={sent ? "ok" : draft.status === "edited" ? "warn" : "neutral"}>
                  {draft.status}
                </Badge>
              </Field>
            </div>

            <div className="px-4 py-3">
              <textarea
                className="h-72 w-full rounded border border-[var(--color-line)] p-3 font-mono text-xs"
                value={text}
                onChange={(e) => setText(e.target.value)}
                readOnly={sent}
                spellCheck={false}
              />
              <p className="mt-1 text-xs text-[var(--color-muted)]">
                Editing does not overwrite what the system wrote. Both versions stay readable, so
                what a person changed is answerable afterwards.
                {draft.edited_by && ` Last edited by ${draft.edited_by}.`}
              </p>
            </div>

            <div className="flex flex-wrap items-center gap-2 border-t border-[var(--color-line)] px-4 py-3">
              <Button tone="info" disabled={Boolean(busy) || unsaved || sent} onClick={save}>
                Save my version
              </Button>
              <Button tone="ok" disabled={Boolean(busy) || unsaved || sent} onClick={send}>
                Send as {reviewer || "…"}
              </Button>
              {unsaved && (
                <span className="text-xs text-[var(--color-muted)]">
                  Save it as a draft before editing or sending.
                </span>
              )}
              {sent && (
                <span className="text-xs text-[var(--color-muted)]">
                  Sent by {draft.sent_by} to {draft.channel}. A sent message cannot be rewritten.
                </span>
              )}
            </div>

            <p className="border-t border-[var(--color-line)] px-4 py-2 text-xs text-[var(--color-muted)]">
              The send names the person in the header. There is no default behind it: a blank name
              and a service name are both refused, by the API and again by the database. The agent
              never sends.
            </p>
          </>
        )}
      </Panel>

      <Panel title="Drafts" subtitle="Every recap created, and who sent it">
        {drafts.length === 0 ? (
          <Empty>No draft saved yet.</Empty>
        ) : (
          <ul className="divide-y divide-[var(--color-line)]">
            {drafts.map((d) => (
              <li key={d.id} className="flex flex-wrap items-center justify-between gap-2 px-4 py-2.5 text-xs">
                <button
                  type="button"
                  className="text-left hover:underline"
                  onClick={() => {
                    setDraft(d);
                    setText(d.edited_body ?? d.generated_body);
                  }}
                >
                  {d.source_title ?? d.source_id} · v{d.draft_version}
                </button>
                <span className="flex items-center gap-1.5 text-[var(--color-muted)]">
                  <Badge tone={d.status === "sent" ? "ok" : d.status === "edited" ? "warn" : "neutral"}>
                    {d.status}
                  </Badge>
                  {d.sent_by && <>sent by {d.sent_by}</>}
                  {d.edited_by && !d.sent_by && <>edited by {d.edited_by}</>}
                  <span>{d.item_count} item(s)</span>
                </span>
              </li>
            ))}
          </ul>
        )}
      </Panel>
    </div>
  );
}
