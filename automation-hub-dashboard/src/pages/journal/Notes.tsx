import { useState } from "react";
import Card from "../../components/common/Card";
import { apiPostJson, useLive } from "../../lib/api";
import { useApp } from "../../app-context";
import { dash, type Note, whenFull } from "../../lib/journal";

/** Journal > Notes: your own notes. A note attached to a trade sits beside
 *  its record; it never edits the record's facts. */
export default function JournalNotes() {
  const { toast } = useApp();
  const notes = useLive<{ notes: Note[] }>("/journal/notes", 15000);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const save = async () => {
    if (!text.trim()) return;
    setBusy(true);
    try {
      await apiPostJson("/journal/notes", { text });
      setText("");
      void notes.refetch();
      toast("Note saved", "success");
    } catch (e) {
      toast(`Could not save the note: ${(e as Error).message}`, "error");
    } finally {
      setBusy(false);
    }
  };
  return (
    <Card title="Notes" subtitle="General notes, and notes you added on trade records">
      <div className="jr-note-form">
        <textarea value={text} maxLength={4000} rows={3} aria-label="New note"
          placeholder="A general note — to note a specific trade, open its record" onChange={(e) => setText(e.target.value)} />
        <button type="button" className="btn btn-soft btn-sm" disabled={busy || !text.trim()} onClick={() => void save()}>Save note</button>
      </div>
      {(notes.data?.notes ?? []).length === 0 ? <p className="dim">No notes yet.</p> : (
        <ul className="jr-notes">
          {(notes.data?.notes ?? []).map((n) => (
            <li key={n.note_id}>
              <span className="dim">{whenFull(n.created_at)} · {n.author}
                {n.journal_record_id ? <> · <a href={`#/trade/${n.journal_record_id}`}>{n.symbol ?? "trade"} {n.strategy_name ?? ""}</a></> : " · general"}
              </span>
              <p>{n.text || dash}</p>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}
