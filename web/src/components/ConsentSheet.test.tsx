import { createRef, useState } from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ConsentSheet } from "./ConsentSheet";

function SheetHarness({ mobile = true }: { mobile?: boolean }) {
  const [open, setOpen] = useState(false);
  const triggerRef = createRef<HTMLButtonElement>();

  return (
    <>
      <button ref={triggerRef} type="button" onClick={() => setOpen(true)}>
        Review exact remedy
      </button>
      <ConsentSheet
        busy={false}
        mobile={mobile}
        onClose={() => setOpen(false)}
        open={open}
        returnFocusRef={triggerRef}
        title="Approve exact remedy"
        footer={
          <>
            <button type="button">Decline</button>
            <button type="button">Approve remedy</button>
          </>
        }
      >
        <p>Execution has not begun.</p>
      </ConsentSheet>
    </>
  );
}

beforeEach(() => {
  Object.defineProperty(HTMLDialogElement.prototype, "showModal", {
    configurable: true,
    value: vi.fn(function showModal(this: HTMLDialogElement) {
      this.open = true;
    }),
  });
  Object.defineProperty(HTMLDialogElement.prototype, "close", {
    configurable: true,
    value: vi.fn(function close(this: HTMLDialogElement) {
      this.open = false;
      this.dispatchEvent(new Event("close"));
    }),
  });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("ConsentSheet", () => {
  it("opens as a native modal without initially focusing either decision", async () => {
    render(<SheetHarness />);

    fireEvent.click(screen.getByRole("button", { name: "Review exact remedy" }));

    const dialog = await screen.findByRole("dialog", { name: "Approve exact remedy" });
    expect(HTMLDialogElement.prototype.showModal).toHaveBeenCalledOnce();
    expect(dialog).toHaveAttribute("aria-modal", "true");
    await waitFor(() =>
      expect(screen.getByRole("heading", { name: "Approve exact remedy" })).toHaveFocus(),
    );
    expect(screen.getByRole("button", { name: "Decline" })).not.toHaveFocus();
    expect(screen.getByRole("button", { name: "Approve remedy" })).not.toHaveFocus();
    expect(dialog.querySelector(".consent-sheet-footer")).not.toBeNull();
  });

  it("closes explicitly and restores focus to the invoking control", async () => {
    render(<SheetHarness />);
    const trigger = screen.getByRole("button", { name: "Review exact remedy" });
    trigger.focus();
    fireEvent.click(trigger);

    fireEvent.click(
      await screen.findByRole("button", { name: "Close evidence sheet" }),
    );

    await waitFor(() => expect(trigger).toHaveFocus());
    expect(screen.queryByRole("dialog", { name: "Approve exact remedy" })).not.toBeInTheDocument();
  });

  it("uses a complementary desktop inspector instead of modal semantics", () => {
    render(<SheetHarness mobile={false} />);

    const inspector = screen.getByRole("complementary", { name: "Approve exact remedy" });
    expect(inspector).toBeVisible();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});
