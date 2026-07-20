import type { ExecutionMode } from "../domain/runtime";

interface AppHeaderProps {
  executionMode: ExecutionMode | null;
}

function environmentLabel(mode: ExecutionMode | null): string {
  switch (mode) {
    case "openai_live":
      return "OpenAI live workspace";
    case "sdk_stub":
      return "SDK QA workspace";
    case "replay_fixture":
      return "Replay workspace";
    case null:
      return "Runtime pending";
  }
}

export function AppHeader({ executionMode }: AppHeaderProps) {
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
        <span className="environment-badge">
          {environmentLabel(executionMode)}
        </span>
      </div>
    </header>
  );
}
