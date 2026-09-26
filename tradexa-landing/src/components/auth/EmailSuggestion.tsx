/** "Did you mean …?" under an email field. Applying it is the person's choice. */
export function EmailSuggestion({ suggestion, onApply }: { suggestion: string; onApply: () => void }) {
  return (
    <p className="mt-1.5 text-xs text-white/55" aria-live="polite">
      Did you mean{" "}
      <button
        type="button"
        onClick={onApply}
        className="rounded font-medium text-gold underline decoration-gold/40 underline-offset-2 transition-colors hover:text-gold-soft focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-gold/60"
      >
        {suggestion}
      </button>
      ?
    </p>
  );
}
