import { useEffect, useId, useRef, type ReactNode, type RefObject } from "react";

interface ConsentSheetProps {
  busy?: boolean;
  children: ReactNode;
  footer?: ReactNode;
  mobile: boolean;
  onClose: () => void;
  open: boolean;
  returnFocusRef: RefObject<HTMLElement | null>;
  title: string;
}

export function ConsentSheet({
  busy = false,
  children,
  footer,
  mobile,
  onClose,
  open,
  returnFocusRef,
  title,
}: ConsentSheetProps) {
  const headingId = useId();
  const dialogRef = useRef<HTMLDialogElement>(null);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const closeHandledRef = useRef(false);

  useEffect(() => {
    if (!mobile) {
      return;
    }
    const dialog = dialogRef.current;
    if (dialog === null) {
      return;
    }
    if (open && !dialog.open) {
      closeHandledRef.current = false;
      dialog.showModal();
      queueMicrotask(() => {
        if (dialogRef.current?.open) {
          headingRef.current?.focus();
        }
      });
    } else if (!open && dialog.open) {
      dialog.close();
    }
  }, [mobile, open]);

  const finishClose = () => {
    if (closeHandledRef.current) {
      return;
    }
    closeHandledRef.current = true;
    const returnTarget = returnFocusRef.current;
    onClose();
    queueMicrotask(() => returnTarget?.focus());
  };

  const requestClose = () => {
    if (busy) {
      return;
    }
    const dialog = dialogRef.current;
    if (mobile && dialog?.open) {
      dialog.close();
      finishClose();
      return;
    }
    finishClose();
  };

  const content = (
    <>
      <header className="consent-sheet-header">
        <div>
          <p className="eyebrow">Evidence inspector</p>
          <h2 id={headingId} ref={headingRef} tabIndex={-1}>
            {title}
          </h2>
        </div>
        {mobile ? (
          <button
            className="sheet-close"
            type="button"
            disabled={busy}
            onClick={requestClose}
          >
            Close evidence sheet
          </button>
        ) : null}
      </header>
      <div className="consent-sheet-body">{children}</div>
      {footer === undefined ? null : (
        <footer className="consent-sheet-footer">{footer}</footer>
      )}
    </>
  );

  if (!mobile) {
    return (
      <aside className="evidence-inspector" aria-labelledby={headingId}>
        {content}
      </aside>
    );
  }

  return (
    <dialog
      ref={dialogRef}
      className="consent-sheet"
      aria-labelledby={headingId}
      aria-modal="true"
      onCancel={(event) => {
        if (busy) {
          event.preventDefault();
        }
      }}
      onClose={finishClose}
    >
      {content}
    </dialog>
  );
}
