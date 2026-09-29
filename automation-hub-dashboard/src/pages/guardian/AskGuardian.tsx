import { useState } from "react";
import Card from "../../components/common/Card";
import { Badge } from "../../components/common/ui";
import { apiPostJson, useLive } from "../../lib/api";
import { clock, CONFIDENCE_TONE, type ReasoningAnswer, type ReasoningStatus } from "./common";

/** Ask Guardian (PRD §20, §27, §43). The model sees Guardian's secret-free
 *  evidence pack only, must cite it, and every citation is checked. The
 *  answer is advice: nothing here can change the platform. */
export default function AskGuardian() {
  const status = useLive<ReasoningStatus>("/guardian/reasoning", 30000);
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);
  const [answer, setAnswer] = useState<ReasoningAnswer | null>(null);
  const [error, setError] = useState<string | null>(null);
  const s = status.data;
  const ask = async () => {
    setBusy(true); setError(null); setAnswer(null);
    try { setAnswer(await apiPostJson<ReasoningAnswer>("/guardian/reasoning/ask", { question })); status.refetch(); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  };
  return (
    <>
      <Card title="Ask Guardian" subtitle="Answers from Guardian's recorded evidence only, with citations and a confidence level">
        {s && !s.available && (
          <p className="dim" data-testid="guardian-reasoning-off">
            <Badge text="OFF" tone="default" /> The reasoning layer is off: no API key is configured
            (HUB_LLM_API_KEY or ANTHROPIC_API_KEY). Nothing is sent anywhere.
          </p>
        )}
        <textarea className="gd-input gd-question" rows={3} maxLength={2000} value={question}
          placeholder="e.g. Why did the SMC lab stop receiving candles this morning?"
          aria-label="Question for Guardian" onChange={(e) => setQuestion(e.target.value)} />
        <div className="gd-owner-buttons">
          <button type="button" className="btn btn-primary btn-sm" disabled={busy || !question.trim() || !s?.available}
            onClick={ask}>{busy ? "Reading the evidence…" : "Ask"}</button>
          <span className="dim">Advice only — it cannot change a strategy, a risk limit, an order or the paper/live mode.</span>
        </div>
        {error && <p className="neg">{error}</p>}
        {answer && (
          <div className="gd-answer" data-testid="guardian-answer">
            {answer.outcome === "ANSWERED" ? (
              <>
                <div className="gd-trace-head">
                  <Badge text={answer.confidence ?? "UNKNOWN"} tone={CONFIDENCE_TONE[answer.confidence ?? "UNKNOWN"] ?? "default"} />
                  {answer.claimed_confidence && answer.claimed_confidence !== answer.confidence && (
                    <span className="dim">the model claimed {answer.claimed_confidence}; lowered because its citations did not check out</span>)}
                </div>
                <p>{answer.answer}</p>
                <p className="dim">Evidence cited: {answer.citations?.length ? answer.citations.map((c) => <code key={c}>{c} </code>) : "none that exists"}</p>
                {!!answer.unverified_citations?.length && <p className="neg">Not in the evidence (ignored): {answer.unverified_citations.join(", ")}</p>}
                {!!answer.limitations?.length && <ul className="gd-reasons">{answer.limitations.map((l) => <li key={l} className="dim">{l}</li>)}</ul>}
              </>
            ) : <p className="neg">{answer.reason ?? answer.outcome}</p>}
            {answer.pack_sha256 && <p className="dim mono gd-cluster">evidence pack sha256 {answer.pack_sha256.slice(0, 16)}…</p>}
          </div>
        )}
      </Card>
      <Card title="Questions asked" subtitle="Each is recorded with the hash of the evidence the model was shown">
        {s?.history.length ? (
          <ul className="gd-list">
            {s.history.map((a) => (
              <li key={a.action_id}>
                <span className="mono dim">{clock(a.at)}</span>
                <Badge text={a.result} tone={a.result === "ANSWERED" ? "green" : "amber"} />
                <span>{a.reason}</span>
              </li>
            ))}
          </ul>
        ) : <p className="dim">No questions yet.</p>}
      </Card>
    </>
  );
}
