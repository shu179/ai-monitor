type AnimatedLoadingTextProps = {
  className?: string;
};

export function AnimatedLoadingText({ className = "" }: AnimatedLoadingTextProps) {
  return (
    <span className={className}>
      <span>Surfacing new opportunities</span>
      <span className="ml-0.5 inline-flex text-[var(--brand-cyan)]" aria-hidden="true">
        <span>.</span>
        <span className="loading-brand-dot-2">.</span>
        <span className="loading-brand-dot-3">.</span>
      </span>
    </span>
  );
}
