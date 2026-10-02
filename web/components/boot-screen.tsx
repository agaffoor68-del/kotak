/** Full-viewport placeholder shown while the session is being resolved. */

export function BootScreen() {
  return (
    <div className="flex min-h-screen items-center justify-center">
      <div className="flex items-center gap-3 text-ink-faint">
        <span className="h-4 w-4 animate-spin rounded-full border-2 border-edge border-t-accent" />
        <span className="text-xs uppercase tracking-widest">Loading AlphaTradePro</span>
      </div>
    </div>
  );
}