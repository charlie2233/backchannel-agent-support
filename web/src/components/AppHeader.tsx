interface AppHeaderProps {
  workspace: string;
}

export function AppHeader({ workspace }: AppHeaderProps) {
  return (
    <header className="top-bar">
      <a className="brand" href="#workspace" aria-label="Backchannel console home">
        <span className="brand-mark" aria-hidden="true">
          <span />
          <span />
        </span>
        <span>Backchannel</span>
      </a>
      <div className="top-context">
        <span>Operational recovery console</span>
        <span className="environment-badge">{workspace}</span>
      </div>
    </header>
  );
}
